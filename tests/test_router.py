"""The router: priorities, and getting out of the way of humans.

What is tested here is only what the router adds — the two brains have their
own suites. Namely: that the pump no longer claims watts the water heater has
just taken, and that a cycle a human started is kept out of reach of a decision
that cannot be walked back.
"""

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.router import (  # noqa: E402
    DEFAULT_APPLIANCE_DRAW_W,
    Allocation,
    RunningAppliance,
    appliance_reserve_w,
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
# Getting out of the way of a cycle a human started
# ---------------------------------------------------------------------------


def test_a_running_dishwasher_blocks_a_two_hour_commitment(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """1 100 W of surplus with a dishwasher mid-cycle is not 1 100 W of surplus.

    Its heating phases will take most of it back, and the contactor cannot be
    reopened for two hours.
    """
    grid = -1100.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    running = [RunningAppliance("lave_vaisselle", mean_draw_w=850.0)]

    without = allocate(grid, water, router_thr, pool_inputs, pool_thr)
    with_it = allocate(grid, water, router_thr, pool_inputs, pool_thr, running)

    assert without.water.signal_switch_on is True
    assert with_it.water.signal_switch_on is False
    assert with_it.appliance_reserve_w == pytest.approx(850.0)


def test_it_does_not_break_a_cycle_already_under_way(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """The surplus is gone either way, and the appliance cannot be stopped."""
    grid = -900.0
    water = replace(base_inputs, grid_smooth_w=grid, heater_power_w=HEATER_W,
                    signal_currently_on=True,
                    signal_on_at=base_inputs.now - _minutes(30))
    allocation = allocate(grid, water, router_thr, pool_inputs, pool_thr,
                          [RunningAppliance("lave_vaisselle", 850.0)])

    assert allocation.water.signal_switch_on is True


def test_the_pump_is_not_held_back_by_the_reserve(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    """It steps down in five minutes; reserving for it would waste real sun."""
    grid = -1500.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=52.0)
    allocation = allocate(grid, water, router_thr,
                          replace(pool_inputs, water_temp_c=26.0), pool_thr,
                          [RunningAppliance("lave_vaisselle", 850.0)])

    assert allocation.grid_for_pool_w == pytest.approx(grid)
    assert allocation.pool.target_speed >= 2


def test_two_appliances_reserve_the_sum():
    running = [RunningAppliance("lave_vaisselle", 850.0),
               RunningAppliance("seche_linge", 1200.0)]
    assert appliance_reserve_w(running) == pytest.approx(2050.0)


def test_an_unlearned_appliance_falls_back_to_a_nominal_draw():
    assert RunningAppliance("lave_vaisselle").reserve_w == \
        DEFAULT_APPLIANCE_DRAW_W["lave_vaisselle"]
    assert RunningAppliance("inconnu").reserve_w > 0


def test_nothing_running_reserves_nothing():
    assert appliance_reserve_w([]) == 0.0


# ---------------------------------------------------------------------------
# Readability of the outcome
# ---------------------------------------------------------------------------


def test_the_summary_names_what_took_the_surplus(
    base_inputs, router_thr, pool_inputs, pool_thr
):
    grid = -2500.0
    water = replace(base_inputs, grid_smooth_w=grid, tank_middle_c=45.0,
                    energy_needed_kwh=2.0)
    line = summary(allocate(grid, water, router_thr,
                            replace(pool_inputs, water_temp_c=26.0), pool_thr,
                            [RunningAppliance("lave_vaisselle", 850.0)]))
    assert "chauffe-eau" in line and "piscine" in line and "appareils" in line
