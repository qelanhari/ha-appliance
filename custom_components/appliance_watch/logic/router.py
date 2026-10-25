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

Above both come the machines humans start: dishwasher, washer, dryer. Until
their last heating they have priority, so the heating power they still owe is
reserved against the heater's start and the pump's step-ups — see
:class:`RunningAppliance`.

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
# Heating power of each machine, the step its element adds to the meter.
# Dishwasher: 1 940-2 015 W above the ambient base (README traces). Washer: its
# measured profile's heating. Dryer: 1 124.5 Wh over 35.3 min on 23/10, nearly
# all heating — replaced by the learned mean once cycles have been watched.
HEATING_W: dict[str, float] = {
    "lave_vaisselle": 1980.0,
    "lave_linge": max(watts for _, watts in WASHER_PROFILE),
    "seche_linge": 1900.0,
}
FALLBACK_HEATING_W: float = 2000.0
# Machines whose last heating cannot be told: the dryer heats until it stops.
HEATS_TO_THE_END: frozenset[str] = frozenset({"seche_linge"})
# Over its last minutes a cycle can barely overlap a two-hour hold: the reserve
# fades out over this span. Ten minutes at 2 kW is ~330 Wh, five cents at a
# blue peak-hour rate, against a heater start delayed until the machine stops.
TAPER_MINUTES: float = 10.0


@dataclass(frozen=True)
class RunningAppliance:
    """A machine a human started — or armed — and the heating it still owes.

    Until its last heating, a machine comes before the water heater and the
    pump: its next heating must find the surplus free. After it, what is left
    (tumbling, rinsing, drying on residual heat) is small and already on the
    meter, so nothing is reserved.
    """

    name: str
    # Learned mean draw over whole cycles; for the dryer, its heating power.
    mean_draw_w: float | None = None
    remaining_min: float | None = None
    # True once the last heating is over. Unknown is False: keep priority.
    heating_done: bool = False
    # What the meter already shows of it: reserving it again would count twice.
    drawing_w: float = 0.0
    # Armed for a solar start, not running yet: no end to fade towards.
    waiting: bool = False

    @property
    def heating_w(self) -> float:
        if (self.name in HEATS_TO_THE_END and self.mean_draw_w is not None
                and self.mean_draw_w > 0):
            return self.mean_draw_w
        return HEATING_W.get(self.name, FALLBACK_HEATING_W)

    @property
    def reserve_w(self) -> float:
        if self.heating_done:
            return 0.0
        owed = max(0.0, self.heating_w - self.drawing_w)
        left = self.remaining_min
        if self.waiting or left is None or left <= 0 or left >= TAPER_MINUTES:
            return owed
        return owed * left / TAPER_MINUTES


# The washer heats while it washes (or steams, or dries); from the rinse on,
# as ThinQ reports it, its heating is over.
WASHER_HEATING_OVER: frozenset[str] = frozenset(
    {"rincage", "essorage", "refroidissement", "attente_rincage"})


def heating_done(name: str, *, stage: str | None = None, heats: int = 0,
                 expected_heats: int | None = None, heating_now: bool = False) -> bool:
    """Whether a machine's last heating is behind it. Unknown is False."""
    if name == "lave_linge":
        return stage in WASHER_HEATING_OVER
    if name == "lave_vaisselle":
        return (expected_heats is not None and heats >= expected_heats
                and not heating_now)
    return False


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

    Machines still to heat come first: their reserve is surplus neither the
    heater nor the pump may take. It weighs on the heater only while it is not
    engaged — a contactor already closed cannot be opened before two hours, so
    there is nothing left to decide. It weighs on the pump's step-ups, while
    the pump keeps its v1 baseline.
    """
    running = running or []
    reserve = appliance_reserve_w(running)
    yielded = pool_yield_w(pool_inputs)

    # A reserve makes the grid look *less* favourable: it is surplus already
    # spoken for. The pump's borrowed watts make it look *more* favourable:
    # they are the heater's by priority.
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

    pool_grid = grid_smooth_w + claimed + reserve
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
    "WASHER_HEATING_OVER",
    "Allocation",
    "RunningAppliance",
    "appliance_reserve_w",
    "heating_done",
    "pool_yield_w",
    "route",
    "summary",
]
