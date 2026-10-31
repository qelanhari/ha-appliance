"""Shared entity base: one device per watcher, names from translations."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DISHWASHER, DOMAIN, DRYER, ROUTER, WASHER
from .coordinator import ApplianceCoordinator, WatcherState

# How often a noisy entity may write. The recorder keeps every write whose
# state or attributes changed: at every meter reading, that was 20 rows a
# minute for the residual alone.
QUIET_WRITE_S = 60

DEVICE_NAMES = {DISHWASHER: "Lave-vaisselle", WASHER: "Lave-linge", DRYER: "Sèche-linge",
                ROUTER: "Routeur solaire"}
MODELS = {WASHER: "Suivi par LG ThinQ", ROUTER: "Chauffe-eau et pompe piscine"}


class ApplianceEntity(CoordinatorEntity[ApplianceCoordinator]):
    """A view on one watcher — entities hold no state of their own."""

    _attr_has_entity_name = True
    # Seconds between two writes while only noise changes; 0 writes every refresh.
    _min_write_s: float = 0

    def __init__(self, coordinator: ApplianceCoordinator, watcher: str,
                 key: str) -> None:
        super().__init__(coordinator)
        self._watcher = watcher
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{watcher}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_{watcher}")},
            name=DEVICE_NAMES.get(watcher, watcher.replace("_", " ").title()),
            manufacturer="Home Energy Pilot",
            model=MODELS.get(watcher, "Détection par la consommation"),
        )
        self._written_at: datetime | None = None
        self._written_milestone: object = None

    @property
    def watcher_state(self) -> WatcherState:
        return self.coordinator.states[self._watcher]

    def _milestone(self) -> object:
        """What must be written at once, however soon after the last write."""
        return self.available

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._min_write_s and not self._write_due():
            return
        super()._handle_coordinator_update()

    def _write_due(self) -> bool:
        now, milestone = dt_util.utcnow(), self._milestone()
        if (self._written_at is not None and milestone == self._written_milestone
                and (now - self._written_at).total_seconds() < self._min_write_s):
            return False
        self._written_at, self._written_milestone = now, milestone
        return True
