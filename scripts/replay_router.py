#!/usr/bin/env python3
"""Replay recorded days through the router and diff against what really happened.

Prints an agreement rate per load and lists every divergence with the reason
the router gives, so each one can be classed as "a correction doing its job" or
"a bug". This runs before the router is allowed to command anything.

Two inputs cannot be recovered from the history and are stated rather than
guessed — both are noted in the output:

* the learned daily usage, which lived in the old integration's store. A flat
  1.5 kWh stands in, which is what its own fixtures used;
* the smoothing. Both integrations run an EMA whose time constant is about
  seven minutes, fed by a meter reporting every couple of seconds; the export
  only has one sample every five minutes. The same time constant is applied to
  those samples, which is the closest honest approximation — without it a
  sample caught on a cloud edge reads as surplus the real brain never saw.

Usage:
    python3 scripts/replay_router.py [tests/fixtures/router/*.json]
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "appliance_watch"))

from logic import thermo  # noqa: E402
from logic.cost import CycleAccumulator  # noqa: E402
from logic.loads import pool as pool_load  # noqa: E402
from logic.loads import water_heater as water_load  # noqa: E402
from logic.router import route  # noqa: E402

# Matches what the coordinator injects in production, not the module defaults.
WATER_THR = water_load.Thresholds(heater_nominal_w=650.0)
POOL_THR = pool_load.Thresholds()
ASSUMED_DAILY_USAGE_KWH = 1.5
# Time constant of the grid EMA in both integrations, in minutes.
SMOOTH_TAU_MIN = 7.0


def hours_to_morning(now: datetime) -> float:
    morning = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if now >= morning:
        morning += timedelta(days=1)
    return (morning - now).total_seconds() / 3600


def smooth(previous: float | None, raw: float, step_minutes: float) -> float:
    """EMA rescaled to the sampling step, as the coordinators do."""
    if previous is None:
        return raw
    import math
    alpha = 1.0 - math.exp(-step_minutes / SMOOTH_TAU_MIN)
    return alpha * raw + (1.0 - alpha) * previous


def build(day: dict, index: int, signal_on_at: datetime | None,
          grid_smooth: float | None = None):
    columns = day["columns"]
    now = datetime.fromisoformat(day["times"][index])

    def value(name, default=None):
        raw = columns[name][index]
        return default if raw is None else raw

    tank_top = value("tank_top_c", WATER_THR.target_top_c)
    needed = thermo.energy_budget_until_morning_kwh(
        current_top_c=tank_top,
        target_top_c=WATER_THR.target_top_c,
        floor_c=WATER_THR.hard_floor_c,
        garage_c=value("garage_c", 18.0),
        hours_to_morning=hours_to_morning(now),
        expected_usage_kwh=ASSUMED_DAILY_USAGE_KWH,
    )
    grid = grid_smooth if grid_smooth is not None else value("grid_w", 0.0)
    water = water_load.Inputs(
        now=now,
        mode="auto",
        tank_top_c=tank_top,
        tank_middle_c=value("tank_middle_c"),
        garage_c=value("garage_c", 18.0),
        outdoor_c=value("outdoor_c"),
        grid_smooth_w=grid,
        pv_power_w=value("pv_w", 0.0),
        heater_power_w=value("heater_w", 0.0),
        tempo_color=value("tempo_color", "Bleu"),
        tempo_next_color=value("tempo_next_color"),
        is_hc=value("is_hc") == "on",
        forecast_today_kwh=value("forecast_today_kwh"),
        forecast_tomorrow_kwh=value("forecast_tomorrow_kwh"),
        energy_needed_kwh=needed,
        cycle=CycleAccumulator(),
        signal_on_at=signal_on_at,
        signal_currently_on=value("signal_on") == "on",
        boost_currently_on=(value("boost_on", 0.0) or 0.0) > 0.5,
    )
    pool = pool_load.Inputs(
        now=now,
        daylight=value("sun") == "above_horizon",
        grid_w=grid,
        pump_speed=int(value("pump_speed", 1) or 1),
        water_temp_c=value("water_temp_c"),
        air_temp_c=value("outdoor_c"),
        mode="auto",
        # Filled in by the caller: a v3 session is state the router carries,
        # and without it the cap and cooldown never fire — the replay would
        # re-enter v3 on every tick and call it a divergence.
        v3_started_at=None,
        v3_last_ended_at=None,
        tempo_color=value("tempo_color"),
        is_hc=value("is_hc") == "on",
        season=None,
    )
    return now, water, pool


def replay(path: Path) -> dict:
    day = json.loads(path.read_text())
    columns = day["columns"]
    steps = len(day["times"])
    signal_on_at: datetime | None = None
    previous_signal = False
    v3_started_at: datetime | None = None
    v3_last_ended_at: datetime | None = None
    season: str | None = None
    grid_smooth: float | None = None
    step_minutes = float(day["step_minutes"])

    water_same = water_diff = pool_same = pool_diff = 0
    divergences: list[tuple] = []
    # A session-based, rate-limited controller cannot be judged tick by tick:
    # two runs that spend the same minutes at each speed but enter them a few
    # minutes apart disagree on half the samples while behaving identically.
    minutes_router = {0: 0, 1: 0, 2: 0, 3: 0}
    minutes_actual = {0: 0, 1: 0, 2: 0, 3: 0}
    water_minutes_router = water_minutes_actual = 0

    for index in range(steps):
        now = datetime.fromisoformat(day["times"][index])
        on_now = columns["signal_on"][index] == "on"
        if on_now and not previous_signal:
            signal_on_at = now
        if not on_now:
            signal_on_at = None
        previous_signal = on_now

        raw_grid = columns["grid_w"][index]
        if raw_grid is not None:
            grid_smooth = smooth(grid_smooth, float(raw_grid), step_minutes)
        now, water_in, pool_in = build(day, index, signal_on_at, grid_smooth)
        pool_in = replace(pool_in, v3_started_at=v3_started_at,
                          v3_last_ended_at=v3_last_ended_at, season=season)
        allocation = route(water_in.grid_smooth_w, water_in, WATER_THR,
                           pool_in, POOL_THR)
        # Carry the pump's own state forward, as the coordinator does.
        if allocation.pool.enter_v3:
            v3_started_at = now
        if allocation.pool.leave_v3:
            v3_last_ended_at = now
            v3_started_at = None
        season = allocation.pool.season

        minutes_router[allocation.pool.target_speed] += int(step_minutes)
        actual_speed_now = columns["actual_pump_speed"][index]
        if actual_speed_now is not None:
            minutes_actual[int(actual_speed_now)] += int(step_minutes)
        if allocation.water.signal_switch_on:
            water_minutes_router += int(step_minutes)

        actual_action = columns["actual_water_action"][index]
        if actual_action is not None:
            if actual_action != "wait":
                water_minutes_actual += int(step_minutes)
            actual_on = actual_action != "wait"
            if allocation.water.signal_switch_on == actual_on:
                water_same += 1
            else:
                water_diff += 1
                divergences.append((now, "chauffe-eau",
                                    f"routeur={'ON' if allocation.water.signal_switch_on else 'OFF'}"
                                    f" / réel={'ON' if actual_on else 'OFF'} ({actual_action})",
                                    allocation.water.reason))

        actual_speed = columns["actual_pump_speed"][index]
        if actual_speed is not None:
            if allocation.pool.target_speed == int(actual_speed):
                pool_same += 1
            else:
                pool_diff += 1
                divergences.append((now, "piscine",
                                    f"routeur=v{allocation.pool.target_speed}"
                                    f" / réel=v{int(actual_speed)}",
                                    allocation.pool.reason))

    return {
        "date": day["date"],
        "water": (water_same, water_diff),
        "pool": (pool_same, pool_diff),
        "divergences": divergences,
        "minutes_router": minutes_router,
        "minutes_actual": minutes_actual,
        "water_minutes": (water_minutes_router, water_minutes_actual),
    }


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]] or sorted(
        (ROOT / "tests" / "fixtures" / "router").glob("*.json"))
    if not paths:
        raise SystemExit("aucune trace — lancer scripts/export_router_traces.py")

    print("Hypothèses : usage quotidien figé à "
          f"{ASSUMED_DAILY_USAGE_KWH} kWh, lissage transparent à 5 min.\n")
    for path in paths:
        report = replay(path)
        ws, wd = report["water"]
        ps, pd = report["pool"]
        print(f"=== {report['date']} ===")
        print(f"  chauffe-eau : {ws}/{ws+wd} identiques "
              f"({100*ws/max(1, ws+wd):.0f} %)")
        print(f"  piscine     : {ps}/{ps+pd} identiques "
              f"({100*ps/max(1, ps+pd):.0f} %)")
        mr, ma = report["minutes_router"], report["minutes_actual"]
        print("  minutes par vitesse   routeur / réel : "
              + "  ".join(f"v{k} {mr[k]:3d}/{ma[k]:3d}" for k in (0, 1, 2, 3)))
        wr, wa = report["water_minutes"]
        print(f"  minutes de chauffe    routeur / réel : {wr} / {wa}")
        grouped: dict[tuple, list] = {}
        for moment, load, delta, reason in report["divergences"]:
            grouped.setdefault((load, delta, reason), []).append(moment)
        for (load, delta, reason), moments in sorted(
                grouped.items(), key=lambda kv: -len(kv[1])):
            span = (f"{moments[0]:%H:%M}–{moments[-1]:%H:%M}"
                    if len(moments) > 1 else f"{moments[0]:%H:%M}")
            print(f"    {load:11s} {len(moments):3d}× {span}  {delta}")
            print(f"                 └─ {reason[:88]}")
        print()


if __name__ == "__main__":
    main()
