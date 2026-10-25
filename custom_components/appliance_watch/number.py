"""The pool's speed, on a slider, pinned by hand."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ApplianceCoordinator
from .router_entities import router_numbers


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(router_numbers(coordinator))
