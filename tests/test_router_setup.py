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
    WASHER_REMOTE,
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


async def test_an_armed_washer_owes_its_heating_before_the_heater(hass, noon):
    thinq_washer(hass)
    washer(hass, "initial")
    hass.states.async_set(WASHER_REMOTE, "on")
    sunny_noon(hass, export_w=1500)
    turn_on, _, _ = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    router = hass.data[DOMAIN][entry.entry_id].router
    await drive(hass, entry)
    await run(hass, noon, minutes=2)

    assert router.allocation.appliance_reserve_w == pytest.approx(2109.0)
    assert not [call for call in turn_on if call.data["entity_id"] == CONTACTOR]


async def test_the_pump_may_run_while_an_armed_washer_waits(hass, noon):
    """It stops in seconds: waiting for the washer is the heater's job, not its."""
    thinq_washer(hass)
    washer(hass, "initial")
    hass.states.async_set(WASHER_REMOTE, "on")
    sunny_noon(hass, export_w=1500)
    hass.states.async_set(MIDDLE, "54")
    hass.states.async_set(TOP, "54.5")
    _, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await drive(hass, entry)
    await run(hass, noon, minutes=2)

    assert any(call.data["entity_id"] == SPEED and call.data["value"] > 1
               for call in set_value)


class _EnergyPlatform:
    def __init__(self, payload):
        self.payload = payload

    async def async_get_solar_forecast(self, hass, entry_id):
        return self.payload


class _Integration:
    def __init__(self, payload):
        self.payload = payload

    async def async_get_platform(self, name):
        assert name == "energy"
        return _EnergyPlatform(self.payload)


async def test_the_curve_is_read_through_the_energy_platform(hass, noon, monkeypatch):
    import custom_components.appliance_watch.forecast_source as source_module

    source = MockConfigEntry(domain="open_meteo_solar_forecast", title="Toit")
    source.add_to_hass(hass)
    payload = {"wh_hours": {"2026-07-15T12:00:00+00:00": 2400, "2026-07-15T13:00:00+00:00": 2500,
                            "2026-07-16T12:00:00+00:00": 2400, "2026-07-16T13:00:00+00:00": 2500}}

    async def fake_integration(hass, domain):
        assert domain == "open_meteo_solar_forecast"
        return _Integration(payload)

    monkeypatch.setattr(source_module, "async_get_integration", fake_integration)
    sunny_noon(hass)
    await mocked(hass)
    entry = await setup_entry(hass, ROUTER | {"solar_forecast_entry": source.entry_id})
    await run(hass, noon, minutes=1)

    router = hass.data[DOMAIN][entry.entry_id].router
    decision = hass.states.get(entity_id(hass, entry, "routeur_chauffe_eau_decision"))
    assert len(router.forecast.curve) == 4
    assert decision.attributes["soleil_utilisable_demain_kwh"] == pytest.approx(1.3)


async def test_without_a_curve_the_totals_still_decide(hass, noon):
    sunny_noon(hass)
    await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await run(hass, noon, minutes=1)

    decision = hass.states.get(entity_id(hass, entry, "routeur_chauffe_eau_decision"))
    assert decision.attributes["soleil_utilisable_demain_kwh"] is None
    assert decision.state == "heat_solar"


async def test_a_sunless_winter_day_is_topped_up_at_the_end_of_the_night(hass, freezer):
    """No sun all day: two hours at off-peak rates, ending at 06:00."""
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-12-10 02:50:00+00:00")   # 03:50 in Paris
    sunny_noon(hass, export_w=-300)
    hass.states.async_set(SPEED, "0")
    hass.states.async_set(POOL_WATER, "12")
    hass.states.async_set(HC, "on")
    hass.states.async_set("sun.sun", "below_horizon")
    _, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await drive(hass, entry)
    await run(hass, freezer, minutes=5)
    assert not [c for c in set_value if c.data["entity_id"] == SPEED]

    await run(hass, freezer, minutes=10)                # past 04:00
    assert [c.data["value"] for c in set_value if c.data["entity_id"] == SPEED] == [1.0]


async def test_a_manual_boost_lights_the_boost_only(hass, noon):
    sunny_noon(hass, export_w=0)
    turn_on, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    router = hass.data[DOMAIN][entry.entry_id].router
    await drive(hass, entry)
    await router.async_set_water_mode("boost")
    await run(hass, noon, minutes=2)
    assert not [c for c in turn_on if c.data["entity_id"] == CONTACTOR]
    assert [c.data["value"] for c in set_value if c.data["entity_id"] == BOOST] == [1.0]

    await router.async_set_water_mode("auto")
    await run(hass, noon, minutes=2)
    assert [c.data["value"] for c in set_value if c.data["entity_id"] == BOOST] == [1.0, 0.0]


async def test_a_forced_heat_is_never_cancelled_by_the_router(hass, noon):
    sunny_noon(hass, export_w=2500)
    _, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    await drive(hass, entry)
    await hass.services.async_call("button", "press", {
        "entity_id": entity_id(hass, entry, "routeur_chauffe_forcee")}, blocking=True)
    await run(hass, noon, minutes=10)       # a solar cycle runs meanwhile
    assert [c.data["value"] for c in set_value if c.data["entity_id"] == BOOST] == [1.0]


async def test_the_router_cancels_its_own_boost_after_its_cycle(hass, noon):
    sunny_noon(hass, export_w=0)
    _, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    router = hass.data[DOMAIN][entry.entry_id].router
    await drive(hass, entry)
    router.memory.water.boost_requested = True    # left over from a solar boost
    hass.states.async_set(BOOST, "1")
    await run(hass, noon, minutes=1)
    assert [c.data["value"] for c in set_value if c.data["entity_id"] == BOOST] == [0.0]


async def test_a_skim_launched_by_hand_goes_straight_to_v3(hass, noon):
    sunny_noon(hass, export_w=2500)
    hass.states.async_set(MIDDLE, "54")
    hass.states.async_set(TOP, "54.5")
    _, _, set_value = await mocked(hass)
    entry = await setup_entry(hass, ROUTER)
    router = hass.data[DOMAIN][entry.entry_id].router
    await drive(hass, entry)
    router.memory.pool.last_speed_change_at = dt_util.utcnow()
    router.memory.pool.v3_last_ended_at = dt_util.utcnow()
    await hass.services.async_call("button", "press", {
        "entity_id": entity_id(hass, entry, "routeur_lancer_ecumage")}, blocking=True)
    await hass.async_block_till_done()
    assert [c.data["value"] for c in set_value if c.data["entity_id"] == SPEED][:1] == [3.0]
