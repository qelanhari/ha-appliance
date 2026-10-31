"""The « Routeur solaire » device: what it decided, why, and whether it acts.

Every entity here is a view on :class:`solar_router.SolarRouter`; none holds
state of its own. They exist only when the router is configured.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
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
from .entity import QUIET_WRITE_S, ApplianceEntity
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
    # Its value, or an attribute, moves at every meter reading.
    noisy: bool = False


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
        "soleil_utilisable_2h_kwh": _round(water.solar_usable_hold_kwh),
        "soleil_utilisable_demain_kwh": _round(water.solar_usable_tomorrow_kwh),
        "prevision_demain_kwh": _round(water.forecast_tomorrow_kwh),
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
        "cede_au_chauffe_eau_w": round(allocation.pool_yield_w),
        "filtration_du_jour_min": round(router.memory.pool.filtered_min),
        "pilotage": router.memory.piloting,
    }


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _sky_attributes(router: SolarRouter) -> dict[str, Any]:
    learned = router.memory.sky
    return {
        "source_courbe": router.forecast.entry_id,
        "heures_de_courbe": len(router.forecast.curve),
        "consommation_maison_par_heure_w": [round(value) for value in learned.house_w],
        "production_reelle_du_jour_kwh": round(learned.day_actual_kwh, 2),
        "prevision_du_jour_kwh": _round(learned.day_forecast_kwh),
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
        noisy=True,
    ),
    RouterSensorDescription(
        key="reseau_lisse",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value=lambda r: _allocation(r, "grid_smooth_w"),
        noisy=True,
    ),
    RouterSensorDescription(
        key="reserve_appareils",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        value=lambda r: _allocation(r, "appliance_reserve_w"),
        attributes=lambda r: {"cycles": sorted(r.running_reserves)},
    ),
    RouterSensorDescription(
        key="biais_prevision",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda r: round(r.memory.sky.bias, 2),
        attributes=_sky_attributes,
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
        if description.noisy:
            self._min_write_s = QUIET_WRITE_S

    @property
    def native_value(self) -> object:
        return self.entity_description.value(self.router)

    def _milestone(self) -> object:
        value = self.native_value
        # A power reading is news only when it appears or goes; a speed always is.
        if self.device_class == SensorDeviceClass.POWER:
            return self.available, value is None
        return self.available, value

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


SPEED_TO_MODE: dict[int, str] = {speed: mode for mode, speed
                                 in pool_load.MANUAL_TO_SPEED.items()}


class PoolSpeedSlider(RouterEntity, NumberEntity):
    """0-3 on a slider: moving it pins that speed as the pool's mode.

    In an automatic mode it shows the speed the router wants, so the slider
    is not stuck at 0; the « Mode de la piscine » select brings back auto.
    """

    _attr_icon = "mdi:pool"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 3
    _attr_native_step = 1

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, "vitesse_manuelle_piscine")

    @property
    def native_value(self) -> int | None:
        mode = self.router.memory.pool.mode
        if mode in pool_load.MANUAL_TO_SPEED:
            return pool_load.MANUAL_TO_SPEED[mode]
        allocation = self.router.allocation
        return allocation.pool.target_speed if allocation else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"mode": self.router.memory.pool.mode}

    async def async_set_native_value(self, value: float) -> None:
        await self.router.async_set_pool_mode(SPEED_TO_MODE[round(value)])
        await self.coordinator.async_request_refresh()


class ForceHeatButton(RouterEntity, ButtonEntity):
    """A day's boost of the Atlantic, at once; it ends on its own."""

    _attr_icon = "mdi:water-boiler-alert"

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, "chauffe_forcee")

    async def async_press(self) -> None:
        await self.router.async_force_heat()


class LaunchSkimButton(RouterEntity, ButtonEntity):
    """A v3 skim session now, if the sun pays for it; cooldown waived."""

    _attr_icon = "mdi:pool"

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, "lancer_ecumage")

    async def async_press(self) -> None:
        await self.router.async_launch_skim()
        await self.coordinator.async_request_refresh()


class StartWasherButton(RouterEntity, ButtonEntity):
    """The armed washer started now, as the router would; its programme still
    has to be held by SmartThinQ."""

    _attr_icon = "mdi:washing-machine"

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, "lancer_lave_linge")

    async def async_press(self) -> None:
        self.coordinator.solar.force_start()
        await self.coordinator.async_request_refresh()


def router_numbers(coordinator: ApplianceCoordinator) -> list[NumberEntity]:
    if not coordinator.router.configured:
        return []
    return [PoolSpeedSlider(coordinator)]


def router_buttons(coordinator: ApplianceCoordinator) -> list[ButtonEntity]:
    if not coordinator.router.configured:
        return []
    return [ForceHeatButton(coordinator), LaunchSkimButton(coordinator),
            StartWasherButton(coordinator)]


def router_sensors(coordinator: ApplianceCoordinator) -> list[SensorEntity]:
    if not coordinator.router.configured:
        return []
    return [RouterSensor(coordinator, description) for description in ROUTER_SENSORS]
