"""Appliance cycle detection from a power trace.

Pure logic: no Home Assistant import, no I/O, no clock of its own — every
decision is driven by the ``(timestamp, watts)`` samples fed in. That is what
makes it testable against the real traces recorded from the meters
(``tests/fixtures/``).

Two detectors, because two very different situations:

* :class:`SharedMeterDetector` — washer and dryer behind one Shelly. A cycle is
  armed on a modest rise, *confirmed* only by a heating burst, then attributed
  to one appliance or the other by how much power is still being drawn a
  quarter of an hour in.
* :class:`PlateauDetector` — the dishwasher, which no meter sees. It is read
  from the house's unmeasured load, where the oven also lives. The tell is not
  the level (a base load of a few hundred watts shifts it) but the *step* above
  the ambient baseline, and how flat that step stays.

Thresholds are not invented: see ``README.md`` for the traces they come from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .samples import Trace, seconds_above


class Phase(str, Enum):
    """What a watcher believes the appliance is doing."""

    IDLE = "veille"
    RUNNING = "en_cours"
    FINISHED = "termine"


@dataclass(frozen=True)
class Transition:
    """Something worth telling the outside world about."""

    kind: str  # "started" | "identified" | "peer" | "finished"
    at: datetime
    appliance: str
    started_at: datetime | None = None
    duration_minutes: float | None = None
    energy_wh: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class SharedMeterConfig:
    """Two appliances, one meter — washer and dryer in the garage.

    Defaults come from six days of recorded cycles, plus twenty months of
    logged oddities on this particular circuit — which also carries a freezer:

    * standby sits at 30 W with brief 75 W blips, and five episodes of ~180 W
      lasting a quarter of an hour would fool any plain "above 100 W" rule;
    * the freezer compressor draws ~120 W and **surges to 1.5-2 kW on start**,
      which would confirm a cycle on its own — so the confirmation burst has to
      be *held*, not merely touched;
    * the same compressor is why idling is judged well above 120 W: a cycle
      that treated the freezer as activity would never end.
    """

    burst: str = "seche_linge"  # sustained heating -> dryer
    steady: str = "lave_linge"  # heats once, then runs cool -> washer
    arm_w: float = 100.0
    confirm_w: float = 1500.0
    # Held, not touched: a freezer's start-up surge is over in a second, a
    # heating element runs for minutes. Measured over twelve days, every one of
    # the sixteen genuine excursions past confirm_w lasted at least 129 s and
    # none lasted under it — so twenty seconds clears a real cycle six times
    # over while still asking for twenty times an inrush. It also keeps a brief
    # manual test detectable, which a minute did not.
    confirm_hold: timedelta = timedelta(seconds=20)
    confirm_within: timedelta = timedelta(minutes=10)
    classify_at: timedelta = timedelta(minutes=20)
    classify_from: timedelta = timedelta(minutes=15)
    classify_w: float = 800.0  # measured: washer 144 W, dryer 1035-1983 W
    # The washer's drum runs at 142-215 W and the freezer's compressor at a
    # similar level, so no threshold separates them: this one is set low, where
    # the traces put it, and the consequence is accepted — a compressor running
    # when the laundry finishes delays "terminé" by up to one compressor run.
    # The countdown has already reached zero by then, so nothing is misstated.
    idle_w: float = 100.0
    # A cycle stays alive on *sustained* draw only: single-minute blips of
    # 180-210 W keep appearing on this meter, and they would otherwise hold a
    # finished cycle open for another ten minutes each time.
    active_window: timedelta = timedelta(minutes=2)
    off_delay: timedelta = timedelta(minutes=10)
    nominal: timedelta = timedelta(minutes=65)
    # A second appliance joining a cycle already under way.
    # Washer running: its long programme heats up to three times, the last one
    # for ten minutes at 2 200 W. Averaged over twenty minutes that still only
    # reaches ~1 200 W, where a dryer running alongside holds past 2 000 W.
    peer_burst_w: float = 1600.0
    peer_burst_window: timedelta = timedelta(minutes=20)
    # Dryer running: it idles between heats around 200 W, so a trough sitting
    # between these two bounds is a second machine drawing underneath it. Above
    # the upper bound there is simply no trough in the window and nothing can be
    # concluded — which is the honest answer, not a peer.
    peer_trough_min_w: float = 300.0
    peer_trough_max_w: float = 800.0
    peer_trough_window: timedelta = timedelta(minutes=20)
    # A duration quantile rather than the plain minimum: the single sample
    # caught while a heating element ramps down is not a trough.
    peer_trough_quantile: float = 0.1


@dataclass(frozen=True)
class PlateauConfig:
    """One appliance read from the *unmeasured* house load — the dishwasher.

    The dishwasher steps 1 940-2 015 W above the ambient base and holds it flat
    for 4 to 14 minutes; the oven steps 2 150-2 230 W. The band below keeps a
    ~70 W margin on each side of that gap. Working on the step rather than the
    absolute level is what makes the rule survive a noisy base load: an unusual
    base produces a *missed* cycle, never a false one.
    """

    appliance: str = "lave_vaisselle"
    step_min_w: float = 1850.0
    step_max_w: float = 2080.0
    hold: timedelta = timedelta(minutes=4)
    spread_max_w: float = 300.0
    baseline_window: timedelta = timedelta(minutes=30)
    baseline_quantile: float = 0.2
    # Silence cannot mean "finished" here: between two heating phases this
    # dishwasher goes 42 minutes without drawing anything visible, and the
    # final dry draws nothing at all. So the expected duration carries the
    # cycle, a late heating extends it, and `quiet_after_end` is only the pause
    # observed before declaring it over.
    nominal: timedelta = timedelta(minutes=63)
    quiet_after_end: timedelta = timedelta(minutes=8)
    # How far back a confirmed plateau may reach to find the cycle's true first
    # heating: longer than the choppy opening burst, shorter than the 42-minute
    # lull that sits in the middle of a cycle.
    backdate_max_gap: timedelta = timedelta(minutes=25)
    max_duration: timedelta = timedelta(hours=3)


@dataclass
class Cycle:
    """A cycle in flight."""

    started_at: datetime
    appliance: str | None = None
    peer: str | None = None
    energy_wh: float = 0.0
    last_active: datetime = field(default=datetime.min)


class SharedMeterDetector:
    """Washer/dryer cycles on a single meter."""

    def __init__(self, config: SharedMeterConfig | None = None) -> None:
        self.config = config or SharedMeterConfig()
        self._trace = Trace(keep=timedelta(minutes=35))
        self._armed_at: datetime | None = None
        self._cycle: Cycle | None = None
        self._previous: tuple[datetime, float] | None = None

    @property
    def phase(self) -> Phase:
        return Phase.RUNNING if self._cycle else Phase.IDLE

    @property
    def cycle(self) -> Cycle | None:
        return self._cycle

    def feed(self, at: datetime, watts: float) -> list[Transition]:
        """Push one reading; return whatever it changed."""
        self._accumulate(at, watts)
        self._trace.add(at, watts)
        self._previous = (at, watts)
        if self._cycle is None:
            return self._while_idle(at, watts)
        return self._while_running(at, watts)

    def tick(self, at: datetime) -> list[Transition]:
        """Re-decide on the clock alone, holding the last reading.

        A meter reporting on change falls silent exactly when a cycle ends, so
        waiting for the next reading would postpone the end for ever. Silence
        means "unchanged", so the last value is carried forward — including
        into the energy total.
        """
        if self._cycle is None or self._previous is None:
            return []
        watts = self._previous[1]
        self._accumulate(at, watts)
        self._previous = (at, watts)
        return self._while_running(at, watts)

    def _accumulate(self, at: datetime, watts: float) -> None:
        """Integrate the *previous* reading up to now — power held until changed."""
        if self._cycle is None or self._previous is None:
            return
        hours = (at - self._previous[0]).total_seconds() / 3600
        self._cycle.energy_wh += self._previous[1] * hours

    def _while_idle(self, at: datetime, watts: float) -> list[Transition]:
        cfg = self.config
        if watts < cfg.arm_w:
            self._armed_at = None
            return []
        if self._armed_at is None:
            self._armed_at = at
        if watts >= cfg.confirm_w and self._burst_is_held(at):
            return [self._start(at)]
        if at - self._armed_at > cfg.confirm_within:
            self._armed_at = None  # a 180 W blip that never heated: not a cycle
        return []

    def _burst_is_held(self, at: datetime) -> bool:
        """True when the draw has *stayed* high, not merely spiked.

        The freezer sharing this circuit surges on every compressor start;
        averaged over the window that is worth a fraction of the threshold.

        The window must also be genuinely *observed*. A reading holds until the
        next one, so a single sample would otherwise fill the whole window on
        its own and confirm instantly — which is what happened when Home
        Assistant restarted in the middle of a burst: the integration came up,
        read 2 071 W once, and opened a cycle it had watched for no time at
        all, dated from that moment instead of from the real rise.
        """
        window_start = at - self.config.confirm_hold
        oldest = self._trace.samples[0][0] if self._trace.samples else at
        if oldest > window_start:
            return False  # not watched long enough to claim anything was held
        mean = self._trace.mean(window_start, at)
        return mean is not None and mean >= self.config.confirm_w

    def _start(self, at: datetime) -> Transition:
        """Open a cycle, dated back to when the rise began, not to the burst."""
        started_at = self._armed_at or at
        self._cycle = Cycle(started_at=started_at, last_active=at)
        self._armed_at = None
        return Transition(kind="started", at=at, appliance="", started_at=started_at,
                          reason="montée confirmée par une chauffe")

    def _while_running(self, at: datetime, watts: float) -> list[Transition]:
        cycle = self._cycle
        assert cycle is not None
        sustained = self._trace.mean(at - self.config.active_window, at)
        if sustained is not None and sustained >= self.config.idle_w:
            cycle.last_active = max(cycle.last_active, at)

        out: list[Transition] = []
        if cycle.appliance is None:
            out += self._classify(at, cycle)
        elif cycle.peer is None:
            out += self._look_for_peer(at, cycle)
        out += self._maybe_finish(at, cycle)
        return out

    def _classify(self, at: datetime, cycle: Cycle) -> list[Transition]:
        """Name the appliance on how hot it still runs a quarter of an hour in."""
        cfg = self.config
        if at - cycle.started_at < cfg.classify_at:
            return []
        window_start = cycle.started_at + cfg.classify_from
        mean = self._trace.mean(window_start, at)
        if mean is None:
            return []
        cycle.appliance = cfg.burst if mean > cfg.classify_w else cfg.steady
        return [Transition(kind="identified", at=at, appliance=cycle.appliance,
                           started_at=cycle.started_at,
                           reason=f"{mean:.0f} W en moyenne entre T+15 et T+20")]

    def _look_for_peer(self, at: datetime, cycle: Cycle) -> list[Transition]:
        """Spot the *other* appliance joining a cycle already under way.

        Each one is caught by what the running appliance cannot do on its own:
        a washer past its heating burst never sustains 1.2 kW, and a dryer's
        troughs never sit as high as 350 W.
        """
        cfg = self.config
        if at - cycle.started_at < cfg.classify_at:
            return []
        if cycle.appliance == cfg.steady:
            level = self._trace.mean(at - cfg.peer_burst_window, at)
            joined = level is not None and level > cfg.peer_burst_w
            evidence = f"{level:.0f} W soutenus" if level is not None else ""
        else:
            level = self._trace.quantile(at - cfg.peer_trough_window, at,
                                         cfg.peer_trough_quantile)
            joined = (level is not None
                      and cfg.peer_trough_min_w < level < cfg.peer_trough_max_w)
            evidence = f"creux à {level:.0f} W" if level is not None else ""
        if not joined:
            return []
        cycle.peer = cfg.burst if cycle.appliance == cfg.steady else cfg.steady
        return [Transition(kind="peer", at=at, appliance=cycle.peer,
                           started_at=cycle.started_at,
                           reason=f"second appareil détecté ({evidence})")]

    def _maybe_finish(self, at: datetime, cycle: Cycle) -> list[Transition]:
        if at - cycle.last_active < self.config.off_delay:
            return []
        ended = cycle.last_active
        self._cycle = None
        self._trace.clear()
        return [Transition(kind="finished", at=ended,
                           appliance=cycle.appliance or self.config.steady,
                           started_at=cycle.started_at,
                           duration_minutes=(ended - cycle.started_at).total_seconds() / 60,
                           energy_wh=round(cycle.energy_wh, 1),
                           reason="puissance retombée en veille")]


class PlateauDetector:
    """A cycle read from the unmeasured house load, by its heating plateaus."""

    def __init__(self, config: PlateauConfig | None = None) -> None:
        self.config = config or PlateauConfig()
        self._trace = Trace(keep=timedelta(minutes=45))
        self._candidate_at: datetime | None = None
        self._cycle: Cycle | None = None
        self._previous: tuple[datetime, float] | None = None
        self._plateau_seen_at: datetime | None = None
        self._baseline_w: float | None = None

    @property
    def phase(self) -> Phase:
        return Phase.RUNNING if self._cycle else Phase.IDLE

    @property
    def cycle(self) -> Cycle | None:
        return self._cycle

    @property
    def baseline_w(self) -> float | None:
        """Ambient unmeasured load, as last computed — what the step sits on."""
        return self._baseline_w

    def feed(self, at: datetime, watts: float) -> list[Transition]:
        self._accumulate(at, watts)
        baseline = self._baseline(at)
        self._baseline_w = baseline
        self._trace.add(at, watts)
        self._previous = (at, watts)
        if baseline is None:
            return []

        in_plateau = self._track_candidate(at, watts, baseline)
        if in_plateau:
            self._plateau_seen_at = at
            if self._cycle is None:
                return [self._start(baseline)]
        if self._cycle is not None:
            return self._maybe_finish(at)
        return []

    def _accumulate(self, at: datetime, watts: float) -> None:
        if self._cycle is None or self._previous is None:
            return
        hours = (at - self._previous[0]).total_seconds() / 3600
        self._cycle.energy_wh += self._previous[1] * hours

    def _baseline(self, at: datetime) -> float | None:
        """Ambient unmeasured load: the level the house sits at most of the time.

        A duration quantile, so a plateau occupying a third of the window does
        not drag the reference up with it.
        """
        cfg = self.config
        return self._trace.quantile(at - cfg.baseline_window, at, cfg.baseline_quantile)

    def _track_candidate(self, at: datetime, watts: float, baseline: float) -> bool:
        """True once a step has stayed inside the band, and flat, long enough."""
        cfg = self.config
        step = watts - baseline
        if not cfg.step_min_w <= step <= cfg.step_max_w:
            # A brief dip is the pump between two rinses, not the end of a
            # plateau — but anything outside the band breaks the candidate.
            self._candidate_at = None
            return False
        if self._candidate_at is None:
            self._candidate_at = at
            return False
        if at - self._candidate_at < cfg.hold:
            return False
        spread = self._trace.spread(self._candidate_at, at)
        return spread is not None and spread <= cfg.spread_max_w

    def _start(self, baseline: float) -> Transition:
        confirmed_at = self._candidate_at or self._plateau_seen_at
        assert confirmed_at is not None
        started_at = self._earliest_excursion(confirmed_at, baseline)
        self._cycle = Cycle(started_at=started_at,
                            appliance=self.config.appliance,
                            last_active=started_at)
        return Transition(kind="started", at=confirmed_at, appliance=self.config.appliance,
                          started_at=started_at,
                          reason="palier de chauffe dans la bande du lave-vaisselle")

    def _earliest_excursion(self, confirmed_at: datetime, baseline: float) -> datetime:
        """Walk back to the *first* heating of this cycle, not the one we proved.

        The opening burst alternates heating and pumping — on one recorded cycle
        it never held four unbroken minutes, and the plateau that proved the
        cycle came twenty minutes later. Dating the cycle from there would make
        the whole countdown twenty minutes wrong, so the trace is re-read
        backwards, hopping over gaps shorter than a full quiet period.
        """
        cfg = self.config
        earliest = confirmed_at
        for stamp, watts in reversed(self._trace.samples):
            if stamp > confirmed_at:
                continue
            if not cfg.step_min_w <= watts - baseline <= cfg.step_max_w:
                continue
            if earliest - stamp > cfg.backdate_max_gap:
                break  # too long a silence: that was a different cycle
            earliest = stamp
        return earliest

    def _maybe_finish(self, at: datetime) -> list[Transition]:
        cycle = self._cycle
        assert cycle is not None
        cfg = self.config
        overdue = at - cycle.started_at > cfg.max_duration
        last_plateau = self._plateau_seen_at or cycle.started_at
        # The cycle runs at least its expected length; a heating that lands
        # past that pushes the end out to it.
        ended_at = max(cycle.started_at + cfg.nominal, last_plateau)
        if at < ended_at + cfg.quiet_after_end and not overdue:
            return []
        if overdue:
            ended_at = at
        self._cycle = None
        self._candidate_at = None
        self._plateau_seen_at = None
        return [Transition(kind="finished", at=ended_at, appliance=cycle.appliance or "",
                           started_at=cycle.started_at,
                           duration_minutes=(ended_at - cycle.started_at).total_seconds() / 60,
                           energy_wh=round(cycle.energy_wh, 1),
                           reason="durée attendue écoulée, plus de chauffe" if not overdue
                           else "durée maximale atteinte")]

    def tick(self, at: datetime) -> list[Transition]:
        """Re-decide on the clock alone — see :meth:`SharedMeterDetector.tick`."""
        if self._cycle is None or self._previous is None:
            return []
        watts = self._previous[1]
        self._accumulate(at, watts)
        self._previous = (at, watts)
        return self._maybe_finish(at)


def elapsed_ratio(started_at: datetime, now: datetime, nominal: timedelta) -> float:
    """Progress in percent, capped at 99 until the cycle actually ends."""
    if nominal.total_seconds() <= 0:
        return 0.0
    ratio = (now - started_at).total_seconds() / nominal.total_seconds()
    return round(min(max(ratio, 0.0), 0.99) * 100, 0)


def remaining_minutes(started_at: datetime, now: datetime,
                      nominal: timedelta) -> float:
    """Minutes left against the expected duration; never negative."""
    left = nominal - (now - started_at)
    return max(0.0, round(left.total_seconds() / 60, 0))


__all__ = [
    "Cycle",
    "Phase",
    "PlateauConfig",
    "PlateauDetector",
    "SharedMeterConfig",
    "SharedMeterDetector",
    "Transition",
    "elapsed_ratio",
    "remaining_minutes",
    "seconds_above",
]
