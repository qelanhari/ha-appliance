"""The hourly solar curve, read from whichever forecast integration is chosen.

Forecast.Solar and Open-Meteo Solar Forecast both serve the Energy dashboard
through the same platform call, ``energy.async_get_solar_forecast``: that is
read here, so either works and nothing depends on their internals.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import dt as dt_util

from .const import CONF_SOLAR_FORECAST_ENTRY, entry_value
from .logic.solar_curve import Curve, parse_wh_hours

_LOGGER = logging.getLogger(__name__)

# The sources refresh hourly at best; asking more often only costs.
REFRESH = timedelta(minutes=15)
FORECAST_DOMAINS = ("open_meteo_solar_forecast", "forecast_solar")


class SolarForecastSource:
    """Keeps the latest curve of the configured forecast entry, in local time."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.curve: Curve = {}
        self._fetched_at: datetime | None = None

    @property
    def entry_id(self) -> str | None:
        return entry_value(self.entry, CONF_SOLAR_FORECAST_ENTRY) or None

    async def refresh(self, now: datetime) -> Curve:
        """The curve, fetched again once it is older than the refresh period."""
        if self._fetched_at is not None and now - self._fetched_at < REFRESH:
            return self.curve
        self._fetched_at = now
        source = self.hass.config_entries.async_get_entry(self.entry_id or "")
        if source is None:
            self.curve = {}
            return self.curve
        try:
            integration = await async_get_integration(self.hass, source.domain)
            platform = await integration.async_get_platform("energy")
            payload = await platform.async_get_solar_forecast(self.hass, source.entry_id)
        except (HomeAssistantError, IntegrationNotFound, ImportError,
                AttributeError, KeyError, ValueError) as err:
            # A curve that cannot be read falls back to the daily totals.
            _LOGGER.warning("Routeur : courbe solaire illisible (%s) : %s", source.domain, err)
            return self.curve
        curve = parse_wh_hours((payload or {}).get("wh_hours"))
        self.curve = {dt_util.as_local(hour): wh for hour, wh in curve.items()}
        return self.curve


__all__ = ["FORECAST_DOMAINS", "SolarForecastSource"]
