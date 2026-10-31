"""Start the washer on solar surplus, once it has been armed for remote start.

The machine is loaded and its programme chosen by hand; the "remote start"
button then hands the *when* to Home Assistant. Two bars:

* the heating covered — 90 % of the cycle from the sun: start;
* half the cycle from the sun — acceptable only when nothing better is to be
  expected: production unsteady, the day's forecast peak an hour behind, or
  no hour left today forecast to cover the heating.

The cost is estimated, not hoped for:

* what the washer draws, minute by minute — its profile, measured;
* what the house can give it — the export it has *held*, not its average: a
  passing cloud is not a sunny spell. Nothing is sold back, so that export is
  otherwise lost;
* whether that lasts — the solar forecast for the next hour.

And it does not wait for a sun that will not come. When the forecast, scaled
by what the panels really produce, can no longer reach the lower bar today —
for a whole observation window, so that a cloud is not taken for the day — it
settles for the best the sun can still give: the hour of the hourly curve whose
surplus, the house's own draw taken off, saves the most. Today's if one is
worth it, else tomorrow's — armed in the evening, it waits for tomorrow's best
hour. Nothing worth waiting for: it starts now, or at the off-peak hours on a
Rouge peak.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .samples import Trace
from .solar_curve import Sky, hours_between
from .sun_outlook import ForecastNow, Outlook, outlook, realism

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
    # Out of reach of the lower bar, the best hour ahead is waited for only if
    # it saves at least this much, and this much more than starting now.
    min_target_saving: float = 0.15
    target_gain: float = 0.05


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
    # Out of reach of the bar: the hour waited for instead, and its saving.
    target_at: datetime | None = None
    target_saving: float | None = None
    # When the start is expected, from the forecast, and what it should save.
    expected_at: datetime | None = None
    expected_saving: float | None = None


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


def _end_of_day(at: datetime) -> datetime:
    return at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def hourly_savings(at: datetime, sky: ForecastNow, ratio: float | None,
                   profile: tuple[tuple[float, float], ...]) -> list[list[tuple[datetime, float]]]:
    """What a start at each hour ahead would save, today's hours then tomorrow's.

    Today's are scaled by what the panels give now (else the learned bias),
    tomorrow's by the bias; the house's learned draw is taken off each hour.
    """
    if not sky.curve:
        return []
    midnight = _end_of_day(at)
    days = ((at.replace(minute=0, second=0, microsecond=0), midnight,
             ratio if ratio is not None else sky.bias),
            (midnight, midnight + timedelta(days=1), sky.bias))
    out = []
    for start, end, scale in days:
        hourly = Sky(sky.curve, scale, sky.house_w)
        out.append([(hour, estimate(profile, max(0.0, hourly.net_w(hour)), None, None).saving)
                    for hour, _ in hours_between(sky.curve, start, end)])
    return out


def best_hour(at: datetime, sky: ForecastNow, ratio: float | None,
              profile: tuple[tuple[float, float], ...],
              enough: float) -> tuple[datetime, float] | None:
    """The hour ahead whose expected surplus saves the most, with that saving.

    Tomorrow's hours count only when today offers less than ``enough``. The
    earliest of equal hours wins.
    """
    best: tuple[datetime, float] | None = None
    for hours in hourly_savings(at, sky, ratio, profile):
        for hour, saving in hours:
            if best is None or saving > best[1]:
                best = (hour, saving)
        if best is not None and best[1] >= enough:
            break
    return best


def _next_minute(at: datetime) -> datetime:
    """``at`` rounded up to the minute: an hour already begun reads « now »
    without changing on every refresh, which the recorder would keep."""
    floor = at.replace(second=0, microsecond=0)
    return floor if floor == at else floor + timedelta(minutes=1)


def first_hour(at: datetime, sky: ForecastNow, ratio: float | None,
               profile: tuple[tuple[float, float], ...],
               bar: float) -> tuple[datetime, float] | None:
    """The first hour ahead expected to save at least ``bar``."""
    for hours in hourly_savings(at, sky, ratio, profile):
        for hour, saving in hours:
            if saving >= bar:
                return hour, saving
    return None


class SolarStarter:
    """Says when to press "start", from the grid meter's recent past."""

    def __init__(self, config: SolarStartConfig | None = None) -> None:
        self.config = config or SolarStartConfig()
        self._trace = Trace(keep=self.config.window + timedelta(minutes=1))
        self._pv = Trace(keep=self.config.window + timedelta(minutes=1))
        self._asked_at: datetime | None = None
        self._woken_at: datetime | None = None
        self._out_of_reach_since: datetime | None = None
        # The best hour once settled on: a later forecast never pushes it back.
        self._target: datetime | None = None
        self._target_saving: float = 0.0
        # Asked by hand: start at once, whatever the sun, until disarmed.
        self._forced = False
        self.outlook: Outlook | None = None

    def force(self) -> None:
        """Start the armed washer at the next decision, without waiting for the sun."""
        self._forced = True

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

    def _heating_out_of_reach(self, at: datetime, total: timedelta | None,
                              sky: ForecastNow | None) -> bool:
        """True when no hour left today is forecast to cover the heating.

        Holding out for it then only loses the sun there is: on 30 Oct 2026,
        armed at 13:02 under a 2 kW peak, it waited until 15:00 for 90 % while
        the best hour could give 87 %. Without a curve, nothing can be said.
        """
        if sky is None or not sky.curve:
            return False
        ratio = realism(self._median(self._pv, at), sky.now_w, sky.raining)
        today = hourly_savings(at, sky, ratio, profile_for(total))[0]
        return all(saving < self.config.good_saving for _, saving in today)

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

    def bar_w(self, total: timedelta | None) -> float:
        """The least held export that reaches the lower bar, for this programme."""
        profile, low, high = profile_for(total), 0.0, 5000.0
        for _ in range(20):
            middle = (low + high) / 2
            if estimate(profile, middle, None, None).saving >= self.config.min_saving:
                high = middle
            else:
                low = middle
        return high

    def _median(self, trace: Trace, at: datetime) -> float | None:
        if not self._observed(trace, at):
            return None
        return trace.quantile(at - self.config.window, at, 0.5)

    def _sun_out_of_reach(self, at: datetime, total: timedelta | None,
                          sky: ForecastNow | None) -> bool:
        """True once the lower bar has been out of reach for a whole window."""
        grid = self._median(self._trace, at)
        pv = self._median(self._pv, at)
        self.outlook = outlook(at, sky, pv) if sky is not None else None
        best = self.outlook.best_pv_w if self.outlook else None
        if grid is None or best is None or (pv is None and not self.outlook.production_over):
            self._out_of_reach_since = None
            return False
        house_w = (pv or 0.0) + grid
        if best - house_w >= self.bar_w(total):
            self._out_of_reach_since = None
            return False
        if self._out_of_reach_since is None:
            self._out_of_reach_since = at
        return at - self._out_of_reach_since >= self.config.window

    def _best_hour(self, at: datetime, total: timedelta | None,
                   sky: ForecastNow | None, saving_now: float) -> tuple[datetime, float] | None:
        """The hour to hold on for, or None to start now.

        Once settled on, the hour stands until it comes: a passing cloud, or
        a forecast revised later, does not make it start now nor wait longer.
        Only an earlier hour found worth it brings it forward.
        """
        cfg = self.config
        if sky is not None and (self._target is None or at < self._target):
            ratio = self.outlook.ratio if self.outlook else None
            found = best_hour(at, sky, ratio, profile_for(total), cfg.min_target_saving)
            worth = max(cfg.min_target_saving, saving_now + cfg.target_gain)
            if (found is not None and found[0] > at and found[1] >= worth
                    and (self._target is None or found[0] <= self._target)):
                self._target, self._target_saving = found
        if self._target is None or at >= self._target:
            return None
        return self._target, self._target_saving

    def _expected(self, at: datetime, total: timedelta | None,
                  sky: ForecastNow | None, required: float) -> tuple[datetime, float] | None:
        """When the forecast says the start will come: the first hour that
        meets the bar now held to, else the lower one, else the best hour."""
        if sky is None:
            return None
        ratio = self.outlook.ratio if self.outlook else None
        profile = profile_for(total)
        found = (first_hour(at, sky, ratio, profile, required)
                 or first_hour(at, sky, ratio, profile, self.config.min_saving)
                 or best_hour(at, sky, ratio, profile, self.config.min_target_saving))
        if found is None or found[1] <= 0:
            return None
        return max(found[0], _next_minute(at)), found[1]

    def decide(self, at: datetime, *, armed: bool | None,
               washer_running: bool | None, enabled: bool,
               asleep: bool = False, total: timedelta | None = None, price: float | None = None,
               forecast: Forecast | None = None,
               peak_at: datetime | None = None,
               sky: ForecastNow | None = None,
               expensive_now: bool = False,
               program_ready: bool = True) -> Decision:
        """Whether to start now — True once per attempt.

        Every condition has to be *known*: a remote-start flag or a washer
        status that ThinQ cannot report is no permission to start a machine,
        and neither is a programme the starter does not hold
        (``program_ready``, see :mod:`logic.program_memory`).
        """
        if not (enabled and armed is True and washer_running is False):
            self._asked_at = self._woken_at = self._out_of_reach_since = None
            self._target = None
            self._forced = False
            return Decision(waiting=False, estimate=None)
        surplus = self.held_export_w(at)
        if self._forced:
            forced = estimate(profile_for(total), surplus, forecast, price) \
                if surplus is not None else None
            return self._go(at, Decision(waiting=True, estimate=forced, reason="départ forcé"),
                            asleep=asleep, program_ready=program_ready)
        if surplus is None:
            expected = self._expected(at, total, sky, self.config.min_saving)
            return Decision(waiting=True, estimate=None,
                            expected_at=expected[0] if expected else None,
                            expected_saving=expected[1] if expected else None)
        result = estimate(profile_for(total), surplus, forecast, price)
        required, reason = self._required(at, peak_at)
        if required >= self.config.good_saving and self._heating_out_of_reach(at, total, sky):
            required, reason = self.config.min_saving, "chauffe couverte hors d'atteinte aujourd'hui"
        if result.saving >= self.config.good_saving:
            required, reason = self.config.good_saving, "chauffe couverte"
        decision = Decision(waiting=True, estimate=result,
                            required=required, reason=reason)
        if not program_ready:
            # Said first: no sun can start it, the wait is not for the sun.
            return replace(decision, reason="programme non mémorisé")
        if result.saving < required:
            if not self._sun_out_of_reach(at, total, sky):
                expected = self._expected(at, total, sky, required)
                if expected is None:
                    return decision
                return replace(decision, expected_at=expected[0], expected_saving=expected[1])
            target = self._best_hour(at, total, sky, result.saving)
            if target is not None:
                day = "demain " if target[0].date() > at.date() else ""
                return replace(decision, target_at=target[0], target_saving=target[1],
                               expected_at=target[0], expected_saving=target[1],
                               reason=f"meilleure heure prévue : {day}{target[0]:%H:%M}")
            reached = self._target is not None and at >= self._target
            if not reached and self.outlook is not None \
                    and self.outlook.production_over and self.outlook.tomorrow_good:
                # Without an hourly curve, only the day's total says tomorrow is worth it.
                return replace(decision, reason="soleil de demain attendu")
            if expensive_now:
                return replace(decision, reason="soleil hors d'atteinte : heures creuses attendues")
            decision = replace(decision, reason=(
                "meilleure heure du jour" if reached else "soleil hors d'atteinte aujourd'hui"))
        return self._go(at, decision, asleep=asleep, program_ready=program_ready)

    def _go(self, at: datetime, decision: Decision, *, asleep: bool,
            program_ready: bool) -> Decision:
        """Wake or start, once per attempt — unless the programme is not held."""
        if not program_ready:
            # Started anyway, it would run the default programme.
            return replace(decision, reason="programme non mémorisé")
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
    "best_hour",
    "first_hour",
    "hourly_savings",
    "estimate",
    "profile_for",
]
