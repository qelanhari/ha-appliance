#!/usr/bin/env python3
"""Replay recorded days through the router as a counterfactual.

An earlier version of this script fed the router the *real* contactor state,
then reported that it agreed with reality on every sample. That number meant
nothing: once ``signal_currently_on`` and ``signal_on_at`` come from the
history, the two-hour hold forces the answer and the brain is never asked.
Worse, the night branches were never reached at all, so the very corrections
this port exists for went untested.

The router now drives its own loads, and the meter is corrected for the
difference between what it would draw and what really was drawn:

    grid = grid_real − (heater_real + pump_real) + (heater_sim + pump_sim)

which holds as long as nothing else in the house reacts to those loads — true
here, the rest of the consumption being human.

The consequence is that per-tick agreement stops being a meaningful metric:
the two systems are deliberately doing different things. What is reported
instead is *behaviour* — when and for how long each load ran, on both sides —
and the tests check behaviour too.

Usage:
    python3 scripts/replay_router.py [tests/fixtures/router/*.json]
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "appliance_watch"))

from logic import thermo  # noqa: E402
from logic.cost import CycleAccumulator  # noqa: E402
from logic.loads import pool as pool_load  # noqa: E402
from logic.loads import water_heater as water_load  # noqa: E402
from logic.router import route  # noqa: E402

# What the coordinator injects in production, not the module defaults.
WATER_THR = water_load.Thresholds(heater_nominal_w=650.0)
POOL_THR = pool_load.Thresholds()

# The old integration's learned daily usage lived in its store and cannot be
# recovered. It is an *input*, not a detail — it decides whether a night
# checkpoint has anything to do at all — so the replay sweeps a plausible
# range instead of picking one value and calling it neutral.
USAGE_RANGE_KWH = (1.5, 3.0, 4.5)

# Grid EMA time constant. `pool_pump` uses alpha 0.2 per 30 s rescaled by
# elapsed time, i.e. tau = −30/ln(0.8) ≈ 134 s; `dhwp_smart` applies 0.3 per
# event refresh, faster still. 2.2 minutes is the slower of the two and the
# only one with a defined time constant. An earlier version used 7 minutes —
# the *settling* time quoted in a comment, not tau — which flattened the cloud
# edges the real brains did see.
SMOOTH_TAU_MIN = 2.2

# Minimum time between two commands, mirroring the coordinators.
PUMP_DWELL = timedelta(seconds=300)
HEATER_DWELL = timedelta(seconds=60)


def hours_to_morning(now: datetime) -> float:
    morning = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if now >= morning:
        morning += timedelta(days=1)
    return (morning - now).total_seconds() / 3600


def smooth(previous: float | None, raw: float, step_minutes: float) -> float:
    if previous is None:
        return raw
    alpha = 1.0 - math.exp(-step_minutes / SMOOTH_TAU_MIN)
    return alpha * raw + (1.0 - alpha) * previous


def is_night(moment: datetime) -> bool:
    return moment.hour >= 22 or moment.hour < 7


class Simulation:
    """The router's own view of the house, carried from tick to tick."""

    def __init__(self, usage_kwh: float) -> None:
        self.usage_kwh = usage_kwh
        self.signal_on = False
        self.signal_on_at: datetime | None = None
        self.last_signal_change: datetime | None = None
        self.boost_on = False
        self.pump_speed = 1
        self.last_pump_change: datetime | None = None
        self.v3_started_at: datetime | None = None
        self.v3_last_ended_at: datetime | None = None
        self.season: str | None = None
        self.grid_smooth: float | None = None
        self.heating_minutes = 0
        self.night_heating_minutes = 0
        self.pump_minutes = {0: 0, 1: 0, 2: 0, 3: 0}
        self.events: list[tuple[datetime, str]] = []

    @property
    def heater_w(self) -> float:
        # The appliance idles between compressor runs, but for a
        # counterfactual its nominal draw is the only honest figure.
        return WATER_THR.heater_nominal_w if self.signal_on else 0.0

    @property
    def pump_w(self) -> float:
        return float(pool_load.PUMP_W[self.pump_speed])


def _value(columns: dict, name: str, index: int, default=None):
    raw = columns[name][index]
    return default if raw is None else raw


def step(sim: Simulation, day: dict, index: int, step_minutes: float) -> None:
    columns = day["columns"]
    now = datetime.fromisoformat(day["times"][index])

    def value(name, default=None):
        return _value(columns, name, index, default)

    # The counterfactual meter: take the real loads out, put ours in.
    real_heater = value("heater_w", 0.0)
    real_pump = float(pool_load.PUMP_W.get(int(value("pump_speed", 1) or 1), 160))
    grid_raw = (value("grid_w", 0.0) - real_heater - real_pump
                + sim.heater_w + sim.pump_w)
    sim.grid_smooth = smooth(sim.grid_smooth, grid_raw, step_minutes)

    tank_top = value("tank_top_c", WATER_THR.target_top_c)
    needed = thermo.energy_budget_until_morning_kwh(
        current_top_c=tank_top,
        target_top_c=WATER_THR.target_top_c,
        floor_c=WATER_THR.hard_floor_c,
        garage_c=value("garage_c", 18.0),
        hours_to_morning=hours_to_morning(now),
        expected_usage_kwh=sim.usage_kwh,
    )
    water_in = water_load.Inputs(
        now=now, mode="auto", tank_top_c=tank_top,
        tank_middle_c=value("tank_middle_c"),
        garage_c=value("garage_c", 18.0), outdoor_c=value("outdoor_c"),
        grid_smooth_w=sim.grid_smooth, pv_power_w=value("pv_w", 0.0),
        heater_power_w=sim.heater_w,
        tempo_color=value("tempo_color", "Bleu"),
        tempo_next_color=value("tempo_next_color"),
        is_hc=value("is_hc") == "on",
        forecast_today_kwh=value("forecast_today_kwh"),
        forecast_tomorrow_kwh=value("forecast_tomorrow_kwh"),
        energy_needed_kwh=needed, cycle=CycleAccumulator(),
        signal_on_at=sim.signal_on_at, signal_currently_on=sim.signal_on,
        boost_currently_on=sim.boost_on,
    )
    pool_in = pool_load.Inputs(
        now=now, daylight=value("sun") == "above_horizon",
        grid_w=sim.grid_smooth, pump_speed=sim.pump_speed,
        water_temp_c=value("water_temp_c"), air_temp_c=value("outdoor_c"),
        mode="auto", v3_started_at=sim.v3_started_at,
        v3_last_ended_at=sim.v3_last_ended_at,
        tempo_color=value("tempo_color"), is_hc=value("is_hc") == "on",
        season=sim.season,
    )
    allocation = route(sim.grid_smooth, water_in, WATER_THR, pool_in, POOL_THR)

    # Apply, with the rate limits the coordinators impose.
    wanted_on = allocation.water.signal_switch_on
    if wanted_on != sim.signal_on and (
            sim.last_signal_change is None
            or now - sim.last_signal_change >= HEATER_DWELL):
        sim.signal_on = wanted_on
        sim.last_signal_change = now
        sim.signal_on_at = now if wanted_on else None
        sim.events.append((now, "chauffe ON" if wanted_on else "chauffe OFF"))
    sim.boost_on = allocation.water.boost_mode_on

    wanted_speed = allocation.pool.target_speed
    if wanted_speed != sim.pump_speed and (
            sim.last_pump_change is None
            or now - sim.last_pump_change >= PUMP_DWELL
            or allocation.pool.leave_v3):
        sim.pump_speed = wanted_speed
        sim.last_pump_change = now
    if allocation.pool.enter_v3:
        sim.v3_started_at = now
    if allocation.pool.leave_v3:
        sim.v3_last_ended_at = now
        sim.v3_started_at = None
    sim.season = allocation.pool.season

    minutes = int(step_minutes)
    if sim.signal_on:
        sim.heating_minutes += minutes
        if is_night(now):
            sim.night_heating_minutes += minutes
    sim.pump_minutes[sim.pump_speed] += minutes


def replay(path: Path, usage_kwh: float = 3.0) -> dict:
    day = json.loads(path.read_text())
    step_minutes = float(day["step_minutes"])
    sim = Simulation(usage_kwh)
    for index in range(len(day["times"])):
        step(sim, day, index, step_minutes)

    columns = day["columns"]
    actual_heating = actual_night = 0
    actual_pump = {0: 0, 1: 0, 2: 0, 3: 0}
    for index, stamp in enumerate(day["times"]):
        now = datetime.fromisoformat(stamp)
        action = columns["actual_water_action"][index]
        if action is not None and action != "wait":
            actual_heating += int(step_minutes)
            if is_night(now):
                actual_night += int(step_minutes)
        speed = columns["actual_pump_speed"][index]
        if speed is not None:
            actual_pump[int(speed)] += int(step_minutes)

    return {
        "date": day["date"],
        "usage_kwh": usage_kwh,
        "router": {"heating": sim.heating_minutes,
                   "night": sim.night_heating_minutes,
                   "pump": sim.pump_minutes},
        "actual": {"heating": actual_heating, "night": actual_night,
                   "pump": actual_pump},
        "events": sim.events,
        "forecast_tomorrow_kwh": columns["forecast_tomorrow_kwh"][0],
        "tempo": columns["tempo_color"][0],
        "water_temp_c": columns["water_temp_c"][0],
    }


def pump_kwh(minutes: dict[int, int]) -> float:
    return sum(pool_load.PUMP_W[speed] * minutes[speed]
               for speed in minutes) / 60_000


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]] or sorted(
        (ROOT / "tests" / "fixtures" / "router").glob("*.json"))
    if not paths:
        raise SystemExit("aucune trace — lancer scripts/export_router_traces.py")

    print("Simulation contrefactuelle : le routeur pilote ses propres charges et\n"
          "le compteur est corrigé en conséquence. L'accord au tick n'a donc pas\n"
          "de sens ici — c'est le comportement qui est comparé.\n")
    for path in paths:
        first = True
        for usage in USAGE_RANGE_KWH:
            report = replay(path, usage)
            router, actual = report["router"], report["actual"]
            if first:
                print(f"=== {report['date']}  (Tempo {report['tempo']}, "
                      f"demain {report['forecast_tomorrow_kwh']} kWh, "
                      f"eau {report['water_temp_c']} °C) ===")
                print(f"    réel      chauffe {actual['heating']:3d} min "
                      f"(dont {actual['night']:3d} de nuit) · "
                      f"pompe {pump_kwh(actual['pump']):.2f} kWh "
                      f"{[actual['pump'][s] for s in (0, 1, 2, 3)]}")
                first = False
            print(f"    usage {usage:.1f} kWh/j  chauffe {router['heating']:3d} min "
                  f"(dont {router['night']:3d} de nuit) · "
                  f"pompe {pump_kwh(router['pump']):.2f} kWh "
                  f"{[router['pump'][s] for s in (0, 1, 2, 3)]}")
            if report["events"]:
                marks = "  ".join(f"{moment:%H:%M} {what}"
                                  for moment, what in report["events"])
                print(f"                    {marks}")
        print()


if __name__ == "__main__":
    main()
