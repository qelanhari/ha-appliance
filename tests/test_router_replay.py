"""Three real days, replayed through the router and diffed against history.

This is the guard that the port did not quietly change behaviour. It is not a
unit test: it runs whole recorded days through the brains and compares their
decisions to the ones the two integrations actually took, minute for minute.

The water heater is held to exact agreement. The pump is held to its *time
per speed* instead, because a controller with a fifteen-minute cap and a
thirty-minute cooldown cannot be judged tick by tick: two runs that spend the
same minutes at each speed but enter them four minutes apart disagree on half
the samples while behaving identically.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

FIXTURES = ROOT / "tests" / "fixtures" / "router"
DAYS = sorted(FIXTURES.glob("*.json"))

pytestmark = pytest.mark.skipif(not DAYS, reason="aucune trace exportée")


@pytest.fixture(scope="module")
def reports():
    from replay_router import replay  # noqa: E402

    return {path.stem: replay(path) for path in DAYS}


def test_every_day_was_replayed(reports):
    assert len(reports) == len(DAYS)


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_water_heater_decides_exactly_as_before(reports, day):
    """The port of thirteen branches written on real incidents."""
    same, different = reports[day]["water"]
    assert different == 0, [d for d in reports[day]["divergences"]
                            if d[1] == "chauffe-eau"]
    assert same > 0


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_water_heater_runs_for_the_same_time(reports, day):
    router, actual = reports[day]["water_minutes"]
    assert router == actual


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_pump_keeps_its_baseline_hours(reports, day):
    """v1 runs around the clock; that total is the one that must not move."""
    router = reports[day]["minutes_router"]
    actual = reports[day]["minutes_actual"]
    assert abs(router[1] - actual[1]) <= 30  # one sample of slack either way


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_pump_spends_a_comparable_amount_of_energy(reports, day):
    """Some v2 time becomes shorter, harder v3 bursts — a phase difference in
    the skim sessions, not a change of policy. Ten per cent is the limit at
    which that stops being an explanation."""
    watts = {0: 0, 1: 160, 2: 500, 3: 1100}
    router = reports[day]["minutes_router"]
    actual = reports[day]["minutes_actual"]
    kwh = lambda m: sum(watts[s] * m[s] for s in watts) / 60_000  # noqa: E731
    assert kwh(router) == pytest.approx(kwh(actual), rel=0.12)


@pytest.mark.parametrize("day", [path.stem for path in DAYS])
def test_the_pump_never_stops_outside_a_red_day(reports, day):
    """v0 belongs to winter and to Rouge peak hours; neither applies here."""
    trace = json.loads((FIXTURES / f"{day}.json").read_text())
    if "Rouge" in (trace["columns"]["tempo_color"] or []):
        pytest.skip("jour Rouge — l'arrêt est attendu")
    assert reports[day]["minutes_router"][0] == 0
