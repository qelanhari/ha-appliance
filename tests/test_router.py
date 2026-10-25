"""The router: priorities, and getting out of the way of humans.

What is tested here is only what the router adds — the two brains have their
own suites. Namely: that the pump no longer claims watts the water heater has
just taken, and that a cycle a human started is kept out of reach of a decision
that cannot be walked back.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.router import (
    HEATING_W,
    Allocation,
    RunningAppliance,
    appliance_reserve_w,
    has_priority,
    heating_done,
    pool_yield_w,
    route,
    summary,
)

# The module default is the 800 W nameplate; production injects the measured
# 650 W draw, and every figure below is read against that.
HEATER_W = 650.0


@pytest.fixture
def router_thr(thr):
    return replace(thr, heater_nominal_w=HEATER_W)


def allocate(grid_w, water_inputs, water_thr, pool_inputs, pool_thr,
             running=None) -> Allocation:
    return route(grid_w, water_inputs, water_thr, pool_inputs, pool_thr, running)


# ---------------------------------------------------------------------------
# Priority: the same watts cannot be spent twice
# ---------------------------------------------------------------------------


def test_the_pump_sees_the_grid_the_water_heater_leaves_behind(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """The whole point. 1 100 W of surplus pays for the heater, not for both.

    Run on the same reading, each brain would find 1 100 W of room: the heater
    takes 650 W and the pump steps up 340 W, and the meter ends up importing.
    """
    grid = -1100.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    allocation = allocate(grid, water, router_thr,
                          replace(pool_inputs, water_temp_c=26.0), pool_thr)

    assert allocation.water.signal_switch_on is True
    assert allocation.claimed_by_water_w == pytest.approx(HEATER_W)
    # The pump is handed −1100 + 650 = −450 W, which no longer pays for the
    # 340 W step up to v2 with its 200 W margin.
    assert allocation.grid_for_pool_w == pytest.approx(-450.0)
    assert allocation.pool.target_speed == 1


def test_with_enough_sun_both_get_served(base_inputs, router_thr, pool_inputs, pool_thr):
    grid = -2500.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    allocation = allocate(grid, water, router_thr,
                          replace(pool_inputs, water_temp_c=26.0), pool_thr)

    assert allocation.water.signal_switch_on is True
    assert allocation.pool.target_speed >= 2


def test_a_heater_already_running_claims_nothing_more(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """Its draw is already in the meter reading; counting it twice would idle
    the pump against surplus that exists."""
    grid = -900.0
    water = replace(base_inputs, grid_smooth_w=grid, heater_power_w=HEATER_W,
                    signal_currently_on=True,
                    signal_on_at=base_inputs.now - _minutes(30))
    allocation = allocate(grid, water, router_thr,
                          replace(pool_inputs, water_temp_c=26.0), pool_thr)

    assert allocation.claimed_by_water_w == pytest.approx(0.0)
    assert allocation.grid_for_pool_w == pytest.approx(grid)


def _minutes(n):
    from datetime import timedelta
    return timedelta(minutes=n)


# ---------------------------------------------------------------------------
# Machines come first, until their last heating
# ---------------------------------------------------------------------------


def test_a_dishwasher_still_to_heat_blocks_a_two_hour_commitment(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """1 100 W of surplus with a dishwasher between heats is not 1 100 W of
    surplus: its next 2 kW heating must find it free."""
    grid = -1100.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    running = [RunningAppliance("lave_vaisselle")]

    without = allocate(grid, water, router_thr, pool_inputs, pool_thr)
    with_it = allocate(grid, water, router_thr, pool_inputs, pool_thr, running)

    assert without.water.signal_switch_on is True
    assert with_it.water.signal_switch_on is False
    assert with_it.appliance_reserve_w == pytest.approx(HEATING_W["lave_vaisselle"])


def test_past_its_last_heating_it_steps_aside(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    grid = -1100.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    done = [RunningAppliance("lave_vaisselle", heating_done=True)]
    allocation = allocate(grid, water, router_thr, pool_inputs, pool_thr, done)
    assert allocation.water.signal_switch_on is True


def test_it_does_not_break_a_cycle_already_under_way(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """The contactor cannot be opened before two hours anyway."""
    grid = -900.0
    water = replace(base_inputs, grid_smooth_w=grid, heater_power_w=HEATER_W,
                    signal_currently_on=True,
                    signal_on_at=base_inputs.now - _minutes(30))
    allocation = allocate(grid, water, router_thr, pool_inputs, pool_thr,
                          [RunningAppliance("lave_vaisselle")])

    assert allocation.water.signal_switch_on is True


def test_the_pump_may_use_the_sun_while_a_machine_waits_or_pauses(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """It stops in seconds: it is not held back, it makes way when one heats."""
    grid = -1500.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=52.0)
    pool = replace(pool_inputs, water_temp_c=26.0)
    busy = allocate(grid, water, router_thr, pool, pool_thr,
                    [RunningAppliance("lave_vaisselle"),
                     RunningAppliance("lave_linge", waiting=True)])
    assert busy.pool.target_speed >= 2


@pytest.mark.parametrize(("machine", "priority"), [
    (RunningAppliance("lave_vaisselle"), True),
    (RunningAppliance("lave_vaisselle", heating_done=True), False),
    (RunningAppliance("lave_linge", waiting=True), False)])
def test_only_a_machine_under_way_and_still_to_heat_has_priority(machine, priority):
    assert has_priority(machine) is priority


def test_what_is_already_on_the_meter_is_not_reserved_twice():
    heating = RunningAppliance("lave_vaisselle", drawing_w=1900.0)
    assert heating.reserve_w == pytest.approx(80.0)


def test_two_machines_reserve_the_sum():
    running = [RunningAppliance("lave_vaisselle"), RunningAppliance("lave_linge")]
    assert appliance_reserve_w(running) == pytest.approx(
        HEATING_W["lave_vaisselle"] + HEATING_W["lave_linge"])


def test_the_washer_owes_its_measured_heating():
    assert RunningAppliance("lave_linge").reserve_w == pytest.approx(2109.0)


def test_the_dryer_owes_its_learned_draw_to_the_end():
    assert RunningAppliance("seche_linge", mean_draw_w=1850.0).reserve_w == 1850.0


def test_an_unknown_machine_still_owes_something():
    assert RunningAppliance("inconnu").reserve_w > 0


def test_nothing_running_reserves_nothing():
    assert appliance_reserve_w([]) == 0.0


def test_the_reserve_fades_over_the_last_minutes():
    dishwasher = RunningAppliance("lave_vaisselle", remaining_min=5.0)
    assert dishwasher.reserve_w == pytest.approx(990.0)


@pytest.mark.parametrize("left", [None, 0.0, -3.0, 30.0])
def test_unknown_late_or_distant_ends_keep_the_whole_reserve(left):
    machine = RunningAppliance("lave_vaisselle", remaining_min=left)
    assert machine.reserve_w == HEATING_W["lave_vaisselle"]


def test_an_armed_washer_does_not_fade():
    assert RunningAppliance("lave_linge", waiting=True, remaining_min=2.0).reserve_w == 2109.0


@pytest.mark.parametrize(("stage", "done"), [
    ("lavage", False), ("detection", False), ("vapeur", False), (None, False),
    ("rincage", True), ("essorage", True)])
def test_the_washer_s_heating_ends_with_its_wash(stage, done):
    assert heating_done("lave_linge", stage=stage) is done


@pytest.mark.parametrize(("heats", "expected", "now", "done"), [
    (2, 2, False, True), (2, 2, True, False), (1, 2, False, False), (5, None, False, False)])
def test_the_dishwasher_s_last_heating_is_its_usual_count(heats, expected, now, done):
    assert heating_done("lave_vaisselle", heats=heats, expected_heats=expected,
                        heating_now=now) is done


def test_the_dryer_heats_to_the_end():
    assert heating_done("seche_linge") is False


# ---------------------------------------------------------------------------
# The pump borrows above v1; the heater outranks it
# ---------------------------------------------------------------------------


def test_a_pump_at_v2_no_longer_keeps_the_heater_off(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """700 W of export with the pump at v2 is 1 040 W the heater may count on."""
    grid = -700.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    pool = replace(pool_inputs, pump_speed=2, water_temp_c=26.0)
    allocation = allocate(grid, water, router_thr, pool, pool_thr)

    assert allocation.water.signal_switch_on is True
    # Still exporting with both running: the pump keeps v2 within its hysteresis.
    assert allocation.grid_for_pool_w == pytest.approx(-50.0)
    assert allocation.pool.target_speed <= 2


def test_when_the_heater_needs_them_the_pump_hands_them_back(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    grid = -600.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    pool = replace(pool_inputs, pump_speed=2, water_temp_c=26.0)
    allocation = allocate(grid, water, router_thr, pool, pool_thr)

    assert allocation.water.signal_switch_on is True
    assert allocation.pool.target_speed == 1


def test_a_speed_set_by_hand_is_not_borrowed(pool_inputs):
    assert pool_yield_w(replace(pool_inputs, pump_speed=3, mode="v3")) == 0.0


@pytest.mark.parametrize(("speed", "watts"), [(0, 0.0), (1, 0.0), (2, 340.0), (3, 940.0)])
def test_the_pump_yields_its_draw_above_v1(pool_inputs, speed, watts):
    assert pool_yield_w(replace(pool_inputs, pump_speed=speed)) == pytest.approx(watts)


# ---------------------------------------------------------------------------
# Readability of the outcome
# ---------------------------------------------------------------------------


def test_the_summary_names_what_took_the_surplus(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    grid = -3500.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    line = summary(allocate(grid, water, router_thr,
                            replace(pool_inputs, water_temp_c=26.0), pool_thr,
                            [RunningAppliance("lave_vaisselle")]))
    assert "chauffe-eau" in line and "piscine" in line and "appareils" in line
