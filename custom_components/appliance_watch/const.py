"""Constants and configuration accessors."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.config_entries import ConfigEntry

DOMAIN: Final = "appliance_watch"
STORAGE_VERSION: Final = 1

# Three watchers, one per machine: the dishwasher nobody measures, the washer
# that reports itself through LG ThinQ, and the dryer read off the meter it
# shares with the washer.
DISHWASHER: Final = "lave_vaisselle"
WASHER: Final = "lave_linge"
DRYER: Final = "seche_linge"

WATCHERS: Final = (DISHWASHER, WASHER, DRYER)
# Before 0.5 the washer and the dryer were one watcher; its store key is read
# once to carry the dryer's learned rhythm over.
LEGACY_LAUNDRY: Final = "buanderie"

CONF_TOTAL: Final = "total_entity"
CONF_MEASURED: Final = "measured_entities"
CONF_OPTIONAL: Final = "optional_entities"
CONF_LAUNDRY_METER: Final = "laundry_meter_entity"
# The washer's ThinQ "current status" sensor. Its remaining-time and total-time
# siblings are found on the same device.
CONF_WASHER_STATUS: Final = "washer_status_entity"

# Solar start of the washer, once armed for remote start. The grid meter is
# signed (negative is export); without it the feature stays off.
CONF_GRID: Final = "grid_entity"
CONF_SOLAR: Final = "solar_entity"
CONF_FORECAST_PEAK: Final = "forecast_peak_entity"
CONF_GOOD_SAVING: Final = "solar_good_saving_pct"
CONF_PRICE: Final = "price_entity"
CONF_FORECAST_NOW: Final = "forecast_now_entity"
CONF_FORECAST_NEXT_HOUR: Final = "forecast_next_hour_entity"
CONF_MIN_SAVING: Final = "solar_min_saving_pct"

# Detection knobs surfaced in the Options flow. Their defaults live in the
# logic dataclasses; see README.md for the traces they were measured from.
CONF_STEP_MIN: Final = "step_min_w"
CONF_STEP_MAX: Final = "step_max_w"
CONF_DISHWASHER_NOMINAL: Final = "dishwasher_nominal_minutes"
CONF_DRYER_NOMINAL: Final = "dryer_nominal_minutes"
CONF_DRYER_OFF_DELAY: Final = "dryer_off_delay_minutes"

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
