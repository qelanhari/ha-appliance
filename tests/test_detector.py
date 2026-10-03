"""Replay of real power traces through the detectors.

Every fixture under ``tests/fixtures/`` was recorded from the instance by
``scripts/export_traces.py``. The positives are the cycles that must be caught;
the negatives — the oven, the hob, the garage's fifteen-minute 180 W episodes,
and every wash while the washer says it is running — are the ones that must
never raise a thing. They are the point of
this suite: a detector that only sees the positives is worthless here, since
the dishwasher is read from a load it shares with the oven.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.detector import (  # noqa: E402
    DryerConfig,
    DryerDetector,
    PlateauConfig,
    PlateauDetector,
    elapsed_ratio,
    remaining_minutes,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def trace(name: str) -> list[tuple[datetime, float]]:
    payload = json.loads((FIXTURES / f"{name}.json").read_text())
    return [(datetime.fromisoformat(stamp), watts) for stamp, watts in payload["points"]]


def replay(detector, name: str, tick_after: int = 20, **washer) -> list:
    """Feed the trace, then let the clock run on.

    The coordinator ticks on a timer as well as on readings, because a meter
    that reports on change falls silent exactly when a cycle ends. ``washer``
    is passed through to the dryer detector: what ThinQ says about the washer.
    """
    out = []
    last = None
    for at, watts in trace(name):
        out += detector.feed(at, watts, **washer)
        last = at
    if last is not None:
        for minute in range(1, tick_after + 1):
            out += detector.tick(last + timedelta(minutes=minute), **washer)
    return out


def dryer(name: str, tick_after: int = 20, washer: bool | None = False,
          detector: DryerDetector | None = None) -> list:
    """Replay a garage trace through the dryer, the washer idle unless told."""
    return replay(detector or DryerDetector(), name, tick_after, washer=washer)


def kinds(transitions: list) -> list[str]:
    return [t.kind for t in transitions]


def only(transitions: list, kind: str):
    matching = [t for t in transitions if t.kind == kind]
    assert len(matching) == 1, f"attendu 1 « {kind} », obtenu {len(matching)}"
    return matching[0]


# --------------------------------------------------------------------------
# Dishwasher — read from the unmeasured house load
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, started, duration", [
    ("dishwasher_thu_1342", "13:42", (55, 75)),
    ("dishwasher_wed_0142", "01:42", (55, 75)),
])
def test_dishwasher_cycles_are_detected_with_their_real_start(name, started, duration):
    events = replay(PlateauDetector(), name, tick_after=30)
    assert kinds(events) == ["started", "finished"]

    start = only(events, "started")
    expected = datetime.strptime(started, "%H:%M").time()
    assert abs(start.started_at.hour * 60 + start.started_at.minute
               - expected.hour * 60 - expected.minute) <= 2
    low, high = duration
    assert low <= only(events, "finished").duration_minutes <= high


def test_start_is_backdated_past_a_choppy_first_heating():
    """The 13:42 cycle only holds four unbroken minutes at its *second* plateau.

    Dating the cycle from there would put the countdown twenty minutes out.
    """
    start = only(replay(PlateauDetector(), "dishwasher_thu_1342"), "started")
    assert start.at.strftime("%H:%M") > "14:00"        # proven late
    assert start.started_at.strftime("%H:%M") <= "13:42"  # dated right


def test_dishwasher_detected_even_when_the_window_cuts_the_cycle_short():
    events = replay(PlateauDetector(), "dishwasher_mon_1700", tick_after=0)
    assert only(events, "started").started_at.strftime("%H:%M") <= "17:02"


@pytest.mark.parametrize("name", ["oven_mon_1804", "cooking_wed_1850"])
def test_cooking_never_starts_a_dishwasher_cycle(name):  # noqa: D103
    """The oven steps ~200 W higher, the hob toggles every minute: neither fits."""
    assert replay(PlateauDetector(), name) == []


def test_dishwasher_reports_a_plausible_energy():
    finished = only(replay(PlateauDetector(), "dishwasher_wed_0142", tick_after=30),
                    "finished")
    assert 400 <= finished.energy_wh <= 1600


# --------------------------------------------------------------------------
# Dryer — the laundry meter, whenever ThinQ says the washer is idle
# --------------------------------------------------------------------------

WASHES = ["washer_wed_0835", "washer_sat_1015", "washer_mon_1250", "washer_sat_1120"]


def test_a_drying_cycle_is_caught_end_to_end():
    events = dryer("dryer_wed_1745")
    assert kinds(events) == ["started", "finished"]
    finished = only(events, "finished")
    assert finished.appliance == "seche_linge"
    assert 55 <= finished.duration_minutes <= 65  # 17:48 -> 18:48 on the meter


@pytest.mark.parametrize("name", WASHES)
def test_nothing_opens_while_the_washer_runs(name):
    """The meter is the washer's then: a hot wash draws what a dryer draws.

    Naming one by its power is the mistake that called a washer "sèche-linge"
    on 19 Sept. ThinQ now says which machine runs, so nobody has to guess.
    """
    assert dryer(name, washer=True) == []


@pytest.mark.parametrize("name", [*WASHES, "dryer_wed_1745"])
def test_nothing_opens_while_thinq_has_no_answer(name):
    """A missing answer is not "the washer is off" — no more than a missing
    meter reading is 0 W. A dryer missed beats a washer named a dryer."""
    assert dryer(name, washer=None) == []


@pytest.mark.parametrize("name", [
    "garage_blip_wed_0745", "garage_blip_tue_0845", "garage_blip_mon_1730",
])
def test_the_fifteen_minute_180w_episodes_are_not_cycles(name):
    """Five of these in six days. Any plain "above 100 W" rule fires on them."""
    assert dryer(name) == []


def test_a_cycle_is_dated_from_the_rise_not_from_the_heating_burst():
    start = only(dryer("dryer_wed_1745"), "started")
    assert start.started_at < start.at
    assert (start.at - start.started_at) <= timedelta(minutes=10)


def test_energy_is_time_weighted_not_sample_weighted():
    """The dryer reports 80 samples for 70 minutes; a wash 1 300 for 60.

    Counting samples instead of seconds would put the dryer's consumption an
    order of magnitude below the washer's.
    """
    drying = only(dryer("dryer_wed_1745"), "finished")
    washing = only(dryer("washer_wed_0835"), "finished")  # as if it were a dryer
    assert drying.energy_wh > washing.energy_wh


def test_a_dryer_cycle_opened_by_the_washer_s_burst_is_withdrawn():
    """ThinQ can lag the meter: the cycle the burst opened was the washer's."""
    detector = DryerDetector()
    start = datetime(2026, 10, 3, 17, 20)
    for second in range(0, 120, 10):
        detector.feed(start + timedelta(seconds=second), 1700.0, washer=False)
    assert detector.cycle is not None

    events = detector.washer_started(start + timedelta(minutes=1))
    assert kinds(events) == ["cancelled"]
    assert detector.cycle is None


def test_a_dryer_already_running_survives_the_washer_joining():
    detector = DryerDetector()
    start = datetime(2026, 10, 3, 17, 0)
    for second in range(0, 120, 10):
        detector.feed(start + timedelta(seconds=second), 2000.0, washer=False)
    assert detector.washer_started(start + timedelta(minutes=30)) == []
    assert detector.cycle is not None


def test_with_the_washer_on_the_meter_only_heating_keeps_the_dryer_alive():
    """Its tumbling and the washer's drum look alike; its element does not."""
    detector = DryerDetector()
    start = datetime(2026, 10, 3, 17, 0)
    clock = start
    events = []
    for minutes, watts, washer in [(5, 2000.0, False),   # dryer heating
                                   (8, 200.0, True),     # both tumbling
                                   (5, 2000.0, True),    # dryer reheats
                                   (40, 180.0, True)]:   # washer alone
        for _ in range(minutes * 2):
            events += detector.feed(clock, watts, washer=washer)
            clock += timedelta(seconds=30)
    finished = only(events, "finished")
    assert finished.at - start <= timedelta(minutes=19)  # its last heat
    assert detector.cycle is None


# --------------------------------------------------------------------------
# Progress helpers, fed straight into the Live Activity payload
# --------------------------------------------------------------------------

def test_progress_never_reaches_a_hundred_before_the_end():
    start = datetime(2026, 9, 17, 13, 42)
    nominal = timedelta(minutes=63)
    assert elapsed_ratio(start, start, nominal) == 0
    assert elapsed_ratio(start, start + timedelta(minutes=32), nominal) == 51
    # An overrun must not show a bar past full, nor a finished cycle.
    assert elapsed_ratio(start, start + timedelta(hours=3), nominal) == 99


def test_remaining_time_floors_at_zero():
    start = datetime(2026, 9, 17, 13, 42)
    nominal = timedelta(minutes=63)
    assert remaining_minutes(start, start, nominal) == 63
    assert remaining_minutes(start, start + timedelta(minutes=70), nominal) == 0


# --------------------------------------------------------------------------
# Threshold guards — a changed default should break a test, not a laundry day
# --------------------------------------------------------------------------

def test_dishwasher_band_stays_clear_of_the_oven():
    """Measured: dishwasher steps 1 940-2 015 W, oven 2 150-2 230 W."""
    cfg = PlateauConfig()
    assert cfg.step_min_w < 1940 and cfg.step_max_w > 2015
    assert cfg.step_max_w < 2150


def test_dryer_confirmation_sits_above_the_parasite_episodes():
    cfg = DryerConfig()
    assert cfg.arm_w < 180 < cfg.confirm_w  # the blips arm, but never confirm


def test_the_washer_lag_covers_what_thinq_was_measured_at():
    """3 Oct: the meter rose 2 min 19 s before the start ThinQ reports."""
    assert DryerConfig().washer_lag >= timedelta(minutes=2, seconds=19) * 2


def test_the_shared_end_waits_longer_than_the_dryer_rests_between_heats():
    """Measured gap between two heats: 8.2 min."""
    assert DryerConfig().shared_off_delay > timedelta(minutes=8.2)


# --------------------------------------------------------------------------
# What the traces taught, kept as guards
# --------------------------------------------------------------------------

def test_a_one_minute_blip_does_not_keep_a_finished_cycle_alive():
    """Two blips follow the dryer's last heat; each would add ten minutes."""
    finished = only(dryer("dryer_wed_1745"), "finished")
    assert finished.at.strftime("%H:%M") < "19:00"


def test_a_freezer_start_up_surge_does_not_confirm_a_cycle():
    """The same circuit carries a freezer: ~120 W, surging to 2 kW on start.

    Touching the confirmation threshold is not enough — it has to be held, or
    every compressor start would open an hour-long laundry cycle.
    """
    detector = DryerDetector()
    start = datetime(2026, 9, 17, 3, 0)
    events = []
    for second in range(0, 1800, 10):
        moment = start + timedelta(seconds=second)
        # Compressor running at 120 W, with a one-second 2 kW inrush every
        # ten minutes.
        surge = second % 600 == 0
        events += detector.feed(moment, 2000.0 if surge else 120.0, washer=False)
        if surge:
            events += detector.feed(moment + timedelta(seconds=1), 120.0, washer=False)
    assert events == []


def test_a_real_heating_still_confirms_within_a_couple_of_minutes():
    """The held-burst rule must not push the start of a true cycle out."""
    events = dryer("dryer_wed_1745")
    start = only(events, "started")
    assert start.at.strftime("%H:%M") <= "17:51"  # meter first read 17:48


def test_a_thirty_second_run_is_enough_to_confirm():
    """Switching a machine on briefly must show up — it is how anyone tests it.

    Measured on the real meter: 31 W until 16:19:57, then 2 154 W for thirty
    seconds. The reporting interval is 14 s, so the confirmation has one or two
    readings to work with.
    """
    detector = DryerDetector()
    start = datetime(2026, 9, 17, 16, 19, 52)
    trace_in = [(0, 31.0), (5, 2153.8), (19, 2143.2), (33, 2126.6),
                (35, 219.2), (36, 29.1), (50, 29.5)]
    events = []
    for offset, watts in trace_in:
        events += detector.feed(start + timedelta(seconds=offset), watts, washer=False)
    assert [event.kind for event in events] == ["started"]


def test_a_single_reading_cannot_confirm_a_cycle():
    """Coming up mid-burst must not open a cycle on one sample.

    A reading holds until the next one, so without an observed-window check a
    lone 2 kW sample fills the whole confirmation window by itself. It happened
    in production: Home Assistant restarted during a burst, read 2 071 W once,
    and started an hour-long cycle dated from that instant.
    """
    detector = DryerDetector()
    now = datetime(2026, 9, 17, 16, 24, 32)
    assert detector.feed(now, 2071.7, washer=False) == []
    # Still nothing a few seconds later: the window is not covered yet.
    assert detector.feed(now + timedelta(seconds=5), 2100.0, washer=False) == []
    # Once the trace spans the hold, the same level does confirm.
    assert [e.kind for e in detector.feed(now + timedelta(seconds=25), 2100.0, washer=False)] \
        == ["started"]


def test_the_dryer_ends_sooner_than_the_washer():
    """It is the machine stopped part-way, so it gets a shorter grace period.

    Not the one minute that would be ideal: measured dead times *inside* a
    cycle reach 234 s on one recording, and cutting at a minute would end the
    activity ten minutes early, then open a second one when the drum resumes.
    """
    events = dryer("dryer_wed_1745", tick_after=8)
    finished = only(events, "finished")
    assert finished.appliance == "seche_linge"
    assert finished.at.strftime("%H:%M") == "18:48"  # last sustained draw
    assert 55 <= finished.duration_minutes <= 65


def test_the_rhythm_of_a_cycle_is_measured():
    """Dead times are what tell "stopped" from "between heats" apart.

    The one recorded dryer cycle never goes quiet, so the measurement is shown
    on a trace that does: 234 s on that wash, the figure the floor rests on.
    """
    finished = only(dryer("washer_mon_1250", tick_after=12), "finished")
    assert finished.longest_pause_s >= 120


def test_a_learned_rhythm_never_cuts_below_a_longer_dead_time():
    """The rhythm is learned as a *max*, so one quiet cycle cannot licence it.

    Feeding the detector a pause shorter than this cycle's own would end it
    early — which is exactly what a mean would have done.
    """
    detector = DryerDetector()
    detector.learned_pause_s = 234.0  # the worst seen across cycles
    finished = only(dryer("dryer_wed_1745", tick_after=12, detector=detector), "finished")
    assert 55 <= finished.duration_minutes <= 65


# --------------------------------------------------------------------------
# Dishwasher phases — the one machine whose stage can honestly be read
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("dishwasher_thu_1342", 4),
    ("dishwasher_wed_0142", 2),  # la bouffée d’une minute à 02:01 est sous la bande
])
def test_heating_phases_are_counted(name, expected):
    """Four heatings on the Thursday cycle, three on the night one.

    They are what tells how far along a dishwasher really is — far better than
    a percentage of a nominal hour.
    """
    finished = only(replay(PlateauDetector(), name, tick_after=30), "finished")
    assert finished.heats == expected


def test_the_opening_heating_is_counted_even_though_it_proved_nothing():
    """That burst alternates heating and pumping, so the cycle is only proven
    twenty minutes later — but it was the first heating all the same, and it
    counts as *one* despite its dips."""
    detector = PlateauDetector()
    for at, watts in trace("dishwasher_thu_1342"):
        started = detector.feed(at, watts)
        if started and started[0].kind == "started":
            # The choppy 13:42 burst plus the plateau that proved the cycle.
            assert detector.heats == 2
            assert started[0].started_at.strftime("%H:%M") == "13:41"
            return
    raise AssertionError("cycle jamais détecté")


def test_heating_is_reported_while_it_lasts():
    detector = PlateauDetector()
    seen_hot = seen_cold = False
    for at, watts in trace("dishwasher_thu_1342"):
        detector.feed(at, watts)
        if detector.cycle is None:
            continue
        seen_hot |= detector.heating
        seen_cold |= not detector.heating
    assert seen_hot and seen_cold  # both phases occur within one cycle
