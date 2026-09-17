"""Replay of real power traces through the detectors.

Every fixture under ``tests/fixtures/`` was recorded from the instance by
``scripts/export_traces.py``. The positives are the cycles that must be caught;
the negatives — the oven, the hob, and the garage's fifteen-minute 180 W
episodes — are the ones that must never raise a thing. They are the point of
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
    PlateauConfig,
    PlateauDetector,
    SharedMeterConfig,
    SharedMeterDetector,
    elapsed_ratio,
    remaining_minutes,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def trace(name: str) -> list[tuple[datetime, float]]:
    payload = json.loads((FIXTURES / f"{name}.json").read_text())
    return [(datetime.fromisoformat(stamp), watts) for stamp, watts in payload["points"]]


def replay(detector, name: str, tick_after: int = 20) -> list:
    """Feed the trace, then let the clock run on.

    The coordinator ticks on a timer as well as on readings, because a meter
    that reports on change falls silent exactly when a cycle ends.
    """
    out = []
    last = None
    for at, watts in trace(name):
        out += detector.feed(at, watts)
        last = at
    if last is not None:
        for minute in range(1, tick_after + 1):
            out += detector.tick(last + timedelta(minutes=minute))
    return out


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
# Washer / dryer — one shared meter
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected, duration", [
    ("washer_wed_0835", "lave_linge", (55, 70)),
    ("washer_sat_1015", "lave_linge", (100, 130)),
    ("dryer_wed_1745", "seche_linge", (60, 80)),  # 17:48 -> 18:57 sur le compteur
    ("dryer_mon_1250", "seche_linge", (50, 70)),
])
def test_shared_meter_cycles_are_named_correctly(name, expected, duration):
    events = replay(SharedMeterDetector(), name)
    assert kinds(events) == ["started", "identified", "finished"]
    assert only(events, "identified").appliance == expected

    finished = only(events, "finished")
    assert finished.appliance == expected
    low, high = duration
    assert low <= finished.duration_minutes <= high


@pytest.mark.parametrize("name", [
    "garage_blip_wed_0745", "garage_blip_tue_0845", "garage_blip_mon_1730",
])
def test_the_fifteen_minute_180w_episodes_are_not_cycles(name):
    """Five of these in six days. Any plain "above 100 W" rule fires on them."""
    assert replay(SharedMeterDetector(), name) == []


def test_a_cycle_is_dated_from_the_rise_not_from_the_heating_burst():
    """The washer idles around 100-145 W for several minutes before it heats."""
    start = only(replay(SharedMeterDetector(), "washer_wed_0835"), "started")
    assert start.started_at < start.at
    assert (start.at - start.started_at) <= timedelta(minutes=10)


def test_energy_is_time_weighted_not_sample_weighted():
    """The dryer reports 80 samples for 70 minutes; the washer 1 300 for 60.

    Counting samples instead of seconds would put the dryer's consumption an
    order of magnitude below the washer's.
    """
    dryer = only(replay(SharedMeterDetector(), "dryer_wed_1745"), "finished")
    washer = only(replay(SharedMeterDetector(), "washer_wed_0835"), "finished")
    assert dryer.energy_wh > washer.energy_wh


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


def test_shared_meter_confirmation_sits_above_the_parasite_episodes():
    cfg = SharedMeterConfig()
    assert cfg.arm_w < 180 < cfg.confirm_w  # the blips arm, but never confirm
    assert cfg.classify_w > 300  # the washer's drum tops out around 300 W


# --------------------------------------------------------------------------
# What the traces taught, kept as guards
# --------------------------------------------------------------------------

def test_a_long_washer_programme_is_not_mistaken_for_a_second_appliance():
    """That programme heats three times, the last for ten minutes at 2 200 W.

    Averaged over twenty minutes it stays near 1 200 W, below the bar a dryer
    running alongside would hold — which is exactly why the bar sits where it
    does rather than where a five-minute window would have put it.
    """
    events = replay(SharedMeterDetector(), "washer_sat_1015")
    assert "peer" not in kinds(events)


def test_a_one_minute_blip_does_not_keep_a_finished_cycle_alive():
    """Two blips follow the dryer's last heat; each would add ten minutes."""
    finished = only(replay(SharedMeterDetector(), "dryer_wed_1745"), "finished")
    assert finished.at.strftime("%H:%M") < "19:00"


def test_a_cycle_survives_the_troughs_inside_it():
    """The washer's drum dips under 100 W for a minute at a time near the end."""
    events = replay(SharedMeterDetector(), "washer_wed_0835")
    assert kinds(events).count("finished") == 1


def test_a_freezer_start_up_surge_does_not_confirm_a_cycle():
    """The same circuit carries a freezer: ~120 W, surging to 2 kW on start.

    Touching the confirmation threshold is not enough — it has to be held, or
    every compressor start would open an hour-long laundry cycle.
    """
    detector = SharedMeterDetector()
    start = datetime(2026, 9, 17, 3, 0)
    events = []
    for second in range(0, 1800, 10):
        moment = start + timedelta(seconds=second)
        # Compressor running at 120 W, with a one-second 2 kW inrush every
        # ten minutes.
        surge = second % 600 == 0
        events += detector.feed(moment, 2000.0 if surge else 120.0)
        if surge:
            events += detector.feed(moment + timedelta(seconds=1), 120.0)
    assert events == []


def test_a_real_heating_still_confirms_within_a_couple_of_minutes():
    """The held-burst rule must not push the start of a true cycle out."""
    events = replay(SharedMeterDetector(), "dryer_wed_1745")
    start = only(events, "started")
    assert start.at.strftime("%H:%M") <= "17:51"  # meter first read 17:48


def test_a_thirty_second_run_is_enough_to_confirm():
    """Switching a machine on briefly must show up — it is how anyone tests it.

    Measured on the real meter: 31 W until 16:19:57, then 2 154 W for thirty
    seconds. The reporting interval is 14 s, so the confirmation has one or two
    readings to work with.
    """
    detector = SharedMeterDetector()
    start = datetime(2026, 9, 17, 16, 19, 52)
    trace_in = [(0, 31.0), (5, 2153.8), (19, 2143.2), (33, 2126.6),
                (35, 219.2), (36, 29.1), (50, 29.5)]
    events = []
    for offset, watts in trace_in:
        events += detector.feed(start + timedelta(seconds=offset), watts)
    assert [event.kind for event in events] == ["started"]


def test_a_single_reading_cannot_confirm_a_cycle():
    """Coming up mid-burst must not open a cycle on one sample.

    A reading holds until the next one, so without an observed-window check a
    lone 2 kW sample fills the whole confirmation window by itself. It happened
    in production: Home Assistant restarted during a burst, read 2 071 W once,
    and started an hour-long cycle dated from that instant.
    """
    detector = SharedMeterDetector()
    now = datetime(2026, 9, 17, 16, 24, 32)
    assert detector.feed(now, 2071.7) == []
    # Still nothing a few seconds later: the window is not covered yet.
    assert detector.feed(now + timedelta(seconds=5), 2100.0) == []
    # Once the trace spans the hold, the same level does confirm.
    assert [e.kind for e in detector.feed(now + timedelta(seconds=25), 2100.0)] \
        == ["started"]
