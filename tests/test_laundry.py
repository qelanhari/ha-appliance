"""The washer as LG ThinQ reports it, and the dryer judged against it.

The ``thinq_*`` fixtures carry the laundry meter *and* what ThinQ said about
the washer over the same window, so they replay what the coordinator does: the
washer first, then the dryer told whether the washer runs.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.detector import DryerDetector, Phase  # noqa: E402
from logic.washer import WasherFollower, WasherReading  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
UNAVAILABLE = ("unknown", "unavailable")


def _value(raw: str):
    return None if raw in UNAVAILABLE else raw


def timeline(name: str, *, first_report: str | None = None) -> list:
    """Every meter reading and ThinQ change, in order, as (at, watts, reading).

    ``first_report`` ("HH:MM:SS") moves ThinQ's first report to that moment,
    with "power_off" before it — the washer reported late, not never.
    """
    payload = json.loads((FIXTURES / f"{name}.json").read_text())
    changes = [(datetime.fromisoformat(stamp), key, _value(raw))
               for key, series in payload["washer"].items()
               for stamp, raw in series]
    if first_report:
        first = min(change[0] for change in changes)
        moved = datetime.combine(first.date(), datetime.strptime(
            first_report, "%H:%M:%S").time(), first.tzinfo)
        changes = [(moved if at - first < timedelta(seconds=1) else at, key, value)
                   for at, key, value in changes]
    changes += [(datetime.fromisoformat(stamp), "watts", watts)
                for stamp, watts in payload["points"]]
    changes.sort(key=lambda change: change[0])

    current: dict = {"status": "power_off" if first_report else None,
                     "ends_at": None, "total": None}
    out = []
    for at, key, value in changes:
        current[key] = value
        reading = WasherReading(
            status=current["status"],
            ends_at=datetime.fromisoformat(current["ends_at"]) if current["ends_at"] else None,
            total=timedelta(minutes=float(current["total"])) if current["total"] else None,
        )
        out.append((at, current.get("watts"), reading))
    return out


def replay(name: str, **options) -> tuple[list, list, WasherFollower]:
    """The coordinator's order: washer, withdrawal, dryer, washer energy."""
    washer, dryer = WasherFollower(), DryerDetector()
    washer_events, dryer_events = [], []
    for at, watts, reading in timeline(name, **options):
        for event in washer.update(at, reading):
            washer_events.append(event)
            if event.kind == "started":
                dryer_events += dryer.washer_started(event.started_at)
        if watts is not None:
            dryer_events += dryer.feed(at, watts, washer=washer.running)
        washer.meter(at, watts, counted=dryer.phase is Phase.IDLE)
    return washer_events, dryer_events, washer


def kinds(events: list) -> list[str]:
    return [event.kind for event in events]


# --------------------------------------------------------------------------
# 3 Oct: an 86-minute wash, ThinQ connected half an hour into it
# --------------------------------------------------------------------------

def test_the_wash_is_followed_from_thinq_alone():
    washer_events, dryer_events, _ = replay("thinq_washer_sat_1720")
    assert kinds(washer_events) == ["started", "finished"]
    assert dryer_events == []  # the meter was the washer's throughout


def test_the_start_is_the_machine_s_not_the_moment_thinq_spoke():
    """First report at 17:50: 86 minutes, ending 18:47:13 — started 17:21:13.

    The meter rose at 17:18:54 for the washer's load sensing, so the date is
    right within the minutes the machine spends weighing the load. ThinQ sends
    the status a few milliseconds before the plan, so the start is corrected
    on the update after it.
    """
    started, finished = replay("thinq_washer_sat_1720")[0]
    assert started.at.strftime("%H:%M") == "17:50"
    assert finished.started_at.astimezone(started.at.tzinfo).strftime("%H:%M:%S") \
        == "17:21:13"


def test_the_length_is_measured_to_the_second_it_ended():
    finished = replay("thinq_washer_sat_1720")[0][-1]
    assert finished.at.strftime("%H:%M:%S") == "18:43:39"
    assert finished.duration_minutes == pytest.approx(82.4, abs=0.1)


def test_the_wash_energy_comes_off_the_meter():
    """Counted from the moment ThinQ reported it: 17:50 to 18:43."""
    finished = replay("thinq_washer_sat_1720")[0][-1]
    assert 50 < finished.energy_wh < 400


def test_a_late_thinq_withdraws_the_dryer_the_burst_opened():
    """Had ThinQ spoken only at 17:22, the 17:20 burst would open a dryer.

    The washer's own start then dates it 17:21:13 — within the lag of that
    cycle — so it was the washer, and the dryer is withdrawn rather than left
    running alongside it.
    """
    _, dryer_events, _ = replay("thinq_washer_sat_1720", first_report="17:22:00")
    assert kinds(dryer_events) == ["started", "cancelled"]


def test_a_thinq_that_never_spoke_in_time_is_not_papered_over():
    """Half an hour late, the burst's dryer cycle has already ended — and it
    stays reported. The lag rule is for a slow cloud, not an absent one."""
    _, dryer_events, _ = replay("thinq_washer_sat_1720", first_report="17:50:13")
    assert "cancelled" not in kinds(dryer_events)


def test_the_stages_follow_the_machine():
    seen = []
    washer = WasherFollower()
    for at, _, reading in timeline("thinq_washer_sat_1720"):
        washer.update(at, reading)
        if washer.stage and (not seen or seen[-1] != washer.stage):
            seen.append(washer.stage)
    assert seen == ["lavage", "rincage", "essorage"]


def test_the_expected_length_follows_the_machine_s_revisions():
    """ThinQ moves the end by a few minutes as it goes; so does the countdown."""
    washer = WasherFollower()
    expected = set()
    for at, _, reading in timeline("thinq_washer_sat_1720"):
        washer.update(at, reading)
        if washer.expected:
            expected.add(round(washer.expected.total_seconds() / 60))
    assert min(expected) == 82 and max(expected) == 86


# --------------------------------------------------------------------------
# What the washer follower must and must not conclude
# --------------------------------------------------------------------------

T0 = datetime(2026, 10, 3, 17, 0)


def _running(follower: WasherFollower, minute: int = 0) -> None:
    follower.update(T0 + timedelta(minutes=minute), WasherReading(
        "running", ends_at=T0 + timedelta(minutes=60), total=timedelta(minutes=60)))


def test_a_cloud_outage_does_not_end_a_wash():
    follower = WasherFollower()
    _running(follower)
    assert follower.update(T0 + timedelta(minutes=10), WasherReading(None)) == []
    assert follower.running is True
    # Nor blank its stage: the sensor would fall back to a value its enum lacks.
    assert follower.stage == "lavage"


@pytest.mark.parametrize("status", ["pause", "rinse_hold", "error"])
def test_a_stopped_drum_full_of_laundry_is_still_a_cycle(status):
    follower = WasherFollower()
    _running(follower)
    assert follower.update(T0 + timedelta(minutes=5), WasherReading(status)) == []
    assert follower.running is True


def test_no_answer_and_no_cycle_is_not_idle():
    """What keeps the dryer from claiming the meter during an outage."""
    assert WasherFollower().running is None


@pytest.mark.parametrize("status", ["power_off", "initial", "reserved", "end"])
def test_a_machine_that_is_not_washing_is_idle(status):
    follower = WasherFollower()
    assert follower.update(T0, WasherReading(status)) == []
    assert follower.running is False


def test_without_a_predicted_end_the_start_is_now():
    follower = WasherFollower()
    started = follower.update(T0, WasherReading("detecting"))[0]
    assert started.started_at == T0
    assert follower.expected is None


def test_the_next_programme_does_not_inherit_the_last_one_s_length():
    follower = WasherFollower()
    _running(follower)
    follower.update(T0 + timedelta(minutes=60), WasherReading("end"))
    follower.update(T0 + timedelta(minutes=90), WasherReading("detecting"))
    assert follower.expected is None
