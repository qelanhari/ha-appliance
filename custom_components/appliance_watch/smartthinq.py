"""SmartThinQ's remote-start button: the one thing the unofficial API is used for.

LG's official integration starts a washer woken from sleep on its default
programme; SmartThinQ (HACS ``smartthinq_sensors``) sends the armed one with
the start. Everything else — status, wake-up, power — stays on the official
integration. SmartThinQ's own polling is meant to be off: it is polled here,
only when :mod:`logic.program_memory` asks, and right before a start.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

PLATFORM = "smartthinq_sensors"
REMOTE_START_SUFFIX = "-remote_start-button"
# How long a fresh poll may take to make the button pressable.
BUTTON_WAIT_S = 20


def find_remote_start(hass: HomeAssistant) -> str | None:
    """The enabled remote-start button of a SmartThinQ washer, if there is one."""
    buttons = [entity.entity_id for entity in er.async_get(hass).entities.values()
               if entity.platform == PLATFORM and entity.disabled_by is None
               and entity.unique_id.endswith(REMOTE_START_SUFFIX)]
    if len(buttons) > 1:
        _LOGGER.warning("Plusieurs boutons SmartThinQ Remote Start, %s retenu : %s",
                        buttons[0], buttons)
    return buttons[0] if buttons else None


class SmartThinqStarter:
    """Polls SmartThinQ on demand and presses its remote-start button."""

    def __init__(self, hass: HomeAssistant, button: str) -> None:
        self.hass = hass
        self.button = button

    @property
    def pressable(self) -> bool:
        """SmartThinQ holds an armed programme and offers to start it."""
        state = self.hass.states.get(self.button)
        # "unknown" is a button never pressed, not one that cannot be.
        return state is not None and state.state != "unavailable"

    async def refresh(self) -> bool:
        """One poll of the washer through SmartThinQ."""
        try:
            await self.hass.services.async_call(
                "homeassistant", "update_entity", {"entity_id": self.button},
                blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("SmartThinQ : interrogation refusée : %s", err)
            return False
        return True

    async def start(self) -> bool:
        """Start the armed programme; False when SmartThinQ cannot."""
        if not await self.refresh():
            return False
        for _ in range(BUTTON_WAIT_S):
            if self.pressable:
                break
            await asyncio.sleep(1)
        else:
            _LOGGER.warning("SmartThinQ : départ à distance indisponible après réveil")
            return False
        try:
            await self.hass.services.async_call(
                "button", "press", {"entity_id": self.button}, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("SmartThinQ : départ refusé : %s", err)
            return False
        return True


__all__ = ["SmartThinqStarter", "find_remote_start"]
