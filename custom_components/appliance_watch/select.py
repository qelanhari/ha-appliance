"""The router's modes: the water heater's and the pool pump's, by hand."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ApplianceCoordinator
from .router_entities import PoolModeSelect, WaterModeSelect


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []
    if coordinator.router.configured:
        entities = [WaterModeSelect(coordinator), PoolModeSelect(coordinator)]
    async_add_entities(entities)
