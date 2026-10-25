"""The « Routeur solaire » device: what it decided, why, and whether it acts.

Every entity here is a view on :class:`solar_router.SolarRouter`; none holds
state of its own. They exist only when the router is configured.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, UnitOfPower

from .const import ROUTER
from .coordinator import ApplianceCoordinator
from .entity import ApplianceEntity
from .logic.loads import pool as pool_load
from .logic.loads import water_heater as water_load
from .solar_router import SolarRouter

WATER_ACTIONS = ["wait", "heat_solar", "heat_hc", "boost", "hard_floor", "hold_signal"]


class RouterEntity(ApplianceEntity):
    def __init__(self, coordinator: ApplianceCoordinator, key: str) -> None:
        super().__init__(coordinator, ROUTER, key)

    @property
    def router(self) -> SolarRouter:
        return self.coordinator.router


@dataclass(frozen=True, kw_only=True)
class RouterSensorDescription(SensorEntityDescription):
    value: Callable[[SolarRouter], object]
    attributes: Callable[[SolarRouter], dict[str, Any]] | None = None


def _water_attributes(router: SolarRouter) -> dict[str, Any]:
    allocation, snapshot = router.allocation, router.snapshot
    if allocation is None or snapshot is None:
        return {}
    water = snapshot.water
    return {
        "raison": allocation.water.reason,
        "contacteur_voulu": allocation.water.signal_switch_on,
        "boost_voulu": allocation.water.boost_mode_on,
        "contacteur_reel": water.signal_currently_on,
        "milieu_cuve_c": water.tank_middle_c,
        "besoin_kwh": round(water.energy_needed_kwh, 2),
        "boosts_du_jour": water.boosts_today,
        "pilotage": router.memory.piloting,
    }


def _pool_attributes(router: SolarRouter) -> dict[str, Any]:
    allocation, snapshot = router.allocation, router.snapshot
    if allocation is None or snapshot is None:
        return {}
    return {
        "raison": allocation.pool.reason,
        "vitesse_reelle": snapshot.pool.pump_speed,
        "saison": allocation.pool.season,
        "relais_alimente": snapshot.pool_powered,
        "reseau_vu_w": round(allocation.grid_for_pool_w),
        "pilotage": router.memory.piloting,
    }


def _allocation(router: SolarRouter, field: str) -> float | None:
    allocation = router.allocation
    return round(getattr(allocation, field)) if allocation is not None else None


ROUTER_SENSORS: tuple[RouterSensorDescription, ...] = (
    RouterSensorDescription(
        key="chauffe_eau_decision",
        device_class=SensorDeviceClass.ENUM,
        options=WATER_ACTIONS,
        value=lambda r: r.allocation.water.action if r.allocation else None,
        attributes=_water_attributes,
    ),
    RouterSensorDescription(
        key="piscine_vitesse_cible",
        value=lambda r: r.allocation.pool.target_speed if r.allocation else None,
        attributes=_pool_attributes,
    ),
    RouterSensorDescription(
        key="reseau_lisse",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value=lambda r: _allocation(r, "grid_smooth_w"),
    ),
    RouterSensorDescription(
        key="reserve_appareils",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        value=lambda r: _allocation(r, "appliance_reserve_w"),
    ),
    RouterSensorDescription(
        key="chauffe_eau_jours_appris",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda r: r.memory.water.pattern.samples_n,
    ),
)


class RouterSensor(RouterEntity, SensorEntity):
    entity_description: RouterSensorDescription

    def __init__(self, coordinator: ApplianceCoordinator,
                 description: RouterSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> object:
        return self.entity_description.value(self.router)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        describe = self.entity_description.attributes
        return describe(self.router) if describe else None


class PilotingSwitch(RouterEntity, SwitchEntity):
    """Off by default: the router watches and explains until trusted.

    Turn `dhwp_smart` and `pool_pump` off (or their modes to « off ») before
    turning this on, or two brains will drive the same contactor and pump.
    """

    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, "pilotage")

    @property
    def is_on(self) -> bool:
        return self.router.memory.piloting

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.router.async_set_piloting(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.router.async_set_piloting(False)
        self.async_write_ha_state()


class WaterModeSelect(RouterEntity, SelectEntity):
    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, "mode_chauffe_eau")
        self._attr_options = list(water_load.MODES)

    @property
    def current_option(self) -> str:
        return self.router.memory.water.mode

    async def async_select_option(self, option: str) -> None:
        await self.router.async_set_water_mode(option)
        self.async_write_ha_state()


class PoolModeSelect(RouterEntity, SelectEntity):
    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, "mode_piscine")
        self._attr_options = list(pool_load.MODES)

    @property
    def current_option(self) -> str:
        return self.router.memory.pool.mode

    async def async_select_option(self, option: str) -> None:
        await self.router.async_set_pool_mode(option)
        self.async_write_ha_state()


def router_sensors(coordinator: ApplianceCoordinator) -> list[SensorEntity]:
    if not coordinator.router.configured:
        return []
    return [RouterSensor(coordinator, description) for description in ROUTER_SENSORS]
