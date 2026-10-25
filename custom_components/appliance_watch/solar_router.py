"""Home Assistant side of the solar router: entities in, two commands out.

The judgement is :mod:`logic.router`'s; the memory and clocks are
:mod:`logic.router_state`'s. This reads the meters, the tank, the pool and the
tariff, asks the router, and — only while « Pilotage » is on — drives the
water heater's contactor and boost and the pump's speed.

Off, the router still decides every tick and shows what it would do: that is
how it is compared with `dhwp_smart` and `pool_pump` before replacing them.
Its memory follows the real contactor and pump either way, so switching it on
mid-cycle inherits the two-hour hold and the v3 session as they are.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOOST,
    CONF_CONTACTOR,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_GARAGE_TEMP,
    CONF_GRID,
    CONF_HEATER_ENERGY,
    CONF_HEATER_NOMINAL,
    CONF_HEATER_POWER,
    CONF_OUTDOOR_TEMP,
    CONF_POOL_AIR_TEMP,
    CONF_POOL_SPEED,
    CONF_POOL_SWITCH,
    CONF_POOL_WARM,
    CONF_POOL_WATER_TEMP,
    CONF_SOLAR,
    CONF_TANK_MIDDLE,
    CONF_TANK_TOP,
    CONF_TEMPO_COLOR,
    CONF_TEMPO_HC,
    CONF_TEMPO_NEXT,
    DEFAULT_HEATER_NOMINAL_W,
    DOMAIN,
    STORAGE_VERSION,
    entry_value,
)
from .logic.loads import pool as pool_load
from .logic.loads import water_heater as water_load
from .logic.pattern import expected_kwh_for_day
from .logic.residual import to_watts
from .logic.router import Allocation, RunningAppliance, has_priority, route
from .logic.router_state import (
    CONTACTOR_DWELL_S,
    PUMP_DWELL_S,
    RouterMemory,
    as_dict,
    dwell_elapsed,
    from_dict,
    pattern_from_dict,
)
from .logic.thermo import energy_budget_until_morning_kwh

_LOGGER = logging.getLogger(__name__)

EVENT_ROUTER = f"{DOMAIN}_routeur"
LEGACY_WATER_DOMAIN = "dhwp_smart"
SUN = "sun.sun"
# When the morning's hot water must be there (dhwp_smart's default).
MORNING_DEADLINE = time(6, 30)
TANK_CAPACITY_L = 270
# The boost is a number of days on the Atlantic; one spans any cycle and
# expires on its own if Home Assistant goes down.
BOOST_DAYS = 1.0
SAVE_DELAY_S = 30

# Reads one entity's state, or None when it has nothing to say.
Reader = Callable[[str | None], Any]


@dataclass(frozen=True)
class Snapshot:
    """What the router was given on its last tick, for display."""

    water: water_load.Inputs
    pool: pool_load.Inputs
    pool_powered: bool


class SolarRouter:
    """Routes the surplus to the water heater, then the pool pump."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, state: Reader) -> None:
        self.hass = hass
        self.entry = entry
        self._state = state
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.routeur")
        self.memory = RouterMemory()
        self.allocation: Allocation | None = None
        self.snapshot: Snapshot | None = None
        # "name: watts" per cycle reserved on the last tick, for display.
        self.running_reserves: list[str] = []

    # ------------------------------------------------------------ settings

    def _entity(self, key: str) -> str | None:
        return entry_value(self.entry, key) or None

    @property
    def pool_yield_w(self) -> float:
        """Watts the pump would hand back now, only if the router drives it."""
        if not self.memory.piloting or self.allocation is None:
            return 0.0
        if self.snapshot is not None and not self.snapshot.pool_powered:
            return 0.0
        return self.allocation.pool_yield_w

    @property
    def configured(self) -> bool:
        """Both loads and the meter: the router allocates between the two."""
        return all(self._entity(key) for key in (CONF_GRID, CONF_CONTACTOR, CONF_POOL_SPEED))

    def water_thresholds(self) -> water_load.Thresholds:
        return water_load.Thresholds(heater_nominal_w=float(entry_value(
            self.entry, CONF_HEATER_NOMINAL, DEFAULT_HEATER_NOMINAL_W)))

    def pool_thresholds(self) -> pool_load.Thresholds:
        base = pool_load.Thresholds()
        return pool_load.Thresholds(water_warm_c=float(entry_value(
            self.entry, CONF_POOL_WARM, base.water_warm_c)))

    # --------------------------------------------------------- persistence

    async def async_load(self) -> None:
        self.memory = from_dict(await self._store.async_load())
        if self.memory.water.pattern.samples_n == 0:
            await self._adopt_legacy_pattern()

    async def _adopt_legacy_pattern(self) -> None:
        """Take over the weekday habits dhwp_smart has learned, once."""
        for legacy in self.hass.config_entries.async_entries(LEGACY_WATER_DOMAIN):
            store: Store[dict[str, Any]] = Store(
                self.hass, 1, f"{LEGACY_WATER_DOMAIN}.{legacy.entry_id}")
            data = await store.async_load() or {}
            pattern = pattern_from_dict(data.get("pattern"))
            if pattern.samples_n:
                self.memory.water.pattern = pattern
                _LOGGER.info("Routeur : %d jours d'habitudes repris de dhwp_smart",
                             pattern.samples_n)
                return

    def _save(self) -> None:
        self._store.async_delay_save(lambda: as_dict(self.memory), SAVE_DELAY_S)

    async def async_set_piloting(self, enabled: bool) -> None:
        self.memory.piloting = enabled
        await self._store.async_save(as_dict(self.memory))

    async def async_set_water_mode(self, mode: str) -> None:
        self.memory.water.mode = mode
        await self._store.async_save(as_dict(self.memory))

    async def async_set_pool_mode(self, mode: str) -> None:
        self.memory.pool.mode = mode
        # A new mode takes effect now, not after the pump's dwell.
        self.memory.pool.last_speed_change_at = None
        await self._store.async_save(as_dict(self.memory))

    # ------------------------------------------------------------- reading

    def _float(self, key: str) -> float | None:
        state = self._state(self._entity(key))
        try:
            return float(state.state) if state is not None else None
        except ValueError:
            return None

    def _watts(self, key: str) -> float | None:
        state = self._state(self._entity(key))
        try:
            return (to_watts(float(state.state), state.attributes.get("unit_of_measurement"))
                    if state is not None else None)
        except ValueError:
            return None

    def _text(self, key: str) -> str | None:
        state = self._state(self._entity(key))
        return state.state if state is not None else None

    def _is_on(self, key: str) -> bool | None:
        state = self._state(self._entity(key))
        return None if state is None else state.state == "on"

    # ------------------------------------------------------------- the tick

    async def process(self, now: datetime, running: list[RunningAppliance]) -> None:
        """Read, decide, and act if piloting. Skipped without a grid reading."""
        if not self.configured:
            return
        grid = self._watts(CONF_GRID)
        speed = self._float(CONF_POOL_SPEED)
        if grid is None or speed is None:
            # Deciding on a missing meter, or a pump read as speed 0, is how
            # the old brains ping-ponged: hold everything until they return.
            return
        local = dt_util.as_local(now)
        smoothed = self.memory.feed_grid(now, grid)
        water = self._water_inputs(local, grid, smoothed)
        pool = self._pool_inputs(now, smoothed, pool_load._safe_speed(int(speed)))
        self._observe(now, water, pool)
        powered = self._is_on(CONF_POOL_SWITCH) is not False
        self.snapshot = Snapshot(water=water, pool=pool, pool_powered=powered)
        self.allocation = route(smoothed, water, self.water_thresholds(),
                                pool, self.pool_thresholds(), running)
        self.memory.pool.season = self.allocation.pool.season
        self.running_reserves = [f"{item.name}: {item.reserve_w:.0f} W" for item in running]
        # The heater's claim, or a machine still to heat: the pump hands its
        # surplus back now, not in 5 minutes. Its dwell is for clouds.
        make_way = (self.allocation.claimed_by_water_w > 0
                    or any(has_priority(item) for item in running))
        if self.memory.piloting:
            await self._drive_water(now, water, self.allocation.water)
            if powered:
                await self._drive_pool(now, pool, self.allocation.pool, make_way=make_way)
        self._save()

    def _water_inputs(self, local: datetime, grid_raw: float,
                      smoothed: float) -> water_load.Inputs:
        memory, thr = self.memory.water, self.water_thresholds()
        memory.roll_day(local.date())
        outdoor = self._float(CONF_OUTDOOR_TEMP)
        if outdoor is not None:
            memory.outdoor_sum += outdoor
            memory.outdoor_count += 1
        top = self._float(CONF_TANK_TOP)
        garage = self._float(CONF_GARAGE_TEMP)
        heater_w = self._watts(CONF_HEATER_POWER) or 0.0
        tempo = self._text(CONF_TEMPO_COLOR) or "Bleu"
        is_hc = self._is_on(CONF_TEMPO_HC) is True
        memory.meter(self._float(CONF_HEATER_ENERGY), grid_raw, heater_w, tempo, is_hc)
        usage = expected_kwh_for_day(memory.pattern, local.weekday(),
                                     outdoor if outdoor is not None else 15.0)
        needed = energy_budget_until_morning_kwh(
            # An unread top probe is taken as at target: nothing to heat.
            current_top_c=top if top is not None else thr.target_top_c,
            target_top_c=thr.target_top_c, floor_c=thr.hard_floor_c,
            garage_c=garage if garage is not None else 18.0,
            hours_to_morning=_hours_until(local, MORNING_DEADLINE),
            expected_usage_kwh=usage, capacity_l=TANK_CAPACITY_L)
        contactor_on = self._is_on(CONF_CONTACTOR) is True
        boost = (self._float(CONF_BOOST) or 0.0) > 0.5 or (
            contactor_on and memory.boost_requested)
        abundant = water_load.abundant_surplus_for_boost(smoothed, heater_w, thr)
        return water_load.Inputs(
            now=local, mode=memory.mode,
            tank_top_c=top if top is not None else thr.target_top_c,
            tank_middle_c=self._float(CONF_TANK_MIDDLE),
            garage_c=garage if garage is not None else 18.0,
            outdoor_c=outdoor, grid_smooth_w=smoothed,
            pv_power_w=self._watts(CONF_SOLAR) or 0.0, heater_power_w=heater_w,
            tempo_color=tempo, tempo_next_color=self._text(CONF_TEMPO_NEXT),
            is_hc=is_hc,
            forecast_today_kwh=self._float(CONF_FORECAST_TODAY),
            forecast_tomorrow_kwh=self._float(CONF_FORECAST_TOMORROW),
            energy_needed_kwh=needed, cycle=memory.cycle,
            signal_on_at=memory.signal_on_at, signal_currently_on=contactor_on,
            boost_currently_on=boost, boosts_today=memory.boosts_today,
            abundant_surplus_minutes=memory.abundant_minutes(local, abundant),
        )

    def _pool_inputs(self, now: datetime, smoothed: float, speed: int) -> pool_load.Inputs:
        memory = self.memory.pool
        sun = self.hass.states.get(SUN)
        return pool_load.Inputs(
            now=now, daylight=sun is not None and sun.state == "above_horizon",
            grid_w=smoothed, pump_speed=speed,
            water_temp_c=self._float(CONF_POOL_WATER_TEMP),
            air_temp_c=self._float(CONF_POOL_AIR_TEMP), mode=memory.mode,
            v3_started_at=memory.v3_started_at,
            v3_last_ended_at=memory.v3_last_ended_at,
            tempo_color=self._text(CONF_TEMPO_COLOR),
            is_hc=self._is_on(CONF_TEMPO_HC) is True, season=memory.season,
        )

    def _observe(self, now: datetime, water: water_load.Inputs,
                 pool: pool_load.Inputs) -> None:
        """Follow the real contactor and pump, whoever moved them."""
        if water.signal_currently_on != self.memory.water.switch_was_on:
            self.memory.water.contactor_changed(now, water.signal_currently_on)
        memory = self.memory.pool
        if pool.pump_speed == 3 and memory.v3_started_at is None:
            memory.v3_started_at = now
        elif pool.pump_speed != 3 and memory.v3_started_at is not None:
            memory.v3_started_at = None
            memory.v3_last_ended_at = now

    # ------------------------------------------------------------- driving

    async def _drive_water(self, now: datetime, inputs: water_load.Inputs,
                           decision: water_load.Decision) -> None:
        memory = self.memory.water
        wanted, actual = decision.signal_switch_on, inputs.signal_currently_on
        sovereign = memory.mode in water_load.MANUAL_MODES
        if wanted != actual and (sovereign or dwell_elapsed(
                memory.last_switch_change_at, now, CONTACTOR_DWELL_S)):
            service = "turn_on" if wanted else "turn_off"
            if await self._call("switch", service, {"entity_id": self._entity(CONF_CONTACTOR)},
                                decision.reason):
                memory.contactor_changed(now, wanted)
        # The boost is ours only within our own cycle: a boost set by hand on
        # the Atlantic, contactor off, is left alone.
        if (actual or wanted) and self._entity(CONF_BOOST):
            await self._drive_boost(decision.boost_mode_on, decision.reason)

    async def _drive_boost(self, boost: bool, reason: str) -> None:
        memory = self.memory.water
        if boost and not memory.boost_requested:
            memory.boosts_today += 1
        memory.boost_requested = boost
        desired = BOOST_DAYS if boost else 0.0
        current = self._float(CONF_BOOST)
        if current is not None and abs(current - desired) < 0.5:
            return
        await self._call("number", "set_value",
                         {"entity_id": self._entity(CONF_BOOST), "value": desired}, reason)

    async def _drive_pool(self, now: datetime, inputs: pool_load.Inputs,
                          decision: pool_load.Decision, *, make_way: bool) -> None:
        memory = self.memory.pool
        if decision.target_speed == inputs.pump_speed:
            return
        # Leaving v3 is never delayed: it burns 1.1 kW on the grid otherwise.
        # Neither is stepping down for a load that just started.
        stepping_down = decision.target_speed < inputs.pump_speed
        sovereign = (memory.mode in pool_load.MANUAL_TO_SPEED or decision.leave_v3
                     or (make_way and stepping_down))
        if not sovereign and not dwell_elapsed(memory.last_speed_change_at, now, PUMP_DWELL_S):
            return
        if await self._call("number", "set_value",
                            {"entity_id": self._entity(CONF_POOL_SPEED),
                             "value": float(decision.target_speed)}, decision.reason):
            self.memory.book_pump_step(inputs.pump_speed, decision.target_speed)
            memory.speed_changed(now, decision)

    async def _call(self, domain: str, service: str, data: dict[str, Any],
                    reason: str) -> bool:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Routeur : %s.%s refusé (%s) : %s", domain, service, reason, err)
            return False
        _LOGGER.info("Routeur : %s.%s %s — %s", domain, service, data, reason)
        self.hass.bus.async_fire(EVENT_ROUTER, {
            "service": f"{domain}.{service}", **data, "raison": reason})
        return True


def _hours_until(local: datetime, deadline: time) -> float:
    target = local.replace(hour=deadline.hour, minute=deadline.minute,
                           second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds() / 3600.0


__all__ = ["EVENT_ROUTER", "Snapshot", "SolarRouter"]
