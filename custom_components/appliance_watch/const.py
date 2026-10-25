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
# What the sky still holds today, so as not to wait for a sun that will not
# come: Forecast.Solar's remaining energy, and the house's rain flag.
CONF_FORECAST_REMAINING: Final = "forecast_remaining_entity"
CONF_RAIN: Final = "rain_entity"

# Solar router: the water heater and the pool pump, routed on the same
# smoothed grid (CONF_GRID, CONF_SOLAR above). Without the contactor or the
# pump's speed, that load is left alone.
ROUTER: Final = "routeur"
CONF_CONTACTOR: Final = "water_contactor_entity"
CONF_BOOST: Final = "water_boost_entity"
CONF_TANK_TOP: Final = "water_tank_top_entity"
CONF_TANK_MIDDLE: Final = "water_tank_middle_entity"
CONF_HEATER_POWER: Final = "water_heater_power_entity"
CONF_HEATER_ENERGY: Final = "water_heater_energy_entity"
CONF_GARAGE_TEMP: Final = "water_garage_temp_entity"
CONF_OUTDOOR_TEMP: Final = "outdoor_temp_entity"
CONF_TEMPO_COLOR: Final = "tempo_color_entity"
CONF_TEMPO_NEXT: Final = "tempo_next_color_entity"
CONF_TEMPO_HC: Final = "tempo_hc_entity"
CONF_FORECAST_TODAY: Final = "forecast_today_entity"
CONF_FORECAST_TOMORROW: Final = "forecast_tomorrow_entity"
CONF_POOL_SPEED: Final = "pool_speed_entity"
CONF_POOL_WATER_TEMP: Final = "pool_water_temp_entity"
CONF_POOL_AIR_TEMP: Final = "pool_air_temp_entity"
CONF_HEATER_NOMINAL: Final = "water_heater_nominal_w"
CONF_POOL_WARM: Final = "pool_water_warm_c"
# Daily filtration the pool still gets in winter, topped up at off-peak rates.
CONF_WINTER_MINUTES: Final = "pool_winter_daily_minutes"
# The pump's own relay (Shelly 1PM Gen4). Off means unpowered on purpose:
# the router then leaves the speed alone.
CONF_POOL_SWITCH: Final = "pool_switch_entity"
# The solar forecast integration whose hourly curve the router reads
# (Open-Meteo Solar Forecast or Forecast.Solar).
CONF_SOLAR_FORECAST_ENTRY: Final = "solar_forecast_entry"
# Measured draw of the Atlantic heat pump, not its 800 W nameplate.
DEFAULT_HEATER_NOMINAL_W: Final = 650.0

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
