"""Home Assistant side of the washer's solar start: inputs in, one command out.

The decision is :mod:`logic.solar_start`'s; this reads the grid meter, the
price (for the euros shown) and the forecast, wakes the machine through
ThinQ's operation select if it dozed off, and starts it — through SmartThinQ's
remote-start button when there is one (:mod:`.smartthinq`), since ThinQ's own
"start" runs the default programme after a sleep.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
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
    CONF_FORECAST_REMAINING,
    CONF_FORECAST_TOMORROW,
    CONF_GOOD_SAVING,
    CONF_GRID,
    CONF_MIN_SAVING,
    CONF_PRICE,
    CONF_RAIN,
    CONF_SOLAR,
    CONF_TEMPO_COLOR,
    CONF_TEMPO_HC,
    DOMAIN,
    entry_value,
)
from .logic.program_memory import ProgramMemory, Refresh
from .logic.residual import to_watts
from .logic.solar_curve import Curve
from .logic.solar_start import Decision, Forecast, SolarStartConfig, SolarStarter
from .logic.sun_outlook import ForecastNow, Outlook
from .smartthinq import SmartThinqStarter

_LOGGER = logging.getLogger(__name__)

EVENT_SOLAR_START = f"{DOMAIN}_solar_start"
SUN = "sun.sun"
START_OPTION = "start"
# ThinQ statuses. Armed for remote start, the washer dozes off: woken before
# it is started.
ASLEEP = "sleep"
IDLE = "initial"
WAKE_OPTION = "wake_up"

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
        # Starts the armed programme; None falls back to ThinQ's "start".
        self.smartthinq: SmartThinqStarter | None = None
        self._memory = ProgramMemory()

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

    def _is_on(self, key: str) -> bool | None:
        state = self._state(entry_value(self.entry, key))
        return None if state is None else state.state == "on"

    def _sky(self, forecast: Forecast | None) -> ForecastNow:
        """Forecast.Solar's view of the rest of the day, and whether it rains."""
        sun = self.hass.states.get(SUN)
        setting = sun.attributes.get("next_setting") if sun else None
        return ForecastNow(
            now_w=forecast.now_w if forecast else self._float(CONF_FORECAST_NOW),
            next_hour_kwh=self._float(CONF_FORECAST_NEXT_HOUR),
            remaining_today_kwh=self._float(CONF_FORECAST_REMAINING),
            tomorrow_kwh=self._float(CONF_FORECAST_TOMORROW),
            peak_at=self._peak_at(),
            sunset_at=dt_util.parse_datetime(setting) if setting else None,
            raining=self._is_on(CONF_RAIN),
        )

    def _expensive_now(self) -> bool:
        """A Rouge peak hour: a cycle on the grid waits for the off-peak."""
        colour = self._state(entry_value(self.entry, CONF_TEMPO_COLOR))
        is_red = colour is not None and colour.state.strip().lower() == "rouge"
        return is_red and self._is_on(CONF_TEMPO_HC) is not True

    @property
    def outlook(self) -> Outlook | None:
        return self._starter.outlook

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
                      status: str | None, total: timedelta | None,
                      yield_w: float = 0.0, curve: Curve | None = None,
                      bias: float = 1.0) -> None:
        """Feed the grid reading, decide, and wake or start if it is time.

        ``yield_w`` is what the pool pump would give back by dropping to v1:
        surplus the washer outranks it for.
        """
        if not self.configured:
            return
        grid = self._watts(CONF_GRID)
        if grid is not None:
            self._starter.feed_grid(now, grid - yield_w)
        pv = self._watts(CONF_SOLAR)
        if pv is not None:
            self._starter.feed_pv(now, pv)
        armed, asleep = self._armed(), status == ASLEEP
        await self._sync_program(now, armed, asleep, status == IDLE)
        forecast = self._forecast()
        self.decision = self._starter.decide(
            now, armed=armed, washer_running=washer_running,
            enabled=self.enabled, asleep=asleep, total=total, price=self._float(CONF_PRICE),
            forecast=forecast, peak_at=self._peak_at(),
            sky=replace(self._sky(forecast), curve=curve or None, bias=bias),
            expensive_now=self._expensive_now(),
            program_ready=self.smartthinq is None or self._memory.ready)
        if self.decision.wake:
            await self._press(WAKE_OPTION)
        elif self.decision.start and await self._start():
            self._announce_start()

    async def _sync_program(self, now: datetime, armed: bool | None,
                            asleep: bool, idle: bool) -> None:
        """Poll SmartThinQ when the armed programme is to be remembered or forgotten."""
        if self.smartthinq is None:
            return
        refresh = self._memory.observe(now, armed=armed, asleep=asleep, idle=idle,
                                       wanted=self.enabled)
        if refresh is None or not await self.smartthinq.refresh():
            return
        if refresh is Refresh.FORGET:
            self._memory.forgotten()
        elif self.smartthinq.pressable:
            self._memory.remembered()
            _LOGGER.info("Lave-linge : programme armé mémorisé par SmartThinQ")
        else:
            _LOGGER.warning("Lave-linge : SmartThinQ ne propose pas le départ à distance")

    async def _start(self) -> bool:
        if self.smartthinq is None:
            return await self._press(START_OPTION)
        return await self.smartthinq.start()

    async def _press(self, option: str) -> bool:
        try:
            await self.hass.services.async_call(
                "select", "select_option",
                {"entity_id": self.operation, "option": option}, blocking=True)
        except HomeAssistantError as err:
            # Asked again after retry_after: the cloud may simply have dropped it.
            _LOGGER.warning("Lave-linge : commande « %s » refusée : %s", option, err)
            return False
        return True

    def _announce_start(self) -> None:
        estimate = self.decision.estimate
        _LOGGER.info("Lave-linge démarré au soleil — %s (%s)",
                     self.decision.reason, estimate)
        self.hass.bus.async_fire(EVENT_SOLAR_START, {
            "raison": self.decision.reason,
            "surplus_w": round(estimate.surplus_w) if estimate else None,
            "economie_pct": round(estimate.saving * 100) if estimate else None,
            "cout_estime_eur": estimate.cost_eur if estimate else None,
        })


__all__ = ["EVENT_SOLAR_START", "WasherSolarStart", "solar_config"]
