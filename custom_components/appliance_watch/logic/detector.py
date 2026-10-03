"""Appliance cycle detection from a power trace.

Pure logic: no Home Assistant import, no I/O, no clock of its own — every
decision is driven by the ``(timestamp, watts)`` samples fed in. That is what
makes it testable against the real traces recorded from the meters
(``tests/fixtures/``).

Two detectors, because two very different situations:

* :class:`DryerDetector` — the dryer, behind the Shelly it shares with the
  washer. The washer reports itself through LG ThinQ (:mod:`.washer`), so a
  cycle here is armed on a modest rise, *confirmed* by a held heating burst,
  and is the dryer's whenever the washer is known to be idle.
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

from .samples import Trace


class Phase(str, Enum):
    """What a watcher believes the appliance is doing."""

    IDLE = "veille"
    RUNNING = "en_cours"
    FINISHED = "termine"


@dataclass(frozen=True)
class Transition:
    """Something worth telling the outside world about."""

    kind: str  # "started" | "finished" | "cancelled"
    at: datetime
    appliance: str
    started_at: datetime | None = None
    duration_minutes: float | None = None
    energy_wh: float | None = None
    # Longest silence *inside* the cycle — how the machine's rhythm is learned.
    longest_pause_s: float | None = None
    heats: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class DryerConfig:
    """The dryer, read off the laundry meter it shares with the washer.

    The washer reports itself through LG ThinQ, so the meter no longer has to
    say *which* machine is running — only whether the dryer is. Defaults come
    from the recorded cycles, plus twenty months of logged oddities on this
    circuit — which also carries a freezer:

    * standby sits at 30 W with brief 75 W blips, and five episodes of ~180 W
      lasting a quarter of an hour would fool any plain "above 100 W" rule;
    * the freezer compressor draws ~120 W and **surges to 1.5-2 kW on start**,
      which would confirm a cycle on its own — so the confirmation burst has to
      be *held*, not merely touched.
    """

    appliance: str = "seche_linge"
    arm_w: float = 100.0
    confirm_w: float = 1500.0
    # Held, not touched: a freezer's start-up surge is over in a second, a
    # heating element runs for minutes. Over twelve days, the sixteen genuine
    # excursions past confirm_w lasted 129 s or more, and the shortest since —
    # the new washer's opening burst on 3 Oct — 91 s. Twenty seconds clears a
    # real one several times over while asking for twenty times an inrush,
    # and keeps a brief manual test detectable, which a minute did not.
    confirm_hold: timedelta = timedelta(seconds=20)
    confirm_within: timedelta = timedelta(minutes=10)
    # ThinQ reports the washer a little after the meter sees it: on 3 Oct the
    # meter rose at 17:18:54 for a start the machine dates 17:21:13. A dryer
    # cycle opened that close to a washer's start was the washer.
    washer_lag: timedelta = timedelta(minutes=5)
    # The drum turning, as opposed to standby. Kept above the freezer's 120 W
    # compressor would be ideal, but the dryer tumbles around 200 W between
    # heats with dips well below, so it sits where the traces put it — and a
    # compressor running when the dryer finishes delays "terminé" by one run.
    idle_w: float = 100.0
    # Sustained draw only: single-minute blips of 180-210 W keep appearing on
    # this meter and would otherwise hold a finished cycle open.
    active_window: timedelta = timedelta(seconds=30)
    # The dryer is the machine stopped part-way through, so it closes quickly —
    # but not after one minute: measured mid-cycle silences under the idle
    # threshold run to 300 s, and a one-minute rule would cut the activity ten
    # minutes early, then open a second one when the drum picked up again. Six
    # minutes clears the longest seen; a learned rhythm tightens it.
    off_delay: timedelta = timedelta(minutes=6)
    off_delay_floor: timedelta = timedelta(minutes=2)
    # With the washer on the meter too, standby never comes: only the dryer's
    # element proves it is still running. It reheats every 8.2 min (measured);
    # the washer, once its water is hot, never draws this much again.
    heat_w: float = 1000.0
    shared_off_delay: timedelta = timedelta(minutes=13)
    nominal: timedelta = timedelta(minutes=60)


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
    # Two heatings are 13 to 16 minutes apart, while a single one dips for a
    # minute at a time as the pump runs. Anything closer than this is the same
    # heating breathing, not a new one.
    heat_separation: timedelta = timedelta(minutes=5)
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
    energy_wh: float = 0.0
    last_active: datetime = field(default=datetime.min)
    # Silences that *ended* — the machine's own rhythm between heats. The one
    # that never ends is the cycle finishing, and is not counted here.
    pauses_s: list[float] = field(default_factory=list)
    quiet_since: datetime | None = None
    # Heating phases seen so far. For a dishwasher this says far more about how
    # far along it is than a percentage of a nominal hour: the machine heats
    # for the wash, then for each rinse.
    heats: int = 0
    heating_since: datetime | None = None
    last_heat_end: datetime | None = None

    @property
    def longest_pause_s(self) -> float:
        return max(self.pauses_s) if self.pauses_s else 0.0


class DryerDetector:
    """Dryer cycles on the laundry meter, told apart from the washer by ThinQ.

    Every call says whether the washer is running: True, False, or None when
    ThinQ has no answer. Only a washer *known* to be idle lets a cycle open —
    a missing answer is not "off", and guessing it was is how the washer would
    be named a dryer again.
    """

    def __init__(self, config: DryerConfig | None = None) -> None:
        self.config = config or DryerConfig()
        self._trace = Trace(keep=timedelta(minutes=15))
        self._armed_at: datetime | None = None
        self._cycle: Cycle | None = None
        self._previous: tuple[datetime, float] | None = None
        self.learned_pause_s: float = 0.0

    @property
    def phase(self) -> Phase:
        return Phase.RUNNING if self._cycle else Phase.IDLE

    @property
    def cycle(self) -> Cycle | None:
        return self._cycle

    def feed(self, at: datetime, watts: float, *,
             washer: bool | None) -> list[Transition]:
        """Push one reading; return whatever it changed."""
        self._accumulate(at, watts)
        self._trace.add(at, watts)
        self._previous = (at, watts)
        if self._cycle is None:
            return self._while_idle(at, watts, washer)
        return self._while_running(at, washer)

    def tick(self, at: datetime, *, washer: bool | None) -> list[Transition]:
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
        return self._while_running(at, washer)

    def washer_started(self, started_at: datetime) -> list[Transition]:
        """Withdraw a cycle that was the washer all along.

        The meter can see the washer's opening burst before ThinQ reports it.
        A cycle opened within ``washer_lag`` of the washer's own start was that
        burst; one opened earlier is a dryer the washer has joined.
        """
        cycle = self._cycle
        if cycle is None or cycle.started_at < started_at - self.config.washer_lag:
            return []
        self._cycle = None
        return [Transition(kind="cancelled", at=started_at,
                           appliance=self.config.appliance,
                           started_at=cycle.started_at,
                           reason="c'était le lave-linge, signalé par ThinQ")]

    def _accumulate(self, at: datetime, watts: float) -> None:
        """Integrate the *previous* reading up to now — power held until changed."""
        if self._cycle is None or self._previous is None:
            return
        hours = (at - self._previous[0]).total_seconds() / 3600
        self._cycle.energy_wh += self._previous[1] * hours

    def _while_idle(self, at: datetime, watts: float,
                    washer: bool | None) -> list[Transition]:
        cfg = self.config
        if washer is not False or watts < cfg.arm_w:
            self._armed_at = None  # the meter is the washer's, or quiet
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
        self._cycle = Cycle(started_at=started_at, appliance=self.config.appliance,
                            last_active=at)
        self._armed_at = None
        return Transition(kind="started", at=at, appliance=self.config.appliance,
                          started_at=started_at,
                          reason="chauffe tenue, lave-linge à l'arrêt")

    def _while_running(self, at: datetime, washer: bool | None) -> list[Transition]:
        cycle = self._cycle
        assert cycle is not None
        if washer:
            return self._while_shared(at, cycle)
        sustained = self._trace.mean(at - self.config.active_window, at)
        if sustained is not None and sustained >= self.config.idle_w:
            if cycle.quiet_since is not None:
                # A silence that ended: that is a pause in the machine's
                # rhythm, not the end of the cycle.
                cycle.pauses_s.append((at - cycle.quiet_since).total_seconds())
                cycle.quiet_since = None
            cycle.last_active = max(cycle.last_active, at)
        elif cycle.quiet_since is None:
            cycle.quiet_since = at
        return self._maybe_finish(at, cycle, self._off_delay())

    def _while_shared(self, at: datetime, cycle: Cycle) -> list[Transition]:
        """The washer is drawing too: only the dryer's heating keeps it alive.

        Its tumbling and the washer's drum look alike, so the end is read from
        the last heat, which drops the dryer's final cool-down from the count.
        A silence measured across two machines teaches nothing of either, so
        none is recorded.
        """
        cycle.quiet_since = None
        heat = self._trace.mean(at - self.config.active_window, at)
        if heat is not None and heat >= self.config.heat_w:
            cycle.last_active = max(cycle.last_active, at)
        return self._maybe_finish(at, cycle, self.config.shared_off_delay)

    def _off_delay(self) -> timedelta:
        """How long a silence has to last before the cycle is called over.

        Twice the longest pause ever seen between two heats, never below two
        minutes nor above the configured ceiling. Until a cycle has been
        watched end to end, the ceiling stands.
        """
        ceiling = self.config.off_delay
        if self.learned_pause_s <= 0:
            return ceiling
        learned = timedelta(seconds=self.learned_pause_s * 2)
        return max(self.config.off_delay_floor, min(ceiling, learned))

    def _maybe_finish(self, at: datetime, cycle: Cycle,
                      off_delay: timedelta) -> list[Transition]:
        if at - cycle.last_active < off_delay:
            return []
        ended = cycle.last_active
        self._cycle = None
        self._trace.clear()
        return [Transition(kind="finished", at=ended, appliance=self.config.appliance,
                           started_at=cycle.started_at,
                           duration_minutes=(ended - cycle.started_at).total_seconds() / 60,
                           energy_wh=round(cycle.energy_wh, 1),
                           longest_pause_s=round(cycle.longest_pause_s),
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
        self._heating = False

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

    @property
    def heating(self) -> bool:
        """True while a heating plateau is under way."""
        return self._heating

    @property
    def heats(self) -> int:
        return self._cycle.heats if self._cycle else 0

    def feed(self, at: datetime, watts: float) -> list[Transition]:
        self._accumulate(at, watts)
        baseline = self._baseline(at)
        self._baseline_w = baseline
        self._trace.add(at, watts)
        self._previous = (at, watts)
        if baseline is None:
            return []

        out: list[Transition] = []
        in_plateau = self._track_candidate(at, watts, baseline)
        if in_plateau:
            self._plateau_seen_at = at
            if self._cycle is None:
                out.append(self._start(baseline))
        self._track_heating(at, in_plateau)
        if out:
            return out
        if self._cycle is not None:
            return self._maybe_finish(at)
        return []

    def _track_heating(self, at: datetime, in_plateau: bool) -> None:
        """Count heating phases, ignoring the dips inside one."""
        cycle = self._cycle
        if cycle is None:
            self._heating = False
            return
        if in_plateau:
            if not self._heating:
                separated = (cycle.last_heat_end is None
                             or at - cycle.last_heat_end >= self.config.heat_separation)
                if separated:
                    cycle.heats += 1
                cycle.heating_since = at
            self._heating = True
            cycle.last_heat_end = at
        elif self._heating and at - (cycle.last_heat_end or at) >= timedelta(minutes=1):
            # A minute without a plateau: the pump is running, the heating is
            # over for now. Shorter than that and it is the same one breathing.
            self._heating = False

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
        # A cycle is only proven several minutes in, and its opening heating is
        # already behind us — count what the trace holds rather than start from
        # zero and lose the first phase.
        self._cycle.heats, self._cycle.last_heat_end = self._heats_so_far(
            started_at, confirmed_at, baseline)
        return Transition(kind="started", at=confirmed_at, appliance=self.config.appliance,
                          started_at=started_at,
                          reason="palier de chauffe dans la bande du lave-vaisselle")

    def _heats_so_far(self, since: datetime, until: datetime,
                      baseline: float) -> tuple[int, datetime | None]:
        """Heating phases already recorded in the trace, and when the last ended."""
        cfg = self.config
        count = 0
        last_end: datetime | None = None
        for stamp, watts in self._trace.samples:
            if not since <= stamp <= until:
                continue
            if not cfg.step_min_w <= watts - baseline <= cfg.step_max_w:
                continue
            if last_end is None or stamp - last_end >= cfg.heat_separation:
                count += 1
            last_end = stamp
        return count, last_end

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
        self._heating = False
        return [Transition(kind="finished", at=ended_at, appliance=cycle.appliance or "",
                           started_at=cycle.started_at,
                           duration_minutes=(ended_at - cycle.started_at).total_seconds() / 60,
                           energy_wh=round(cycle.energy_wh, 1),
                           heats=cycle.heats,
                           reason="durée attendue écoulée, plus de chauffe" if not overdue
                           else "durée maximale atteinte")]

    def tick(self, at: datetime) -> list[Transition]:
        """Re-decide on the clock alone — see :meth:`DryerDetector.tick`."""
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
    "DryerConfig",
    "DryerDetector",
    "Phase",
    "PlateauConfig",
    "PlateauDetector",
    "Transition",
    "elapsed_ratio",
    "remaining_minutes",
]
