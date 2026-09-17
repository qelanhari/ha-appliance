"""A way to throw away what was learned, when a habit changes for good."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, LAUNDRY
from .coordinator import ApplianceCoordinator
from .entity import ApplianceEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ForgetFingerprintsButton(coordinator)])


class ForgetFingerprintsButton(ApplianceEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, LAUNDRY, "oublier_empreintes")

    async def async_press(self) -> None:
        await self.coordinator.async_forget_fingerprints()
