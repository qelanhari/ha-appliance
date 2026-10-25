"""Wiring of the solar router: it watches until « Pilotage » is on, then drives.

The decisions themselves are tested on the pure brains; what matters here is
that the router reads the right entities, never acts while observing, acts
once trusted, and leaves an unpowered pump alone.
"""

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from datetime import timedelta

from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from test_setup import (
    DATA,
    GRID,
    LAUNDRY,
    POOL,
    TOTAL,
    entity_id,
    power,
    setup_entry,
    thinq_washer,
    washer,
)

from custom_components.appliance_watch.const import (
    CONF_BOOST,
    CONF_CONTACTOR,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_GRID,
    CONF_HEATER_POWER,
    CONF_POOL_SPEED,
    CONF_POOL_SWITCH,
    CONF_POOL_WATER_TEMP,
    CONF_TANK_MIDDLE,
    CONF_TANK_TOP,
    CONF_TEMPO_COLOR,
    CONF_TEMPO_HC,
    DOMAIN,
)

CONTACTOR = "switch.contacteur_chauffe_eau"
BOOST = "number.boost_chauffe_eau"
TOP = "sensor.ballon_haut"
MIDDLE = "sensor.ballon_milieu"
HEATER = "sensor.ballon_puissance"
SPEED = "number.pompe_vitesse"
RELAY = "switch.pompe_piscine"
POOL_WATER = "sensor.piscine_eau"
TEMPO = "sensor.tempo_couleur"
HC = "binary_sensor.tempo_hc"
TODAY = "sensor.prevision_jour"
TOMORROW = "sensor.prevision_demain"

ROUTER = DATA | {
    CONF_GRID: GRID, CONF_CONTACTOR: CONTACTOR, CONF_BOOST: BOOST,
    CONF_TANK_TOP: TOP, CONF_TANK_MIDDLE: MIDDLE, CONF_HEATER_POWER: HEATER,
    CONF_POOL_SPEED: SPEED, CONF_POOL_SWITCH: RELAY,
    CONF_POOL_WATER_TEMP: POOL_WATER, CONF_TEMPO_COLOR: TEMPO, CONF_TEMPO_HC: HC,
    CONF_FORECAST_TODAY: TODAY, CONF_FORECAST_TOMORROW: TOMORROW,
}


def sunny_noon(hass, *, export_w: float = 2500, relay: str = "on") -> None:
    """A summer noon: big export, a tank worth heating, warm pool water."""
    power(hass, TOTAL, 900)
    power(hass, POOL, 160)
    power(hass, LAUNDRY, 2)
    power(hass, GRID, -export_w)
    power(hass, HEATER, 0)
    hass.states.async_set(CONTACTOR, "off")
    hass.states.async_set(BOOST, "0")
    hass.states.async_set(TOP, "47")
    hass.states.async_set(MIDDLE, "45")
    hass.states.async_set(SPEED, "1")
    hass.states.async_set(RELAY, relay)
    hass.states.async_set(POOL_WATER, "27")
    hass.states.async_set(TEMPO, "Bleu")
    hass.states.async_set(HC, "off")
    hass.states.async_set(TODAY, "25")
    hass.states.async_set(TOMORROW, "25")
    hass.states.async_set("sun.sun", "above_horizon")


async def mocked(hass):
    """Commands recorded, and applied to the entity as the device would.

    Registered once their components are up, which would replace them.
    """
    for component in ("switch", "number", "select"):
        assert await async_setup_component(hass, component, {})
    calls = {"turn_on": [], "turn_off": [], "set_value": []}

    def recorder(domain, service, value):
        async def handle(call):
            calls[service].append(call)
            hass.states.async_set(call.data["entity_id"], value(call))
        hass.services.async_register(domain, service, handle)

    recorder("switch", "turn_on", lambda call: "on")
    recorder("switch", "turn_off", lambda call: "off")
    recorder("number", "set_value", lambda call: str(call.data["value"]))
    return calls["turn_on"], calls["turn_off"], calls["set_value"]


async def run(hass, freezer, minutes: int = 12) -> None:
    for _ in range(minutes * 2):
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def drive(hass, entry) -> None:
    """Through the router: the switch service itself is mocked here."""
    await hass.data[DOMAIN][entry.entry_id].router.async_set_piloting(True)


@pytest.fixture
def noon(freezer):
    freezer.move_to("2026-07-15 10:00:00+00:00")
    return freezer


async def test_without_its_loads_the_router_does_not_appear(hass):
    sunny_noon(hass)
    entry = await setup_entry(hass, DATA | {CONF_GRID: GRID})
    assert entity_id(hass, entry, "routeur_pilotage") is None


async def test_it_decides_but_never_acts_while_observing(hass, noon):
    sunny_noon(hass)
    turn_on, turn_off, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await run(hass, noon)

    decision = hass.states.get(entity_id(hass, entry, "routeur_chauffe_eau_decision"))
    assert decision.state == "heat_solar"
    assert (turn_on, turn_off, set_value) == ([], [], [])


async def test_switched_on_it_drives_both_loads(hass, noon):
    sunny_noon(hass)
    turn_on, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await drive(hass, entry)
    await run(hass, noon)

    assert [call.data["entity_id"] for call in turn_on] == [CONTACTOR]
    assert any(call.data["entity_id"] == SPEED and call.data["value"] > 1
               for call in set_value)


async def test_an_unpowered_pump_is_left_alone(hass, noon):
    sunny_noon(hass, relay="off")
    _, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await drive(hass, entry)
    await run(hass, noon)

    assert not [call for call in set_value if call.data["entity_id"] == SPEED]


async def test_no_grid_reading_no_decision(hass, noon):
    sunny_noon(hass)
    hass.states.async_set(GRID, "unavailable")
    turn_on, _, _ = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await drive(hass, entry)
    await run(hass, noon)

    assert turn_on == []
    assert hass.states.get(entity_id(
        hass, entry, "routeur_chauffe_eau_decision")).state == "unknown"


async def test_piloting_survives_a_restart(hass, noon):
    sunny_noon(hass)
    await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await drive(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id(hass, entry, "routeur_pilotage")).state == "on"


async def test_the_water_heater_habits_are_taken_from_dhwp_smart(hass, hass_storage):
    legacy = MockConfigEntry(domain="dhwp_smart")
    legacy.add_to_hass(hass)
    hass_storage[f"dhwp_smart.{legacy.entry_id}"] = {
        "version": 1, "key": f"dhwp_smart.{legacy.entry_id}",
        "data": {"pattern": {"daily_kwh_ema": [1.5] * 7, "outdoor_coef": 0.1,
                             "samples_n": 159}}}
    sunny_noon(hass)
    entry = await setup_entry(hass, ROUTER)

    learned = hass.states.get(entity_id(hass, entry, "routeur_chauffe_eau_jours_appris"))
    assert learned.state == "159"


async def test_the_pump_makes_way_at_once_when_the_heater_starts(hass, noon):
    """Its 5-minute dwell is for clouds, not for a load that just arrived."""
    sunny_noon(hass, export_w=600)
    hass.states.async_set(SPEED, "2")
    _, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    router = hass.data[DOMAIN][entry.entry_id].router
    await drive(hass, entry)
    router.memory.pool.last_speed_change_at = dt_util.utcnow()
    await run(hass, noon, minutes=1)

    assert [call.data["value"] for call in set_value
            if call.data["entity_id"] == SPEED] == [1.0]


async def test_the_borrowed_watts_are_offered_only_while_driving(hass, noon):
    sunny_noon(hass, export_w=300)
    hass.states.async_set(SPEED, "2")
    await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    router = hass.data[DOMAIN][entry.entry_id].router
    await run(hass, noon, minutes=1)
    observing = router.pool_yield_w
    await drive(hass, entry)
    await run(hass, noon, minutes=1)

    assert (observing, router.pool_yield_w) == (0.0, 340.0)


async def test_the_armed_washer_counts_on_what_the_pump_borrowed(hass, noon):
    thinq_washer(hass)
    washer(hass, "initial")
    sunny_noon(hass, export_w=300)
    hass.states.async_set(SPEED, "2")
    await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await drive(hass, entry)
    await run(hass, noon, minutes=31)

    held = coordinator.solar._starter.held_export_w(dt_util.utcnow())
    assert held == pytest.approx(300.0 + 340.0)
