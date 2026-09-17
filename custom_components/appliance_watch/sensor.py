"""Sensors: what is running, for how much longer, and at what cost."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BOTH, DISHWASHER, DOMAIN, DRYER, LAUNDRY, UNKNOWN, WASHER, WATCHERS
from .coordinator import ApplianceCoordinator, WatcherState
from .entity import ApplianceEntity


@dataclass(frozen=True, kw_only=True)
class WatcherSensor(SensorEntityDescription):
    """A sensor description plus how to read it off the watcher."""

    value: Callable[[WatcherState], object]
    watchers: tuple[str, ...] = WATCHERS


SENSORS: tuple[WatcherSensor, ...] = (
    WatcherSensor(
        key="etat",
        device_class=SensorDeviceClass.ENUM,
        options=["veille", "en_cours", "termine"],
        value=lambda state: state.phase.value,
    ),
    WatcherSensor(
        key="appareil",
        device_class=SensorDeviceClass.ENUM,
        options=[UNKNOWN, WASHER, DRYER, BOTH],
        # Only the shared meter has anything to guess.
        watchers=(LAUNDRY,),
        value=lambda state: state.appliance,
    ),
    WatcherSensor(
        key="phase",
        device_class=SensorDeviceClass.ENUM,
        options=["veille", "chauffe", "cycle", "termine"],
        # Only the dishwasher: its heating *is* the detection signal, so the
        # stage can be stated rather than guessed. A washing machine's cannot.
        watchers=(DISHWASHER,),
        value=lambda state: state.stage,
    ),
    WatcherSensor(
        key="chauffes",
        watchers=(DISHWASHER,),
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda state: state.heats,
    ),
    WatcherSensor(
        key="temps_restant",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value=lambda state: state.remaining,
    ),
    WatcherSensor(
        key="progression",
        native_unit_of_measurement=PERCENTAGE,
        value=lambda state: state.progress,
    ),
    WatcherSensor(
        key="debut",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda state: state.started_at,
    ),
    WatcherSensor(
        key="fin",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda state: state.finished_at,
    ),
    WatcherSensor(
        key="puissance",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value=lambda state: state.power_w,
    ),
    WatcherSensor(
        key="energie",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda state: round(state.total_energy_wh / 1000, 3),
    ),
    WatcherSensor(
        key="duree_dernier_cycle",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda state: state.last_duration,
    ),
    WatcherSensor(
        key="energie_dernier_cycle",
        # No state class: this is one cycle's total, not a running meter, and
        # Home Assistant rejects `measurement` on an energy device class.
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda state: state.last_energy,
    ),
    WatcherSensor(
        key="cycles_appris",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda state: sum(f.samples for f in state.fingerprints.values()),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        ApplianceSensor(coordinator, watcher, description)
        for description in SENSORS
        for watcher in description.watchers
    ]
    entities.append(ResidualSensor(coordinator))
    async_add_entities(entities)


class ApplianceSensor(ApplianceEntity, SensorEntity):
    """One reading off one watcher."""

    entity_description: WatcherSensor

    def __init__(self, coordinator: ApplianceCoordinator, watcher: str,
                 description: WatcherSensor) -> None:
        super().__init__(coordinator, watcher, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> object:
        return self.entity_description.value(self.watcher_state)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        state = self.watcher_state
        if self.entity_description.key == "chauffes":
            return {"chauffes_attendues": state.expected_heats}
        if self.entity_description.key != "temps_restant":
            return None
        learned = state.fingerprints.get(state.appliance)
        return {
            "duree_attendue_min": round(state.expected.total_seconds() / 60),
            "cycles_observes": learned.samples if learned else 0,
            "confiance": round(learned.confidence, 2) if learned else 0.0,
        }


class ResidualSensor(ApplianceEntity, SensorEntity):
    """House consumption that no meter accounts for — where the dishwasher hides.

    Kept as a real entity rather than an internal value: it is the one number to
    look at when a cycle is missed or invented, and it can be graphed.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, DISHWASHER, "conso_non_mesuree")

    @property
    def native_value(self) -> float | None:
        return (round(self.coordinator.residual_w, 1)
                if self.coordinator.residual_w is not None else None)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"base_ambiante_w": self.coordinator.baseline_w}
