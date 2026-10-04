"""Home Assistant side of the washer's solar start: inputs in, one command out.

The decision is :mod:`logic.solar_start`'s; this reads the grid meter, the
price (for the euros shown) and the forecast, and presses "start" through ThinQ's operation select.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FORECAST_NEXT_HOUR,
    CONF_FORECAST_NOW,
    CONF_FORECAST_PEAK,
    CONF_GOOD_SAVING,
    CONF_GRID,
    CONF_MIN_SAVING,
    CONF_PRICE,
    CONF_SOLAR,
    DOMAIN,
    entry_value,
)
from .logic.residual import to_watts
from .logic.solar_start import Decision, Forecast, SolarStartConfig, SolarStarter

_LOGGER = logging.getLogger(__name__)

EVENT_SOLAR_START = f"{DOMAIN}_solar_start"
START_OPTION = "start"

# Reads one entity's state as a float, or None when it has nothing to say.
Reader = Callable[[str | None], Any]


def solar_config(entry: ConfigEntry) -> SolarStartConfig:
    base = SolarStartConfig()
    return SolarStartConfig(
        good_saving=float(entry_value(entry, CONF_GOOD_SAVING, base.good_saving * 100)) / 100,
        min_saving=float(entry_value(entry, CONF_MIN_SAVING, base.min_saving * 100)) / 100,
    )


class WasherSolarStart:
    """Starts the armed washer when the sun makes the cycle cheap enough."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, state: Reader) -> None:
        self.hass = hass
        self.entry = entry
        self._state = state
        self._starter = SolarStarter(solar_config(entry))
        self.enabled = True
        self.decision = Decision(waiting=False, estimate=None)
        self.remote: str | None = None
        self.operation: str | None = None

    @property
    def configured(self) -> bool:
        return bool(entry_value(self.entry, CONF_GRID)) and bool(self.operation)

    def _watts(self, key: str) -> float | None:
        state = self._state(entry_value(self.entry, key))
        if state is None:
            return None
        try:
            return to_watts(float(state.state), state.attributes.get("unit_of_measurement"))
        except ValueError:
            return None

    def _peak_at(self) -> datetime | None:
        state = self._state(entry_value(self.entry, CONF_FORECAST_PEAK))
        return dt_util.parse_datetime(state.state) if state else None

    def _float(self, key: str) -> float | None:
        state = self._state(entry_value(self.entry, key))
        try:
            return float(state.state) if state else None
        except ValueError:
            return None

    def _armed(self) -> bool | None:
        state = self._state(self.remote)
        return None if state is None else state.state == "on"

    def _forecast(self) -> Forecast | None:
        now_w = self._float(CONF_FORECAST_NOW)
        next_kwh = self._float(CONF_FORECAST_NEXT_HOUR)
        if now_w is None or next_kwh is None:
            return None
        return Forecast(now_w=now_w, next_hour_w=next_kwh * 1000)

    async def process(self, now: datetime, *, washer_running: bool | None,
                      total: timedelta | None) -> None:
        """Feed the grid reading, decide, and press start if it is time."""
        if not self.configured:
            return
        grid = self._watts(CONF_GRID)
        if grid is not None:
            self._starter.feed_grid(now, grid)
        pv = self._watts(CONF_SOLAR)
        if pv is not None:
            self._starter.feed_pv(now, pv)
        self.decision = self._starter.decide(
            now, armed=self._armed(), washer_running=washer_running,
            enabled=self.enabled, total=total, price=self._float(CONF_PRICE),
            forecast=self._forecast(), peak_at=self._peak_at())
        if self.decision.start:
            await self._press_start()

    async def _press_start(self) -> None:
        estimate = self.decision.estimate
        try:
            await self.hass.services.async_call(
                "select", "select_option",
                {"entity_id": self.operation, "option": START_OPTION}, blocking=True)
        except HomeAssistantError as err:
            # Asked again after retry_after: the cloud may simply have dropped it.
            _LOGGER.warning("Démarrage solaire du lave-linge refusé : %s", err)
            return
        _LOGGER.info("Lave-linge démarré au soleil — %s (%s)",
                     self.decision.reason, estimate)
        self.hass.bus.async_fire(EVENT_SOLAR_START, {
            "raison": self.decision.reason,
            "surplus_w": round(estimate.surplus_w) if estimate else None,
            "economie_pct": round(estimate.saving * 100) if estimate else None,
            "cout_estime_eur": estimate.cost_eur if estimate else None,
        })


__all__ = ["EVENT_SOLAR_START", "WasherSolarStart", "solar_config"]
