"""When SmartThinQ is polled to remember, or forget, the armed programme."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.program_memory import ProgramMemory, Refresh

T0 = datetime(2026, 10, 26, 12, 16)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def armed_and_remembered() -> ProgramMemory:
    memory = ProgramMemory()
    memory.observe(at(0), armed=True, asleep=False, idle=True)
    memory.observe(at(30), armed=True, asleep=False, idle=True)
    memory.remembered()
    return memory


def test_the_programme_is_remembered_once_arming_has_settled():
    memory = ProgramMemory()
    assert memory.observe(at(0), armed=True, asleep=False, idle=True) is None
    assert memory.observe(at(30), armed=True, asleep=False, idle=True) is Refresh.REMEMBER


def test_it_is_ready_once_smartthinq_offers_the_start():
    assert armed_and_remembered().ready


def test_it_is_not_ready_before_smartthinq_offers_the_start():
    memory = ProgramMemory()
    memory.observe(at(0), armed=True, asleep=False, idle=True)
    memory.observe(at(30), armed=True, asleep=False, idle=True)
    assert not memory.ready


def test_a_failed_poll_is_asked_again_later_not_every_reading():
    memory = ProgramMemory()
    memory.observe(at(0), armed=True, asleep=False, idle=True)
    memory.observe(at(30), armed=True, asleep=False, idle=True)
    assert memory.observe(at(60), armed=True, asleep=False, idle=True) is None
    assert memory.observe(at(330), armed=True, asleep=False, idle=True) is Refresh.REMEMBER


def test_a_remembered_programme_is_not_polled_again():
    memory = armed_and_remembered()
    assert memory.observe(at(400), armed=True, asleep=False, idle=True) is None


def test_a_washer_seen_asleep_first_is_never_remembered():
    """After the sleep, a poll would read the default programme."""
    memory = ProgramMemory()
    memory.observe(at(0), armed=True, asleep=True, idle=False)
    assert memory.observe(at(600), armed=True, asleep=False, idle=True) is None
    assert not memory.ready


def test_sleeping_after_being_remembered_keeps_it_ready():
    memory = armed_and_remembered()
    memory.observe(at(720), armed=True, asleep=True, idle=False)
    assert memory.ready


def test_nothing_is_remembered_while_the_solar_start_is_off():
    memory = ProgramMemory()
    memory.observe(at(0), armed=True, asleep=False, idle=True, wanted=False)
    assert memory.observe(at(30), armed=True, asleep=False, idle=True,
                          wanted=False) is None


def test_disarming_is_forgotten():
    memory = armed_and_remembered()
    assert memory.observe(at(60), armed=False, asleep=False, idle=True) is Refresh.FORGET


def test_disarming_is_no_longer_ready():
    memory = armed_and_remembered()
    memory.observe(at(60), armed=False, asleep=False, idle=True)
    assert not memory.ready


def test_an_unconfirmed_forget_is_asked_again_later():
    memory = armed_and_remembered()
    memory.observe(at(60), armed=False, asleep=False, idle=True)
    assert memory.observe(at(90), armed=False, asleep=False, idle=True) is None
    assert memory.observe(at(360), armed=False, asleep=False, idle=True) is Refresh.FORGET


def test_a_confirmed_forget_is_not_asked_again():
    memory = armed_and_remembered()
    memory.observe(at(60), armed=False, asleep=False, idle=True)
    memory.forgotten()
    assert memory.observe(at(360), armed=False, asleep=False, idle=True) is None


def test_rearming_before_the_forget_cannot_be_trusted():
    """SmartThinQ would still hold the previous programme."""
    memory = armed_and_remembered()
    memory.observe(at(60), armed=False, asleep=False, idle=True)
    memory.observe(at(70), armed=True, asleep=False, idle=True)
    assert memory.observe(at(400), armed=True, asleep=False, idle=True) is None


def test_rearming_after_the_forget_is_remembered_afresh():
    memory = armed_and_remembered()
    memory.observe(at(60), armed=False, asleep=False, idle=True)
    memory.forgotten()
    memory.observe(at(70), armed=True, asleep=False, idle=True)
    assert memory.observe(at(100), armed=True, asleep=False, idle=True) is Refresh.REMEMBER


def test_a_washer_never_seen_armed_is_not_forgotten():
    memory = ProgramMemory()
    assert memory.observe(at(0), armed=False, asleep=False, idle=True) is None


def test_an_unknown_arming_changes_nothing():
    memory = armed_and_remembered()
    assert memory.observe(at(60), armed=None, asleep=False, idle=False) is None
    assert memory.ready


def test_a_late_confirmation_after_disarming_is_ignored():
    memory = ProgramMemory()
    memory.observe(at(0), armed=True, asleep=False, idle=True)
    memory.observe(at(30), armed=True, asleep=False, idle=True)
    memory.observe(at(35), armed=False, asleep=False, idle=True)
    memory.remembered()
    assert not memory.ready
