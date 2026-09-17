"""Shared entity base: one device per watcher, names from translations."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DISHWASHER, DOMAIN
from .coordinator import ApplianceCoordinator, WatcherState

DEVICE_NAMES = {DISHWASHER: "Lave-vaisselle", "buanderie": "Buanderie"}


class ApplianceEntity(CoordinatorEntity[ApplianceCoordinator]):
    """A view on one watcher — entities hold no state of their own."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ApplianceCoordinator, watcher: str,
                 key: str) -> None:
        super().__init__(coordinator)
        self._watcher = watcher
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{watcher}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_{watcher}")},
            name=DEVICE_NAMES.get(watcher, watcher.replace("_", " ").title()),
            manufacturer="Appliance Watch",
            model="Détection par la consommation",
        )

    @property
    def watcher_state(self) -> WatcherState:
        return self.coordinator.states[self._watcher]
