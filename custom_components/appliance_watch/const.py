"""Constants and configuration accessors."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.config_entries import ConfigEntry

DOMAIN: Final = "appliance_watch"
STORAGE_VERSION: Final = 1

# Two watchers, because the two situations are not alike: one appliance nobody
# measures, and two appliances sharing a meter.
DISHWASHER: Final = "lave_vaisselle"
LAUNDRY: Final = "buanderie"
WASHER: Final = "lave_linge"
DRYER: Final = "seche_linge"

WATCHERS: Final = (DISHWASHER, LAUNDRY)
# What the laundry meter can be showing at any moment.
UNKNOWN: Final = "inconnu"
BOTH: Final = "les_deux"

CONF_TOTAL: Final = "total_entity"
CONF_MEASURED: Final = "measured_entities"
CONF_OPTIONAL: Final = "optional_entities"
CONF_LAUNDRY_METER: Final = "laundry_meter_entity"

# Detection knobs surfaced in the Options flow. Their defaults live in the
# logic dataclasses; see README.md for the traces they were measured from.
CONF_STEP_MIN: Final = "step_min_w"
CONF_STEP_MAX: Final = "step_max_w"
CONF_CLASSIFY_W: Final = "classify_w"
CONF_DISHWASHER_NOMINAL: Final = "dishwasher_nominal_minutes"
CONF_LAUNDRY_NOMINAL: Final = "laundry_nominal_minutes"

# How often the watchers re-decide without a new reading. A meter that reports
# on change falls silent exactly when a cycle ends, so the clock has to ask.
TICK_SECONDS: Final = 30


def entry_value(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Read a setting: Options first, then the original install data.

    The only way config is read in this integration — reaching into
    ``entry.data`` directly is how a value edited in Options silently stops
    being applied.
    """
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)
