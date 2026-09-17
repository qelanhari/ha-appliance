"""Wiring: does the integration actually come up, and read the meters right?

The judgement is tested elsewhere against real traces; what matters here is
that the entry sets up, the entities appear, and the residual is computed from
live states — including the case that matters most, a measured sensor going
unavailable.
"""

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.appliance_watch.const import (  # noqa: E402
    CONF_LAUNDRY_METER,
    CONF_MEASURED,
    CONF_OPTIONAL,
    CONF_TOTAL,
    DOMAIN,
)

TOTAL = "sensor.maison"
POOL = "sensor.pompe"
CHARGER = "sensor.borne"
LAUNDRY = "sensor.garage"

DATA = {
    CONF_TOTAL: TOTAL,
    CONF_MEASURED: [POOL],
    CONF_OPTIONAL: [CHARGER],
    CONF_LAUNDRY_METER: LAUNDRY,
}


def power(hass, entity_id, value, unit="W"):
    hass.states.async_set(entity_id, value, {"unit_of_measurement": unit,
                                             "device_class": "power"})


def entity_id(hass, entry, suffix: str) -> str | None:
    """Resolve by unique_id: entity ids follow the UI language, unique ids do not."""
    registry = er.async_get(hass)
    for entry_entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entry_entity.unique_id.endswith(suffix):
            return entry_entity.entity_id
    return None


async def setup_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, title="Appliance Watch")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_appear_for_both_watchers(hass):
    power(hass, TOTAL, 1200)
    power(hass, POOL, 500)
    power(hass, LAUNDRY, 30)
    entry = await setup_entry(hass)

    assert entity_id(hass, entry, "lave_vaisselle_en_marche") is not None
    assert entity_id(hass, entry, "buanderie_en_marche") is not None
    # Only the shared meter has an appliance to guess.
    assert entity_id(hass, entry, "buanderie_appareil") is not None
    assert entity_id(hass, entry, "lave_vaisselle_appareil") is None


async def test_residual_subtracts_every_measured_load(hass):
    power(hass, TOTAL, 2000)
    power(hass, POOL, 500)
    power(hass, CHARGER, 1.0, unit="kW")  # kW: must be scaled, not summed raw
    power(hass, LAUNDRY, 100)
    entry = await setup_entry(hass)

    residual = hass.states.get(entity_id(hass, entry, "conso_non_mesuree"))
    assert residual is not None
    assert float(residual.state) == pytest.approx(400.0)  # 2000 - 500 - 1000 - 100


async def test_a_missing_measured_reading_suspends_the_residual(hass):
    """A sensor that drops out must not be read as 0 W.

    Counting it as zero would inflate the residual by whatever that appliance
    was drawing, and the dishwasher band would start matching other loads.
    """
    power(hass, TOTAL, 2000)
    hass.states.async_set(POOL, "unavailable")
    power(hass, LAUNDRY, 30)
    entry = await setup_entry(hass)

    residual = hass.states.get(entity_id(hass, entry, "conso_non_mesuree"))
    assert residual.state in ("unknown", "unavailable")


async def test_an_absent_optional_meter_is_simply_zero(hass):
    """A car charger publishes nothing while unplugged — that is not a fault."""
    power(hass, TOTAL, 2000)
    power(hass, POOL, 500)
    hass.states.async_set(CHARGER, "unavailable")
    power(hass, LAUNDRY, 100)
    entry = await setup_entry(hass)

    residual = hass.states.get(entity_id(hass, entry, "conso_non_mesuree"))
    assert float(residual.state) == pytest.approx(1400.0)


async def test_unloading_releases_the_subscriptions(hass):
    power(hass, TOTAL, 1200)
    power(hass, POOL, 500)
    power(hass, LAUNDRY, 30)
    entry = await setup_entry(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]
