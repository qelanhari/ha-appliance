"""Install wizard and Options.

The install step asks only what cannot be guessed: which sensor totals the
house, which ones are already metered (so they can be subtracted), which meter
the laundry room shares, and which LG ThinQ sensor reports the washer. Detection thresholds are deliberately *not*
asked for at install — they have sane measured defaults, and they belong in
Options where changing one is a deliberate act.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BOOST,
    CONF_CONTACTOR,
    CONF_DISHWASHER_NOMINAL,
    CONF_DRYER_NOMINAL,
    CONF_DRYER_OFF_DELAY,
    CONF_FORECAST_NEXT_HOUR,
    CONF_FORECAST_NOW,
    CONF_FORECAST_PEAK,
    CONF_FORECAST_REMAINING,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_GARAGE_TEMP,
    CONF_GOOD_SAVING,
    CONF_GRID,
    CONF_HEATER_ENERGY,
    CONF_HEATER_NOMINAL,
    CONF_HEATER_POWER,
    CONF_LAUNDRY_METER,
    CONF_MEASURED,
    CONF_MIN_SAVING,
    CONF_OPTIONAL,
    CONF_OUTDOOR_TEMP,
    CONF_POOL_AIR_TEMP,
    CONF_POOL_SPEED,
    CONF_POOL_SWITCH,
    CONF_POOL_WARM,
    CONF_POOL_WATER_TEMP,
    CONF_PRICE,
    CONF_RAIN,
    CONF_SOLAR,
    CONF_SOLAR_FORECAST_ENTRY,
    CONF_STEP_MAX,
    CONF_STEP_MIN,
    CONF_TANK_MIDDLE,
    CONF_TANK_TOP,
    CONF_TEMPO_COLOR,
    CONF_TEMPO_HC,
    CONF_TEMPO_NEXT,
    CONF_TOTAL,
    CONF_WASHER_STATUS,
    DEFAULT_HEATER_NOMINAL_W,
    DOMAIN,
    entry_value,
)
from .forecast_source import FORECAST_DOMAINS
from .logic.detector import DryerConfig, PlateauConfig
from .logic.loads.pool import Thresholds as PoolThresholds
from .logic.solar_start import SolarStartConfig

POWER_SENSOR = selector.EntitySelectorConfig(domain="sensor", device_class="power")
MEASURED = selector.EntitySelector(selector.EntitySelectorConfig(
    domain="sensor", device_class="power", multiple=True))
OPTIONAL = selector.EntitySelector(selector.EntitySelectorConfig(
    domain="sensor", multiple=True))
ANY_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
RAIN = selector.EntitySelector(selector.EntitySelectorConfig(
    domain=["binary_sensor", "input_boolean"]))
SWITCH = selector.EntitySelector(selector.EntitySelectorConfig(domain="switch"))
NUMBER = selector.EntitySelector(selector.EntitySelectorConfig(domain="number"))
BINARY = selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor"))
TEMPERATURE = selector.EntitySelector(selector.EntitySelectorConfig(
    domain="sensor", device_class="temperature"))
ENERGY = selector.EntitySelector(selector.EntitySelectorConfig(
    domain="sensor", device_class="energy"))
# Its remaining-time and total-time siblings are found on the same device.
WASHER_STATUS = selector.EntitySelector(selector.EntitySelectorConfig(
    domain="sensor", integration="lg_thinq", device_class="enum"))


def _install_schema() -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_TOTAL): selector.EntitySelector(POWER_SENSOR),
        vol.Required(CONF_MEASURED): MEASURED,
        vol.Optional(CONF_OPTIONAL, default=[]): OPTIONAL,
        vol.Required(CONF_LAUNDRY_METER): selector.EntitySelector(POWER_SENSOR),
        vol.Required(CONF_WASHER_STATUS): WASHER_STATUS,
    })


def _number(minimum: float, maximum: float, step: float = 1,
            unit: str | None = None) -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, unit_of_measurement=unit,
        mode=selector.NumberSelectorMode.BOX))


def _optional_entity(entry: ConfigEntry, key: str,
                     chooser: selector.EntitySelector) -> dict:
    """An entity that may be left empty — which switches its feature off."""
    return {vol.Optional(key, description={
        "suggested_value": entry_value(entry, key)}): chooser}


class ApplianceWatchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Install wizard."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_install_schema())
        return self.async_create_entry(title="Appliance Watch", data=user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ApplianceWatchOptionsFlow:
        return ApplianceWatchOptionsFlow()


class ApplianceWatchOptionsFlow(OptionsFlow):
    """The metered loads, the washer's sensor and the detection thresholds.

    ``self.config_entry`` is injected by Home Assistant. The washer's sensor is
    here too, because an entry installed before 0.5 has none to start with; the
    metered loads, because a new metered appliance must be subtracted too.
    """

    def __init__(self) -> None:
        self._appliances: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        if user_input is not None:
            self._appliances = user_input
            return await self.async_step_routeur()

        entry = self.config_entry
        plateau, dryer, solar = PlateauConfig(), DryerConfig(), SolarStartConfig()
        schema = vol.Schema({
            vol.Required(CONF_MEASURED, default=entry_value(
                entry, CONF_MEASURED, [])): MEASURED,
            vol.Optional(CONF_OPTIONAL, default=entry_value(
                entry, CONF_OPTIONAL, [])): OPTIONAL,
            vol.Required(CONF_WASHER_STATUS, description={
                "suggested_value": entry_value(entry, CONF_WASHER_STATUS)}): WASHER_STATUS,
            **_optional_entity(entry, CONF_GRID, selector.EntitySelector(POWER_SENSOR)),
            **_optional_entity(entry, CONF_SOLAR, selector.EntitySelector(POWER_SENSOR)),
            **_optional_entity(entry, CONF_PRICE, ANY_SENSOR),
            **_optional_entity(entry, CONF_FORECAST_NOW, ANY_SENSOR),
            **_optional_entity(entry, CONF_FORECAST_NEXT_HOUR, ANY_SENSOR),
            **_optional_entity(entry, CONF_FORECAST_PEAK, ANY_SENSOR),
            **_optional_entity(entry, CONF_FORECAST_REMAINING, ANY_SENSOR),
            **_optional_entity(entry, CONF_RAIN, RAIN),
            vol.Required(CONF_GOOD_SAVING, default=entry_value(
                entry, CONF_GOOD_SAVING, solar.good_saving * 100)): _number(0, 100, 5, "%"),
            vol.Required(CONF_MIN_SAVING, default=entry_value(
                entry, CONF_MIN_SAVING, solar.min_saving * 100)): _number(0, 100, 5, "%"),
            vol.Required(CONF_STEP_MIN, default=entry_value(
                entry, CONF_STEP_MIN, plateau.step_min_w)): _number(500, 4000, 10, "W"),
            vol.Required(CONF_STEP_MAX, default=entry_value(
                entry, CONF_STEP_MAX, plateau.step_max_w)): _number(500, 4000, 10, "W"),
            vol.Required(CONF_DISHWASHER_NOMINAL, default=entry_value(
                entry, CONF_DISHWASHER_NOMINAL,
                plateau.nominal.total_seconds() / 60)): _number(10, 300, 1, "min"),
            vol.Required(CONF_DRYER_NOMINAL, default=entry_value(
                entry, CONF_DRYER_NOMINAL,
                dryer.nominal.total_seconds() / 60)): _number(10, 300, 1, "min"),
            vol.Required(CONF_DRYER_OFF_DELAY, default=entry_value(
                entry, CONF_DRYER_OFF_DELAY,
                dryer.off_delay.total_seconds() / 60)): _number(1, 30, 1, "min"),
        })
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_routeur(self, user_input: dict[str, Any] | None = None
                                 ) -> ConfigFlowResult:
        """The solar router: left empty, the water heater and pump are untouched."""
        if user_input is not None:
            return self.async_create_entry(data={**self._appliances, **user_input})

        entry = self.config_entry
        entities = (
            (CONF_CONTACTOR, SWITCH), (CONF_BOOST, NUMBER),
            (CONF_TANK_TOP, TEMPERATURE), (CONF_TANK_MIDDLE, TEMPERATURE),
            (CONF_HEATER_POWER, selector.EntitySelector(POWER_SENSOR)),
            (CONF_HEATER_ENERGY, ENERGY), (CONF_GARAGE_TEMP, TEMPERATURE),
            (CONF_OUTDOOR_TEMP, TEMPERATURE), (CONF_TEMPO_COLOR, ANY_SENSOR),
            (CONF_TEMPO_NEXT, ANY_SENSOR), (CONF_TEMPO_HC, BINARY),
            (CONF_FORECAST_TODAY, ANY_SENSOR), (CONF_FORECAST_TOMORROW, ANY_SENSOR),
            (CONF_POOL_SPEED, NUMBER), (CONF_POOL_SWITCH, SWITCH),
            (CONF_POOL_WATER_TEMP, TEMPERATURE), (CONF_POOL_AIR_TEMP, TEMPERATURE),
        )
        schema: dict[Any, Any] = {}
        sources = [selector.SelectOptionDict(value=source.entry_id,
                                             label=f"{source.title} ({source.domain})")
                   for domain in FORECAST_DOMAINS
                   for source in self.hass.config_entries.async_entries(domain)]
        if sources:
            schema.update(_optional_entity(entry, CONF_SOLAR_FORECAST_ENTRY,
                                           selector.SelectSelector(selector.SelectSelectorConfig(
                                               options=sources))))
        for key, chooser in entities:
            schema.update(_optional_entity(entry, key, chooser))
        schema[vol.Required(CONF_HEATER_NOMINAL, default=entry_value(
            entry, CONF_HEATER_NOMINAL, DEFAULT_HEATER_NOMINAL_W))] = _number(200, 3000, 10, "W")
        schema[vol.Required(CONF_POOL_WARM, default=entry_value(
            entry, CONF_POOL_WARM, PoolThresholds().water_warm_c))] = _number(15, 35, 0.5, "°C")
        return self.async_show_form(step_id="routeur", data_schema=vol.Schema(schema))
