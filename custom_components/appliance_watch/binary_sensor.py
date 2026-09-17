"""One binary sensor per watcher: is a cycle running?

This is the entity the Live Activity automations trigger on — a discrete
transition, never the power sensor itself, which changes several times a minute
and would have iOS throttle the activity away.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, WATCHERS
from .coordinator import ApplianceCoordinator
from .entity import ApplianceEntity
from .logic.detector import Phase


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(RunningBinarySensor(coordinator, watcher)
                       for watcher in WATCHERS)


class RunningBinarySensor(ApplianceEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: ApplianceCoordinator, watcher: str) -> None:
        super().__init__(coordinator, watcher, "en_marche")

    @property
    def is_on(self) -> bool:
        return self.watcher_state.phase is Phase.RUNNING

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.watcher_state
        return {
            "appareil": state.appliance,
            "debut": state.started_at.isoformat() if state.started_at else None,
            "duree_attendue_min": round(state.expected.total_seconds() / 60),
        }
