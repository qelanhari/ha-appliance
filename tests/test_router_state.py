"""The router's memory and clocks: smoothing, holds, habits, persistence."""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.fingerprints import Fingerprint
from logic.loads import pool as pool_load
from logic.router_state import (
    RouterMemory,
    as_dict,
    dwell_elapsed,
    from_dict,
    mean_draw_w,
    smooth,
    solar_share,
)

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def test_first_reading_seeds_the_smoothing():
    assert smooth(None, -800.0, 0.0) == -800.0


def test_one_time_constant_covers_most_of_a_step():
    assert smooth(0.0, -1000.0, 132.0) == pytest.approx(-632.1, abs=0.5)


def test_smoothing_ignores_how_often_the_meter_reports():
    many = 0.0
    for _ in range(12):
        many = smooth(many, -1000.0, 10.0)
    assert many == pytest.approx(smooth(0.0, -1000.0, 120.0))


def test_a_clock_going_backwards_changes_nothing():
    assert smooth(-200.0, -1000.0, -30.0) == -200.0


def test_no_previous_change_means_free_to_act():
    assert dwell_elapsed(None, T0, 300) is True


def test_a_change_too_soon_is_refused():
    assert dwell_elapsed(T0, T0 + timedelta(seconds=299), 300) is False


def test_mean_draw_needs_a_few_cycles():
    assert mean_draw_w(Fingerprint(durations=(60.0,), energies=(900.0,))) is None


def test_mean_draw_is_median_energy_over_median_duration():
    learned = Fingerprint(durations=(60.0, 63.0, 66.0), energies=(850.0, 885.0, 1120.0))
    assert mean_draw_w(learned) == pytest.approx(885.0 * 60 / 63.0)


@pytest.mark.parametrize(("grid", "share"), [(-300.0, 1.0), (325.0, 0.5), (900.0, 0.0)])
def test_solar_share_of_the_heater(grid, share):
    assert solar_share(grid, 650.0) == pytest.approx(share)


def test_rising_contactor_opens_a_cycle_and_its_hold():
    memory = RouterMemory()
    memory.water.contactor_changed(T0, True)
    assert memory.water.signal_on_at == T0


def test_falling_contactor_releases_the_hold_and_the_boost():
    memory = RouterMemory()
    memory.water.contactor_changed(T0, True)
    memory.water.boost_requested = True
    memory.water.contactor_changed(T0 + timedelta(hours=2), False)
    assert (memory.water.signal_on_at, memory.water.boost_requested) == (None, False)


def test_heater_energy_is_booked_by_source():
    memory = RouterMemory()
    memory.water.meter(1000.0, -500.0, 650.0, "Bleu", False)
    memory.water.meter(1500.0, -500.0, 650.0, "Bleu", False)
    assert memory.water.cycle.kwh_solar == pytest.approx(0.5)


def test_a_new_day_folds_usage_into_the_weekday_pattern():
    memory = RouterMemory()
    memory.water.roll_day(date(2026, 10, 24))
    memory.water.day_kwh = 2.0
    memory.water.boosts_today = 1
    memory.water.roll_day(date(2026, 10, 25))
    assert (memory.water.pattern.samples_n, memory.water.boosts_today) == (1, 0)


def test_abundant_streak_restarts_after_a_break():
    memory = RouterMemory()
    memory.water.abundant_minutes(T0, True)
    memory.water.abundant_minutes(T0 + timedelta(minutes=5), False)
    assert memory.water.abundant_minutes(T0 + timedelta(minutes=6), True) == 0.0


def test_pump_step_is_booked_before_the_meter_shows_it():
    memory = RouterMemory(grid_smooth_w=-900.0)
    memory.book_pump_step(1, 2)
    assert memory.grid_smooth_w == pytest.approx(-900.0 + 500 - 160)


def test_leaving_v3_starts_the_cooldown():
    memory = RouterMemory()
    memory.pool.v3_started_at = T0
    memory.pool.speed_changed(T0 + timedelta(minutes=15), pool_load.Decision(
        target_speed=1, reason="", enter_v3=False, leave_v3=True))
    assert memory.pool.v3_last_ended_at == T0 + timedelta(minutes=15)


def test_memory_round_trips_through_the_store():
    memory = RouterMemory(piloting=True, grid_smooth_w=-420.0)
    memory.water.contactor_changed(T0, True)
    memory.water.roll_day(date(2026, 10, 25))
    memory.pool.mode = "winter"
    assert as_dict(from_dict(as_dict(memory))) == as_dict(memory)


def test_an_empty_store_is_a_fresh_start_that_does_not_drive():
    assert from_dict(None).piloting is False


def test_an_unknown_mode_falls_back_to_auto():
    assert from_dict({"water": {"mode": "turbo"}}).water.mode == "auto"


def test_a_naive_timestamp_is_dropped_rather_than_guessed():
    assert from_dict({"water": {"signal_on_at": "2026-10-25T12:00:00"}}).water.signal_on_at is None
