"""Start the washer on solar surplus, once it has been armed for remote start.

The machine is loaded and its programme chosen by hand; the "remote start"
button then hands the *when* to Home Assistant. Two bars:

* the heating covered — 90 % of the cycle from the sun: start;
* half the cycle from the sun — acceptable only when nothing better is to be
  expected: production unsteady, or the day's forecast peak an hour behind.

The cost is estimated, not hoped for:

* what the washer draws, minute by minute — its profile, measured;
* what the house can give it — the export it has *held*, not its average: a
  passing cloud is not a sunny spell. Nothing is sold back, so that export is
  otherwise lost;
* whether that lasts — the solar forecast for the next hour.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .samples import Trace

# The washer's draw on 3 Oct 2026, 86-minute programme: (minutes, watts). It
# heats twice, early — 39 Wh at 1.5 kW, then 158 Wh at 2.1 kW — and tumbles
# at ~90 W for the remaining hour. 317 Wh in all, 197 of them in the first
# twelve minutes: the heating is what a start has to be timed for.
WASHER_PROFILE: tuple[tuple[float, float], ...] = (
    (1.4, 59.0),
    (1.5, 1552.0),
    (4.4, 84.0),
    (4.5, 2109.0),
    (73.0, 92.0),
)


@dataclass(frozen=True)
class SolarStartConfig:
    # Nothing is sold back, so an exported kWh is worth nothing and the saving
    # is simply the share of the cycle the sun covers.
    # The heating covered: what a sunny day is waited for.
    good_saving: float = 0.9
    # At least half cheaper than the grid — only when no better is coming.
    # Replayed on the twelve days below, waiting this way raised the saving
    # actually obtained from 0.61 to 0.68 on average, never under 0.51.
    min_saving: float = 0.5
    # Unsteady production: the panels' 20th-80th percentile spread over the
    # window, against their median. Steady mornings measured 0.07-0.28, a
    # cloudy afternoon 1.08.
    unsteady_spread: float = 0.3
    # Forecast.Solar's peak time moves by an hour or two through the day; an
    # hour past it, the day's best is behind.
    after_peak: timedelta = timedelta(hours=1)
    # The surplus counted on is the level the export held 80 % of the last
    # half hour. Replayed on twelve days (22 Sept - 4 Oct) against what the
    # sun then actually gave: 30 min made 8 starts, all at 51-80 % saved;
    # 15 min made 10, five of them at 26-43 % — a cloud on its way looks
    # like sunshine for a quarter of an hour. 45 min only started later.
    window: timedelta = timedelta(minutes=30)
    held_quantile: float = 0.2
    # A command the cloud swallowed is asked again — but not every reading.
    # The same goes for waking the machine up.
    retry_after: timedelta = timedelta(minutes=5)


@dataclass(frozen=True)
class Forecast:
    """Solar production expected now, and on average over the next hour."""

    now_w: float
    next_hour_w: float

    @property
    def decline_w(self) -> float:
        """How much less the panels should give in an hour — never a gain:
        a surplus that has not appeared yet is not counted on."""
        return max(0.0, self.now_w - self.next_hour_w)


@dataclass(frozen=True)
class Estimate:
    """What starting now would be worth."""

    surplus_w: float
    energy_wh: float
    solar_wh: float
    saving: float  # 0..1, against the whole cycle from the grid
    cost_eur: float | None
    grid_cost_eur: float | None


@dataclass(frozen=True)
class Decision:
    waiting: bool
    estimate: Estimate | None
    start: bool = False
    # Armed, the machine falls asleep after ~12 min and must be woken before
    # it takes "start". Start follows once ThinQ reports it awake.
    wake: bool = False
    # The bar this moment is held to, and why.
    required: float | None = None
    reason: str = ""


def profile_for(total: timedelta | None) -> tuple[tuple[float, float], ...]:
    """The measured profile, its tail stretched or cut to the programme's length.

    The heating comes first whatever the programme; what varies is how long
    the drum then turns.
    """
    if total is None:
        return WASHER_PROFILE
    head = WASHER_PROFILE[:-1]
    tail_minutes = total.total_seconds() / 60 - sum(minutes for minutes, _ in head)
    return (*head, (max(0.0, tail_minutes), WASHER_PROFILE[-1][1]))


def estimate(profile: tuple[tuple[float, float], ...], surplus_w: float,
             forecast: Forecast | None, price: float | None) -> Estimate:
    """Energy the cycle would take, how much of it the sun would cover, and
    what that saves. Minute by minute, so the heating is met by the surplus of
    *its* minutes, not by an average over the hour."""
    energy = solar = 0.0
    clock = 0.0
    for minutes, watts in profile:
        whole = int(minutes)
        steps = [1.0] * whole + ([minutes - whole] if minutes > whole else [])
        for step in steps:
            available = surplus_w
            if forecast is not None and clock >= 60:
                available -= forecast.decline_w
            energy += watts * step / 60
            solar += min(watts, max(0.0, available)) * step / 60
            clock += step
    if energy <= 0:
        return Estimate(surplus_w, 0.0, 0.0, 0.0, None, None)
    # The price is the same on both sides, so it only puts euros on the share.
    saving = solar / energy
    if price is None:
        return Estimate(surplus_w, energy, solar, saving, None, None)
    return Estimate(surplus_w, energy, solar, saving,
                    round((energy - solar) / 1000 * price, 3),
                    round(energy / 1000 * price, 3))


class SolarStarter:
    """Says when to press "start", from the grid meter's recent past."""

    def __init__(self, config: SolarStartConfig | None = None) -> None:
        self.config = config or SolarStartConfig()
        self._trace = Trace(keep=self.config.window + timedelta(minutes=1))
        self._pv = Trace(keep=self.config.window + timedelta(minutes=1))
        self._asked_at: datetime | None = None
        self._woken_at: datetime | None = None

    def feed_grid(self, at: datetime, watts: float) -> None:
        """One grid reading, signed: negative is export."""
        self._trace.add(at, watts)

    def feed_pv(self, at: datetime, watts: float) -> None:
        """One reading of the panels' own production."""
        self._pv.add(at, watts)

    def _observed(self, trace: Trace, at: datetime) -> bool:
        first = trace.first
        return first is not None and first[0] <= at - self.config.window

    def pv_spread(self, at: datetime) -> float | None:
        """How unsteady production has been, or None when it cannot be said."""
        if not self._observed(self._pv, at):
            return None
        start = at - self.config.window
        median = self._pv.quantile(start, at, 0.5)
        low = self._pv.quantile(start, at, 0.2)
        high = self._pv.quantile(start, at, 0.8)
        if median is None or low is None or high is None or median <= 50:
            return None
        return (high - low) / median

    def _required(self, at: datetime,
                  peak_at: datetime | None) -> tuple[float, str]:
        """The bar for now: the heating covered, unless nothing better is coming.

        Only *known* steadiness before a *known* peak holds out for more. An
        unknown is not good news, and half the cycle from the sun is still a
        sound start.
        """
        cfg = self.config
        spread = self.pv_spread(at)
        if spread is None or spread > cfg.unsteady_spread:
            return cfg.min_saving, "production instable"
        if peak_at is None or at >= peak_at + cfg.after_peak:
            return cfg.min_saving, "pic de production passé"
        return cfg.good_saving, "production stable avant le pic : chauffe couverte attendue"

    def held_export_w(self, at: datetime) -> float | None:
        """The export held most of the window, or None until it is observed.

        A reading holds until the next one, so one sample would otherwise
        cover the whole window by itself — right after a restart, say.
        """
        if not self._observed(self._trace, at):
            return None
        start = at - self.config.window
        # The grid's *upper* quantile is the export's lower one.
        level = self._trace.quantile(start, at, 1 - self.config.held_quantile)
        return None if level is None else max(0.0, -level)

    def decide(self, at: datetime, *, armed: bool | None,
               washer_running: bool | None, enabled: bool,
               asleep: bool = False, total: timedelta | None = None, price: float | None = None,
               forecast: Forecast | None = None,
               peak_at: datetime | None = None) -> Decision:
        """Whether to start now — True once per attempt.

        Every condition has to be *known*: a remote-start flag or a washer
        status that ThinQ cannot report is no permission to start a machine.
        """
        if not (enabled and armed is True and washer_running is False):
            self._asked_at = self._woken_at = None
            return Decision(waiting=False, estimate=None)
        surplus = self.held_export_w(at)
        if surplus is None:
            return Decision(waiting=True, estimate=None)
        result = estimate(profile_for(total), surplus, forecast, price)
        required, reason = self._required(at, peak_at)
        if result.saving >= self.config.good_saving:
            required, reason = self.config.good_saving, "chauffe couverte"
        decision = Decision(waiting=True, estimate=result,
                            required=required, reason=reason)
        if result.saving < required:
            return decision
        if asleep:
            if self._recent(self._woken_at, at):
                return decision
            self._woken_at = at
            return replace(decision, wake=True)
        if self._recent(self._asked_at, at):
            return decision
        self._asked_at = at
        return replace(decision, start=True)

    def _recent(self, asked_at: datetime | None, at: datetime) -> bool:
        return asked_at is not None and at - asked_at < self.config.retry_after


__all__ = [
    "WASHER_PROFILE",
    "Decision",
    "Estimate",
    "Forecast",
    "SolarStartConfig",
    "SolarStarter",
    "estimate",
    "profile_for",
]
