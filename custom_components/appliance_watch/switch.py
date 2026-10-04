"""Whether the sun may start the washer once it is armed for remote start."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, WASHER
from .coordinator import ApplianceCoordinator
from .entity import ApplianceEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SolarStartSwitch(coordinator)])


class SolarStartSwitch(ApplianceEntity, SwitchEntity):
    """On by default: arming the machine for remote start is already the ask.

    Kept across restarts — a switch turned off for the holidays stays off.
    """

    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, WASHER, "depart_solaire")

    @property
    def is_on(self) -> bool:
        return self.coordinator.solar.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_solar_start(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_solar_start(False)
