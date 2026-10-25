"""Who gets the surplus, and in what order.

The two brains under :mod:`loads` each decide well on their own, and each one
already reasons in deltas — "if I add my draw to the grid, are we still
exporting?". What they cannot do is know about each other: run them on the same
meter reading and both claim the same watts, which is how a 650 W water heater
and a 340 W pump step get started against 700 W of surplus.

So this module does not re-implement affordability. It hands each load a grid
figure that already accounts for what the ones above it have taken:

    water heater   sees the real smoothed grid
    pool           sees it *plus* whatever the water heater just claimed

and the order of that list is the priority. For that order to hold, the pump's
draw above its baseline is handed back to the heater: it is surplus the pump
borrowed and steps down from, not consumption the heater must work around.

The second thing it does is subtract what humans have already started. A cycle
detected by :mod:`..logic.detector` is a load nobody controls, and its heating
phases will eat the surplus for the next hour. Reserving it matters only where
a decision cannot be walked back — see :func:`route`.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .loads import pool as pool_load
from .loads import water_heater as water_load
from .solar_start import WASHER_PROFILE

# Fallback average draw per appliance, used until enough cycles have been
# watched to know better. A dishwasher cycle measured here came to ~885 Wh over
# 63 minutes, so a little over 800 W spread across the hour — the heating
# phases are brutal but they are a minority of the cycle.
DEFAULT_APPLIANCE_DRAW_W: dict[str, float] = {
    "lave_vaisselle": 850.0,
    "lave_linge": 500.0,
    "seche_linge": 1200.0,
    "buanderie": 800.0,
}
FALLBACK_APPLIANCE_DRAW_W: float = 800.0
# Over its last minutes a cycle can barely overlap a two-hour hold: the reserve
# fades out over this span. Ten minutes at 850 W is ~140 Wh, two cents at a
# blue peak-hour rate, against a heater start delayed until the machine stops.
TAPER_MINUTES: float = 10.0
# Profiles measured minute by minute, where the mean would mislead. The
# washer's heating is 197 of its 317 Wh, all in the first twelve minutes.
PROFILES: dict[str, tuple[tuple[float, float], ...]] = {"lave_linge": WASHER_PROFILE}


def remaining_mean_w(profile: tuple[tuple[float, float], ...],
                     elapsed_min: float) -> float | None:
    """Energy still to come over time still to run, from a measured profile."""
    energy_wh = minutes = 0.0
    clock = 0.0
    for length, watts in profile:
        left = max(0.0, min(length, clock + length - elapsed_min))
        energy_wh += watts * left / 60
        minutes += left
        clock += length
    return energy_wh * 60 / minutes if minutes > 0 else None


@dataclass(frozen=True)
class RunningAppliance:
    """A cycle a human started, and what it is expected to keep drawing."""

    name: str
    # Measured mean draw over a whole cycle: energy / duration, from the
    # learned fingerprint. None until a cycle has been watched end to end.
    mean_draw_w: float | None = None
    # Where the cycle stands. Unknown, or past its expected end, keeps the
    # whole reserve: a late cycle is still running.
    elapsed_min: float | None = None
    remaining_min: float | None = None

    @property
    def base_w(self) -> float:
        """What the rest of the cycle draws on average."""
        profile = PROFILES.get(self.name)
        if profile is not None and self.elapsed_min is not None:
            rest = remaining_mean_w(profile, self.elapsed_min)
            if rest is not None:
                return rest
        if self.mean_draw_w is not None and self.mean_draw_w > 0:
            return self.mean_draw_w
        return DEFAULT_APPLIANCE_DRAW_W.get(self.name, FALLBACK_APPLIANCE_DRAW_W)

    @property
    def reserve_w(self) -> float:
        left = self.remaining_min
        if left is None or left <= 0 or left >= TAPER_MINUTES:
            return self.base_w
        return self.base_w * left / TAPER_MINUTES


@dataclass(frozen=True)
class Allocation:
    """What the router decided, and enough of its reasoning to display it."""

    water: water_load.Decision
    pool: pool_load.Decision
    grid_smooth_w: float
    appliance_reserve_w: float
    grid_for_pool_w: float
    claimed_by_water_w: float
    # The pump's draw above v1 in automatic mode, counted as surplus for the
    # heater (and the armed washer): the pump yields it.
    pool_yield_w: float = 0.0


def appliance_reserve_w(running: list[RunningAppliance]) -> float:
    """Watts to keep out of reach of an irreversible decision."""
    return sum(appliance.reserve_w for appliance in running)


def pool_yield_w(pool_inputs: pool_load.Inputs) -> float:
    """What the pump would give back by dropping to v1.

    Only in automatic mode: a speed chosen by hand is the user's, and winter
    already runs on the sun alone at v1 at most.
    """
    if pool_inputs.mode != pool_load.MODE_AUTO:
        return 0.0
    speed = pool_load._safe_speed(pool_inputs.pump_speed)
    return max(0.0, pool_load.PUMP_W[speed] - pool_load.PUMP_W[1])


def route(
    grid_smooth_w: float,
    water_inputs: water_load.Inputs,
    water_thr: water_load.Thresholds,
    pool_inputs: pool_load.Inputs,
    pool_thr: pool_load.Thresholds,
    running: list[RunningAppliance] | None = None,
) -> Allocation:
    """Decide for every load, in priority order.

    The appliance reserve is applied **only** to the water heater, and only
    while it is not already engaged. The asymmetry is deliberate and follows
    what an error costs:

    * starting the heater commits its contactor for two hours, so it must not
      be started on surplus that a dishwasher is about to eat;
    * continuing a cycle already under way is not reconsidered — the surplus
      is gone either way and the appliance cannot be stopped;
    * the pump moves a step every five minutes at worst, so it can simply
      follow the meter down. Reserving for it would idle the pump against a
      surplus that is genuinely there.
    """
    running = running or []
    reserve = appliance_reserve_w(running)
    yielded = pool_yield_w(pool_inputs)

    # A reserve makes the grid look *less* favourable: it is surplus already
    # spoken for. Only for the start decision. The pump's borrowed watts make
    # it look *more* favourable: they are the heater's by priority.
    already_engaged = water_inputs.signal_currently_on
    water_grid = grid_smooth_w - yielded
    if not already_engaged:
        water_grid += reserve
    water = water_load.decide(
        replace(water_inputs, grid_smooth_w=water_grid), water_thr
    )

    # What the heater will actually add to the meter, if it runs. When it is
    # already drawing, it is in the reading and claims nothing more.
    claimed = 0.0
    if water.signal_switch_on:
        claimed = max(0.0, water_thr.heater_nominal_w - water_inputs.heater_power_w)

    pool_grid = grid_smooth_w + claimed
    pool = pool_load.decide(replace(pool_inputs, grid_w=pool_grid), pool_thr)

    return Allocation(
        water=water,
        pool=pool,
        grid_smooth_w=grid_smooth_w,
        appliance_reserve_w=reserve,
        grid_for_pool_w=pool_grid,
        claimed_by_water_w=claimed,
        pool_yield_w=yielded,
    )


def summary(allocation: Allocation) -> str:
    """One line for a log or a sensor attribute."""
    parts = [f"réseau {allocation.grid_smooth_w:.0f} W"]
    if allocation.appliance_reserve_w:
        parts.append(f"réservé aux appareils {allocation.appliance_reserve_w:.0f} W")
    if allocation.claimed_by_water_w:
        parts.append(f"chauffe-eau {allocation.claimed_by_water_w:.0f} W")
    parts.append(f"piscine v{allocation.pool.target_speed}")
    return " · ".join(parts)


__all__ = [
    "Allocation",
    "RunningAppliance",
    "appliance_reserve_w",
    "pool_yield_w",
    "remaining_mean_w",
    "route",
    "summary",
]
