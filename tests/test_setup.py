"""Wiring: does the integration actually come up, and read the meters right?

The judgement is tested elsewhere against real traces; what matters here is
that the entry sets up, the entities appear, and the residual is computed from
live states — including the case that matters most, a measured sensor going
unavailable.
"""

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from datetime import timedelta  # noqa: E402

from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.appliance_watch.const import (  # noqa: E402
    CONF_FORECAST_PEAK,
    CONF_GRID,
    CONF_LAUNDRY_METER,
    CONF_MEASURED,
    CONF_OPTIONAL,
    CONF_SOLAR,
    CONF_TOTAL,
    CONF_WASHER_STATUS,
    DOMAIN,
)

TOTAL = "sensor.maison"
POOL = "sensor.pompe"
CHARGER = "sensor.borne"
LAUNDRY = "sensor.garage"
WASHER_STATUS = "sensor.lave_linge_current_status"
WASHER_REMAINING = "sensor.lave_linge_remaining_time"
WASHER_TOTAL = "sensor.lave_linge_total_time"
WASHER_REMOTE = "binary_sensor.lave_linge_remote_start"
WASHER_OPERATION = "select.lave_linge_operation"
GRID = "sensor.reseau"
SOLAR = "sensor.panneaux"
PEAK = "sensor.pic_solaire"

DATA = {
    CONF_TOTAL: TOTAL,
    CONF_MEASURED: [POOL],
    CONF_OPTIONAL: [CHARGER],
    CONF_LAUNDRY_METER: LAUNDRY,
    CONF_WASHER_STATUS: WASHER_STATUS,
}


def thinq_washer(hass) -> None:
    """Three sensors on one LG ThinQ device, registered as that integration does."""
    thinq = MockConfigEntry(domain="lg_thinq")
    thinq.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=thinq.entry_id, identifiers={("lg_thinq", "washer")})
    registry = er.async_get(hass)
    for key, entity in (("current_state", WASHER_STATUS), ("remain", WASHER_REMAINING),
                        ("total", WASHER_TOTAL), ("remote_control_enabled", WASHER_REMOTE),
                        ("operation_mode", WASHER_OPERATION)):
        registry.async_get_or_create(
            entity.split(".")[0], "lg_thinq", f"washer_{key}", config_entry=thinq,
            device_id=device.id, translation_key=key,
            suggested_object_id=entity.split(".")[1])


def washer(hass, status: str, *, remaining_min: float | None = None,
           total_min: float | None = None) -> None:
    hass.states.async_set(WASHER_STATUS, status)
    ends = (dt_util.utcnow() + timedelta(minutes=remaining_min)).isoformat() \
        if remaining_min is not None else "unknown"
    hass.states.async_set(WASHER_REMAINING, ends)
    hass.states.async_set(WASHER_TOTAL, str(total_min) if total_min else "unknown")


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


async def setup_entry(hass, data=None):
    entry = MockConfigEntry(domain=DOMAIN, data=data or DATA, title="Appliance Watch")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def state_of(hass, entry, suffix: str) -> str:
    return hass.states.get(entity_id(hass, entry, suffix)).state


async def test_entities_appear_for_all_three_watchers(hass):
    power(hass, TOTAL, 1200)
    power(hass, POOL, 500)
    power(hass, LAUNDRY, 30)
    entry = await setup_entry(hass)

    for watcher in ("lave_vaisselle", "lave_linge", "seche_linge"):
        assert entity_id(hass, entry, f"{watcher}_en_marche") is not None
    # Each machine has its own stage, and the washer learns nothing.
    assert entity_id(hass, entry, "lave_linge_etape") is not None
    assert entity_id(hass, entry, "lave_linge_cycles_appris") is None
    assert entity_id(hass, entry, "seche_linge_cycles_appris") is not None


async def test_the_washer_is_followed_from_thinq(hass):
    thinq_washer(hass)
    washer(hass, "power_off")
    power(hass, LAUNDRY, 30)
    entry = await setup_entry(hass)

    washer(hass, "rinsing", remaining_min=30, total_min=90)
    power(hass, LAUNDRY, 150)
    await hass.async_block_till_done()

    assert state_of(hass, entry, "lave_linge_en_marche") == "on"
    assert state_of(hass, entry, "lave_linge_etape") == "rincage"
    assert float(state_of(hass, entry, "lave_linge_temps_restant")) == pytest.approx(30, abs=1)
    # Started an hour ago, by the machine's own plan.
    assert float(state_of(hass, entry, "lave_linge_progression")) == pytest.approx(67, abs=1)
    assert state_of(hass, entry, "seche_linge_en_marche") == "off"

    washer(hass, "end")
    await hass.async_block_till_done()
    assert state_of(hass, entry, "lave_linge_etat") == "termine"
    assert float(state_of(hass, entry, "lave_linge_duree_dernier_cycle")) \
        == pytest.approx(60, abs=1)


async def test_the_meter_is_the_dryer_when_the_washer_is_idle(hass, freezer):
    thinq_washer(hass)
    washer(hass, "power_off")
    power(hass, LAUNDRY, 30)
    entry = await setup_entry(hass)

    for second in range(0, 50, 10):
        freezer.tick(timedelta(seconds=10))
        power(hass, LAUNDRY, 2000 + second)
        await hass.async_block_till_done()

    assert state_of(hass, entry, "seche_linge_en_marche") == "on"
    assert state_of(hass, entry, "lave_linge_en_marche") == "off"


@pytest.mark.parametrize("status", ["running", "unavailable"])
async def test_the_meter_is_not_the_dryer_unless_the_washer_is_known_idle(
        hass, freezer, status):
    thinq_washer(hass)
    washer(hass, status, remaining_min=60, total_min=60)
    power(hass, LAUNDRY, 30)
    entry = await setup_entry(hass)

    for second in range(0, 50, 10):
        freezer.tick(timedelta(seconds=10))
        power(hass, LAUNDRY, 2000 + second)
        await hass.async_block_till_done()

    assert state_of(hass, entry, "seche_linge_en_marche") == "off"


async def test_the_dryer_s_learned_rhythm_survives_the_split(hass, hass_storage):
    """Before 0.5 the dryer was learned under the shared laundry watcher."""
    learned = {"durations": [62.0], "energies": [1300.0], "pauses_s": [120.0],
               "heats": [], "rejected": 0}
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, title="Appliance Watch")
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"buanderie": {"total_energy_wh": 5000.0, "fingerprints": {
            "seche_linge": learned, "lave_linge": learned}}},
    }
    power(hass, TOTAL, 1200)
    power(hass, POOL, 500)
    power(hass, LAUNDRY, 30)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, entry, "seche_linge_cycles_appris") == "1"
    # The old washer's fingerprint and the shared total are not carried over.
    assert float(state_of(hass, entry, "seche_linge_energie")) == 0


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


async def test_the_entity_ids_the_live_activities_rely_on(hass):
    """An English UI names them so; claude-ha's automations hard-code them.

    A wrong id fails silently: the automation loads and never fires.
    """
    power(hass, TOTAL, 1200)
    power(hass, POOL, 500)
    power(hass, LAUNDRY, 30)
    entry = await setup_entry(hass)

    created = {entry_entity.entity_id for entry_entity
               in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    for watcher in ("lave_vaisselle", "lave_linge", "seche_linge"):
        assert {f"binary_sensor.{watcher}_running", f"sensor.{watcher}_state",
                f"sensor.{watcher}_time_remaining", f"sensor.{watcher}_progress",
                f"sensor.{watcher}_expected_end",
                f"sensor.{watcher}_last_cycle_duration"} <= created
    assert {"sensor.lave_vaisselle_stage", "sensor.lave_vaisselle_heating_phases",
            "sensor.lave_linge_stage"} <= created


# --------------------------------------------------------------------------
# Solar start — the washer armed for remote start, the sun doing the rest
# --------------------------------------------------------------------------

async def armed_in_the_sun(hass, freezer, *, export_w: float, minutes: int = 31,
                           armed: str = "on", extra: dict | None = None):
    """Set up with the washer armed, then let the grid export for a while."""
    thinq_washer(hass)
    washer(hass, "initial")
    hass.states.async_set(WASHER_REMOTE, armed)
    power(hass, LAUNDRY, 2)
    power(hass, GRID, -export_w)
    start = async_mock_service(hass, "select", "select_option")
    entry = await setup_entry(hass, DATA | {CONF_GRID: GRID} | (extra or {}))
    for _ in range(minutes * 2):
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    return entry, start


async def test_the_sun_starts_the_armed_washer(hass, freezer):
    entry, start = await armed_in_the_sun(hass, freezer, export_w=1500)
    assert [call.data for call in start] == [
        {"entity_id": WASHER_OPERATION, "option": "start"}]
    waiting = hass.states.get(entity_id(hass, entry, "lave_linge_attente_soleil"))
    assert waiting.attributes["economie_estimee_pct"] >= 50


async def test_steady_sun_before_the_peak_holds_out_for_the_heating(hass, freezer):
    """Half the cycle from the sun is on offer, but better is coming."""
    power(hass, SOLAR, 1500)
    hass.states.async_set(PEAK, (dt_util.utcnow() + timedelta(hours=3)).isoformat())
    entry, start = await armed_in_the_sun(
        hass, freezer, export_w=700, extra={CONF_SOLAR: SOLAR, CONF_FORECAST_PEAK: PEAK})
    assert start == []
    waiting = hass.states.get(entity_id(hass, entry, "lave_linge_attente_soleil"))
    assert waiting.attributes["economie_estimee_pct"] >= 50
    assert waiting.attributes["economie_requise_pct"] == 90


async def test_a_thin_surplus_keeps_it_waiting(hass, freezer):
    entry, start = await armed_in_the_sun(hass, freezer, export_w=150)
    assert start == []
    assert state_of(hass, entry, "lave_linge_attente_soleil") == "on"


async def test_a_washer_not_armed_is_never_started(hass, freezer):
    _, start = await armed_in_the_sun(hass, freezer, export_w=1500, armed="off")
    assert start == []


async def test_the_switch_holds_it_back(hass, freezer):
    entry, start = await armed_in_the_sun(hass, freezer, export_w=1500, minutes=1)
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": entity_id(hass, entry, "lave_linge_depart_solaire")}, blocking=True)
    for _ in range(62):
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert start == []
    assert state_of(hass, entry, "lave_linge_attente_soleil") == "off"


async def test_without_a_grid_meter_there_is_no_solar_start(hass):
    thinq_washer(hass)
    washer(hass, "initial")
    power(hass, LAUNDRY, 2)
    entry = await setup_entry(hass)
    assert state_of(hass, entry, "lave_linge_attente_soleil") == "unavailable"
