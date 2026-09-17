"""Install wizard and Options.

The install step asks only what cannot be guessed: which sensor totals the
house, which ones are already metered (so they can be subtracted), and which
meter the laundry room shares. Detection thresholds are deliberately *not*
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
    CONF_CLASSIFY_W,
    CONF_DISHWASHER_NOMINAL,
    CONF_LAUNDRY_METER,
    CONF_LAUNDRY_NOMINAL,
    CONF_MEASURED,
    CONF_OPTIONAL,
    CONF_STEP_MAX,
    CONF_STEP_MIN,
    CONF_TOTAL,
    DOMAIN,
    entry_value,
)
from .logic.detector import PlateauConfig, SharedMeterConfig

POWER_SENSOR = selector.EntitySelectorConfig(domain="sensor", device_class="power")


def _install_schema() -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_TOTAL): selector.EntitySelector(POWER_SENSOR),
        vol.Required(CONF_MEASURED): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power",
                                          multiple=True)),
        vol.Optional(CONF_OPTIONAL, default=[]): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", multiple=True)),
        vol.Required(CONF_LAUNDRY_METER): selector.EntitySelector(POWER_SENSOR),
    })


def _number(minimum: float, maximum: float, step: float = 1,
            unit: str | None = None) -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, unit_of_measurement=unit,
        mode=selector.NumberSelectorMode.BOX))


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
    """Detection thresholds. ``self.config_entry`` is injected by Home Assistant."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        entry = self.config_entry
        plateau, shared = PlateauConfig(), SharedMeterConfig()
        schema = vol.Schema({
            vol.Required(CONF_STEP_MIN, default=entry_value(
                entry, CONF_STEP_MIN, plateau.step_min_w)): _number(500, 4000, 10, "W"),
            vol.Required(CONF_STEP_MAX, default=entry_value(
                entry, CONF_STEP_MAX, plateau.step_max_w)): _number(500, 4000, 10, "W"),
            vol.Required(CONF_DISHWASHER_NOMINAL, default=entry_value(
                entry, CONF_DISHWASHER_NOMINAL,
                plateau.nominal.total_seconds() / 60)): _number(10, 300, 1, "min"),
            vol.Required(CONF_CLASSIFY_W, default=entry_value(
                entry, CONF_CLASSIFY_W, shared.classify_w)): _number(100, 3000, 10, "W"),
            vol.Required(CONF_LAUNDRY_NOMINAL, default=entry_value(
                entry, CONF_LAUNDRY_NOMINAL,
                shared.nominal.total_seconds() / 60)): _number(10, 300, 1, "min"),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
