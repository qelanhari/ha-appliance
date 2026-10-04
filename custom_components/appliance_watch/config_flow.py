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
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DISHWASHER_NOMINAL,
    CONF_DRYER_NOMINAL,
    CONF_DRYER_OFF_DELAY,
    CONF_FORECAST_NEXT_HOUR,
    CONF_FORECAST_NOW,
    CONF_FORECAST_PEAK,
    CONF_GOOD_SAVING,
    CONF_GRID,
    CONF_LAUNDRY_METER,
    CONF_MEASURED,
    CONF_MIN_SAVING,
    CONF_OPTIONAL,
    CONF_PRICE,
    CONF_SOLAR,
    CONF_STEP_MAX,
    CONF_STEP_MIN,
    CONF_TOTAL,
    CONF_WASHER_STATUS,
    DOMAIN,
    entry_value,
)
from .logic.detector import DryerConfig, PlateauConfig
from .logic.solar_start import SolarStartConfig

POWER_SENSOR = selector.EntitySelectorConfig(domain="sensor", device_class="power")
ANY_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
# Its remaining-time and total-time siblings are found on the same device.
WASHER_STATUS = selector.EntitySelector(selector.EntitySelectorConfig(
    domain="sensor", integration="lg_thinq", device_class="enum"))


def _install_schema() -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_TOTAL): selector.EntitySelector(POWER_SENSOR),
        vol.Required(CONF_MEASURED): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power",
                                          multiple=True)),
        vol.Optional(CONF_OPTIONAL, default=[]): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", multiple=True)),
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
    """The washer's sensor and the detection thresholds.

    ``self.config_entry`` is injected by Home Assistant. The washer's sensor is
    here too, because an entry installed before 0.5 has none to start with.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        entry = self.config_entry
        plateau, dryer, solar = PlateauConfig(), DryerConfig(), SolarStartConfig()
        schema = vol.Schema({
            vol.Required(CONF_WASHER_STATUS, description={
                "suggested_value": entry_value(entry, CONF_WASHER_STATUS)}): WASHER_STATUS,
            **_optional_entity(entry, CONF_GRID, selector.EntitySelector(POWER_SENSOR)),
            **_optional_entity(entry, CONF_SOLAR, selector.EntitySelector(POWER_SENSOR)),
            **_optional_entity(entry, CONF_PRICE, ANY_SENSOR),
            **_optional_entity(entry, CONF_FORECAST_NOW, ANY_SENSOR),
            **_optional_entity(entry, CONF_FORECAST_NEXT_HOUR, ANY_SENSOR),
            **_optional_entity(entry, CONF_FORECAST_PEAK, ANY_SENSOR),
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
