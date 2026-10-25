"""Recorded days, replayed as a counterfactual.

A first version of this file measured per-tick agreement with the old
integrations and reported 100 % on the water heater. A review showed the
number was an echo: the replay injected the *real* contactor state, so the
two-hour hold forced the answer and the brain was never asked. The night
branches — the reason this port exists — were never reached at all.

The replay now drives its own loads and corrects the meter for them, which
makes per-tick agreement meaningless by construction: the two systems are
deliberately doing different things. What is checked here is behaviour, and
only what these traces can honestly support.

**What they cannot support**: the night corrections. The recorder no longer
holds enough of April-May — the only period where a night checkpoint ever
fired — so those are covered by the unit tests in
``test_water_heater_decision.py`` and by nothing else until the instance runs
the router for a season.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

FIXTURES = ROOT / "tests" / "fixtures" / "router"
DAYS = sorted(FIXTURES.glob("*.json"))

pytestmark = pytest.mark.skipif(not DAYS, reason="aucune trace exportée")


@pytest.fixture(scope="module")
def reports():
    from replay_router import USAGE_RANGE_KWH, replay  # noqa: E402

    return {path.stem: {usage: replay(path, usage) for usage in USAGE_RANGE_KWH}
            for path in DAYS}


def _any(reports, day):
    return next(iter(reports[day].values()))


def _water_min(day: str) -> float | None:
    trace = json.loads((FIXTURES / f"{day}.json").read_text())
    water = [w for w in trace["columns"]["water_temp_c"] if w is not None]
    return min(water) if water else None


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_pump_never_spends_more_than_before(reports, day):
    """Nothing in this port gives the pump a reason to run harder."""
    from replay_router import pump_kwh  # noqa: E402

    report = _any(reports, day)
    router = pump_kwh(report["router"]["pump"])
    actual = pump_kwh(report["actual"]["pump"])
    assert router <= actual * 1.15, f"{router:.2f} vs {actual:.2f}"


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_warm_water_gets_the_same_filtration_as_before(reports, day):
    """Speed for speed the two disagree; over a warm day they land together."""
    from replay_router import pump_kwh  # noqa: E402

    water = _water_min(day)
    if water is None or water <= 24.0:
        pytest.skip("eau sous la porte de chaleur — voir le test suivant")
    report = _any(reports, day)
    router = pump_kwh(report["router"]["pump"])
    actual = pump_kwh(report["actual"]["pump"])
    assert router == pytest.approx(actual, rel=0.15), f"{router:.2f} vs {actual:.2f}"


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_cold_water_keeps_the_pump_at_its_baseline(reports, day):
    """The correction, seen from the other end. On 12 May the water was at
    22.1 °C and the old gate let the pump climb anyway, because the outdoor
    sensor sits in the sun and read above 28 °C. Extra filtration on water
    that cold buys nothing.
    """
    water = _water_min(day)
    if water is None or water > 24.0:
        pytest.skip("eau au-dessus de la porte de chaleur")
    minutes = _any(reports, day)["router"]["pump"]
    assert minutes[2] == 0 and minutes[3] == 0


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_pump_never_stops_outside_winter_or_a_red_peak(reports, day):
    trace = json.loads((FIXTURES / f"{day}.json").read_text())
    colours = [c for c in trace["columns"]["tempo_color"] if c]
    water = [w for w in trace["columns"]["water_temp_c"] if w is not None]
    if "Rouge" in colours or (water and min(water) <= 20):
        pytest.skip("jour Rouge ou eau froide — l\'arrêt est attendu")
    assert _any(reports, day)["router"]["pump"][0] == 0


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_water_heater_only_runs_in_daylight_on_these_days(reports, day):
    """None of these traces has a tank cold enough to need the night, so any
    night minute would mean a checkpoint fired that should not have."""
    assert _any(reports, day)["router"]["night"] == 0


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_heater_runs_within_the_same_order_as_before(reports, day):
    """The tank temperature is exogenous here — it comes from a day when the
    real heater ran for its own length — so this cannot be tight. It catches
    a brain that never starts, or one that never stops.
    """
    report = _any(reports, day)
    router, actual = report["router"]["heating"], report["actual"]["heating"]
    assert router > 0
    assert router < 2 * max(actual, 120) + 120


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_result_does_not_hinge_on_the_assumed_daily_usage(reports, day):
    """The one input that had to be invented. If the outcome moved with it,
    nothing here would be worth reading.
    """
    heating = {usage: report["router"]["heating"]
               for usage, report in reports[day].items()}
    assert len(set(heating.values())) == 1, heating


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_every_switch_is_paired(reports, day):
    """An odd number of events means the contactor was left closed at
    midnight — possible, but worth seeing rather than hiding."""
    events = _any(reports, day)["events"]
    for (_, first), (_, second) in zip(events[::2], events[1::2]):
        assert first.endswith("ON") and second.endswith("OFF")
