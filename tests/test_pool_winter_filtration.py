"""Winter still filters two hours a day: the sun first, the off-peak night after."""

import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.loads.pool import (
    Inputs,
    Thresholds,
    decide,
    minutes_to_hc_end,
    winter_top_up_due,
)
from logic.router_state import PoolMemory

TZ = timezone(timedelta(hours=1))


def night(hour: int, minute: int = 0, *, filtered: float = 0.0, **kw) -> Inputs:
    """A winter night, off-peak, pump stopped, cold water."""
    now = datetime(2026, 12, 10, hour, minute, tzinfo=TZ)
    if hour < 6:
        now += timedelta(days=1)
    base = Inputs(now=now, daylight=False, grid_w=300.0, pump_speed=0, water_temp_c=12.0,
                  air_temp_c=3.0, mode="auto", v3_started_at=None, v3_last_ended_at=None,
                  tempo_color="Bleu", is_hc=True, season="hiver", filtered_today_min=filtered)
    return replace(base, **kw)


def test_nothing_runs_while_the_window_can_still_hold_the_top_up():
    assert decide(night(3, 30, filtered=60.0), Thresholds()).target_speed == 0


def test_the_top_up_starts_so_as_to_end_with_the_window():
    decision = decide(night(5, 0, filtered=60.0), Thresholds())
    assert decision.target_speed == 1
    assert "complément" in decision.reason


def test_a_day_with_no_sun_runs_the_full_two_hours_from_four():
    assert decide(night(3, 59), Thresholds()).target_speed == 0
    assert decide(night(4, 0), Thresholds()).target_speed == 1


def test_a_sunny_day_that_filtered_enough_needs_nothing():
    assert decide(night(5, 30, filtered=130.0), Thresholds()).target_speed == 0


def test_it_runs_on_a_rouge_night_too_the_off_peak_is_cheap():
    assert decide(night(5, 0, filtered=60.0, tempo_color="Rouge"), Thresholds()).target_speed == 1


def test_never_outside_the_off_peak_window():
    assert winter_top_up_due(night(5, 0, is_hc=False), Thresholds()) is None


def test_summer_is_left_as_it_was():
    summer = night(5, 0, season="ete", water_temp_c=25.0, mode="auto")
    assert "complément" not in decide(summer, Thresholds()).reason


def test_minutes_to_the_end_of_the_window_cross_midnight():
    assert minutes_to_hc_end(night(22, 0), Thresholds()) == pytest.approx(480.0)


# ------------------------------------------------------------ the counting


def test_filtration_counts_only_while_the_pump_runs():
    memory, start = PoolMemory(), datetime(2026, 12, 10, 12, 0, tzinfo=TZ)
    for minute in range(0, 61):
        memory.count_filtration(start + timedelta(minutes=minute), running=minute < 30)
    assert memory.filtered_min == pytest.approx(30.0)


def test_the_filtration_day_starts_at_six():
    memory = PoolMemory()
    memory.count_filtration(datetime(2026, 12, 11, 5, 59, tzinfo=TZ), running=True)
    memory.count_filtration(datetime(2026, 12, 11, 6, 0, tzinfo=TZ), running=True)
    assert (memory.filtered_min, memory.filtration_day) == (0.0, date(2026, 12, 11))


def test_a_long_gap_is_not_counted_whole():
    memory = PoolMemory()
    memory.count_filtration(datetime(2026, 12, 10, 12, 0, tzinfo=TZ), running=True)
    memory.count_filtration(datetime(2026, 12, 10, 14, 0, tzinfo=TZ), running=True)
    assert memory.filtered_min == 10.0
