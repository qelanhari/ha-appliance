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
    # Longest silence *inside* the cycle — how the machine's rhythm is learned.
    longest_pause_s: float | None = None
    heats: int | None = None
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
    # The fast path, and the common case: a quarter of an hour in, a washer on
    # a cold or short programme is already back to agitating. A dryer never is.
    # The reverse does *not* hold — a washer heating its water draws exactly
    # what a dryer draws — so a high reading here concludes nothing and the
    # rhythm below decides instead. Getting that backwards is what named a
    # washing machine "sèche-linge" on 19 Sept: its wash heated from T+6.6 to
    # T+27.8 and the window landed inside it.
    classify_at: timedelta = timedelta(minutes=20)
    classify_from: timedelta = timedelta(minutes=15)
    classify_w: float = 800.0  # measured: washer 144-147 W in this window
    # What actually separates the two machines: the dryer reheats throughout
    # the cycle, the washer heats once and then only tumbles. Measured gaps
    # between blocks, as this detector merges them — dryer **8.2 min**; washer
    # 49.2, 19.0 and 19.6 min on its long programme, and no second block at all
    # on the short ones. The threshold sits between 8.2 and 19.0 rather than
    # just above 8.2: there is only *one* confirmed dryer trace, and hugging it
    # would repeat the mistake that named a washer "sèche-linge".
    heat_w: float = 1000.0
    # A block has to last: the freezer on this circuit surges past 1.5 kW for
    # about a second when its compressor starts.
    heat_min_s: float = 60.0
    # The washer's element dips below the threshold as the drum turns under
    # it; those dips are not the end of a block.
    heat_merge_s: float = 90.0
    reheat_gap_max: timedelta = timedelta(minutes=13)
    # Strictly longer than reheat_gap_max, so a resumption always gets its
    # chance to speak before silence is read as the washer's answer.
    settled_gap: timedelta = timedelta(minutes=17)
    # The drum still turning, as opposed to a cycle winding down to standby.
    # Time-weighted medians over the quiet window: 177, 174, 146 and **86** W
    # across four recorded washes — the drum drops to 35 W between tumbles and
    # those dips carry real weight. Set well under the lowest of them: a false
    # positive costs nothing (a cycle winding down is reported as the washer
    # anyway), a false negative leaves the machine unnamed.
    drum_w: float = 60.0
    # A name can be put right once, and not near the end of a cycle.
    correct_within: timedelta = timedelta(minutes=60)
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
    # The dryer is the one machine stopped part-way through, so its cycle
    # should close sooner than the washer's ten minutes. It cannot be as short
    # as one minute though: measured mid-cycle silences under the idle
    # threshold run to 300 s on one recorded cycle and 79 s on another, and a
    # one-minute rule would have cut that activity ten minutes early — then
    # started a second one when the drum picked up again. Six minutes clears
    # the longest silence seen with a small margin.
    off_delay_burst: timedelta = timedelta(minutes=6)
    off_delay_floor: timedelta = timedelta(minutes=2)
    active_window_burst: timedelta = timedelta(seconds=30)
    nominal: timedelta = timedelta(minutes=60)
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
    peer: str | None = None
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
    # Heating blocks that have closed, as (start, end). The rhythm of these is
    # what tells a dryer from a washer.
    heat_blocks: list[tuple[datetime, datetime]] = field(default_factory=list)
    reclassified: bool = False

    @property
    def longest_pause_s(self) -> float:
        return max(self.pauses_s) if self.pauses_s else 0.0


class SharedMeterDetector:
    """Washer/dryer cycles on a single meter."""

    def __init__(self, config: SharedMeterConfig | None = None) -> None:
        self.config = config or SharedMeterConfig()
        self._trace = Trace(keep=timedelta(minutes=35))
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
        window = (self.config.active_window_burst
                  if cycle.appliance == self.config.burst
                  else self.config.active_window)
        sustained = self._trace.mean(at - window, at)
        if sustained is not None and sustained >= self.config.idle_w:
            if cycle.quiet_since is not None:
                # A silence that ended: that is a pause in the machine's
                # rhythm, not the end of the cycle.
                cycle.pauses_s.append((at - cycle.quiet_since).total_seconds())
                cycle.quiet_since = None
            cycle.last_active = max(cycle.last_active, at)
        elif cycle.quiet_since is None:
            cycle.quiet_since = at

        self._track_heat(at, watts, cycle)

        out: list[Transition] = []
        named = None
        if cycle.appliance is None or self._may_correct(at, cycle):
            named = self._classify(at, cycle)
        if named is not None and named[0] != cycle.appliance:
            out += self._name(at, cycle, named)
        elif cycle.appliance is not None and cycle.peer is None:
            out += self._look_for_peer(at, cycle)
        out += self._maybe_finish(at, cycle)
        return out

    # -- the heating element, block by block --------------------------------

    def _track_heat(self, at: datetime, watts: float, cycle: Cycle) -> None:
        """Follow the element so the gaps between its blocks can be read."""
        cfg = self.config
        if watts >= cfg.heat_w:
            if cycle.heating_since is None:
                cycle.heating_since = at
            cycle.last_heat_end = at
            return
        if cycle.heating_since is None or cycle.last_heat_end is None:
            return
        if (at - cycle.last_heat_end).total_seconds() < cfg.heat_merge_s:
            return  # a dip under the drum, not the end of the block
        start, end = cycle.heating_since, cycle.last_heat_end
        cycle.heating_since = None
        if (end - start).total_seconds() >= cfg.heat_min_s:
            cycle.heat_blocks.append((start, end))
            cycle.heats = len(cycle.heat_blocks)

    def _reheat_gap(self, cycle: Cycle) -> timedelta | None:
        """How long the element rested before firing again, once it has.

        The block in flight counts only once it has *drawn* for heat_min_s —
        measured end to end, not merely been open that long, so a compressor
        inrush waiting out heat_merge_s can never look like a resumption.
        """
        cfg = self.config
        if (cycle.heating_since is not None and cycle.last_heat_end is not None
                and cycle.heat_blocks
                and (cycle.last_heat_end - cycle.heating_since).total_seconds()
                >= cfg.heat_min_s):
            return cycle.heating_since - cycle.heat_blocks[-1][1]
        if len(cycle.heat_blocks) >= 2:
            return cycle.heat_blocks[-1][0] - cycle.heat_blocks[-2][1]
        return None

    # -- naming --------------------------------------------------------------

    def _may_correct(self, at: datetime, cycle: Cycle) -> bool:
        """A wrong name can be put right once, early, and only one way.

        The fast path can only ever produce "washer", and that is also the
        costly direction to get wrong: the cycle then takes the washer's longer
        grace period and carries the wrong machine's name for an hour. Renaming
        near the end would be worse than living with the name, hence the cap.
        """
        cfg = self.config
        return (not cycle.reclassified
                and cycle.appliance == cfg.steady
                and at - cycle.started_at <= cfg.correct_within)

    def _name(self, at: datetime, cycle: Cycle,
              named: tuple[str, str]) -> list[Transition]:
        appliance, reason = named
        if cycle.appliance is not None:
            cycle.reclassified = True
            reason = f"correction — {reason}"
        cycle.appliance = appliance
        return [Transition(kind="identified", at=at, appliance=appliance,
                           started_at=cycle.started_at, reason=reason)]

    def _classify(self, at: datetime, cycle: Cycle) -> tuple[str, str] | None:
        """Name the appliance, or return None while the evidence is thin.

        Three ways to decide, in order of how soon they can speak:

        1. cool at T+20 — the washer, without ambiguity;
        2. the element fires again soon after resting — the dryer;
        3. it stays off while the drum keeps turning — the washer.

        Deciding *late* is the price of not deciding *wrong*: a hot wash or a
        drying cycle is named around T+35-43 instead of at T+20.
        """
        cfg = self.config
        if (cycle.appliance is None
                and at - cycle.started_at >= cfg.classify_at):
            # A *fixed* window. Ending it at `at` instead would stretch it a
            # little further every tick, so a hot wash eventually falls under
            # the threshold and gets named by the fast path after all — which
            # is how two recorded washes landed on 790 and 797 W against a
            # threshold of 800, correct by accident and by one percent.
            mean = self._trace.mean(cycle.started_at + cfg.classify_from,
                                    cycle.started_at + cfg.classify_at)
            if mean is not None and mean <= cfg.classify_w:
                return cfg.steady, f"{mean:.0f} W en moyenne entre T+15 et T+20"

        gap = self._reheat_gap(cycle)
        if gap is not None and gap <= cfg.reheat_gap_max:
            return cfg.burst, (f"la chauffe repart après "
                               f"{gap.total_seconds() / 60:.0f} min de repos")

        if cycle.heating_since is None and cycle.last_heat_end is not None:
            quiet = at - cycle.last_heat_end
            if quiet >= cfg.settled_gap:
                # The drum has to still be turning. A cycle simply winding down
                # is not evidence of anything, and _maybe_finish will close it.
                level = self._trace.quantile(cycle.last_heat_end, at, 0.5)
                if level is not None and level > cfg.drum_w:
                    return cfg.steady, (
                        f"plus de chauffe depuis {quiet.total_seconds() / 60:.0f} min, "
                        f"tambour à {level:.0f} W")
        return None

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

    def _off_delay_for(self, cycle: Cycle) -> timedelta:
        """How long a silence has to last before the cycle is called over.

        For the dryer — the one machine stopped part-way — this tightens as the
        machine's own rhythm becomes known: twice the longest pause ever seen
        between two heats, never below two minutes nor above the configured
        ceiling. Until a cycle has been watched end to end, the ceiling stands.
        """
        if cycle.appliance != self.config.burst:
            return self.config.off_delay
        ceiling = self.config.off_delay_burst
        if self.learned_pause_s <= 0:
            return ceiling
        learned = timedelta(seconds=self.learned_pause_s * 2)
        return max(self.config.off_delay_floor, min(ceiling, learned))

    def _maybe_finish(self, at: datetime, cycle: Cycle) -> list[Transition]:
        if at - cycle.last_active < self._off_delay_for(cycle):
            return []
        ended = cycle.last_active
        self._cycle = None
        self._trace.clear()
        return [Transition(kind="finished", at=ended,
                           appliance=cycle.appliance or self.config.steady,
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
