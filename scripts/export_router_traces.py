#!/usr/bin/env python3
"""Export everything the router reads, alongside what was actually decided.

The point is to replay real days through the new brain and diff its decisions
against the ones the two integrations took at the time — before it is allowed
to command anything. Divergences are expected wherever a correction changed
the outcome on purpose; the rest must be explained.

Text entities (Tempo colours, the sun) have no long-term statistics, so
everything comes from the history API and is resampled onto a common grid.

Usage:
    python3 scripts/export_router_traces.py [--days 3] [--config ../claude-ha/config.env]
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "router"
STEP = timedelta(minutes=5)

# name -> entity_id. The names are what the replay script reads.
SOURCES: dict[str, str] = {
    "grid_w": "sensor.shellyproem50_34987a450cf4_em0_power",
    "pv_w": "sensor.shellyproem50_34987a450cf4_em1_power",
    "heater_w": "sensor.dhwp_actuator_electric_power_consumption_q",
    "tank_top_c": "sensor.dhwp_actuator_temperature",
    "tank_middle_c": "sensor.dhwp_actuator_middle_water_temperature",
    "garage_c": "sensor.capteur_garage_temperature",
    "outdoor_c": "sensor.hue_outdoor_motion_sensor_2_temperature",
    "water_temp_c": "sensor.tild_vp_temperature",
    "forecast_today_kwh": "sensor.energy_production_today",
    "forecast_tomorrow_kwh": "sensor.energy_production_tomorrow",
    "tempo_color": "sensor.rte_tempo_couleur_actuelle",
    "tempo_next_color": "sensor.rte_tempo_prochaine_couleur",
    "is_hc": "binary_sensor.rte_tempo_heures_creuses",
    "sun": "sun.sun",
    "signal_on": "switch.shellyproem50_a0dd6ca07f48_switch_0",
    "boost_on": "number.dhwp_actuator_boost_mode_duration",
    "pump_speed": "number.antea_vs_test_vs",
    # What the integrations decided at the time — the reference to diff against.
    "actual_water_action": "sensor.smart_dhwp_action",
    "actual_pump_speed": "sensor.smart_pompe_piscine_target_speed",
}
NUMERIC = {
    "grid_w", "pv_w", "heater_w", "tank_top_c", "tank_middle_c", "garage_c",
    "outdoor_c", "water_temp_c", "forecast_today_kwh", "forecast_tomorrow_kwh",
    "boost_on", "pump_speed", "actual_pump_speed",
}


def load_env(config: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in config.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def fetch_day(url: str, token: str, entities: list[str],
              start: datetime, end: datetime) -> dict[str, list]:
    query = (f"{url}/api/history/period/{urllib.parse.quote(start.isoformat())}"
             f"?filter_entity_id={','.join(entities)}"
             f"&end_time={urllib.parse.quote(end.isoformat())}"
             "&minimal_response&no_attributes")
    request = urllib.request.Request(query, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=180) as response:
        blocks = json.load(response)
    out: dict[str, list] = {}
    for block in blocks:
        if not block:
            continue
        points = []
        for state in block:
            stamp = state.get("last_changed") or state.get("last_updated")
            if not stamp:
                continue
            points.append((datetime.fromisoformat(stamp).astimezone(TZ),
                           state.get("state")))
        out[block[0]["entity_id"]] = points
    return out


def resample(points: list, grid: list[datetime], numeric: bool) -> list:
    """Last known value at each grid instant — a reading holds until replaced."""
    out, index, current = [], 0, None
    for moment in grid:
        while index < len(points) and points[index][0] <= moment:
            current = points[index][1]
            index += 1
        if current in (None, "unknown", "unavailable", ""):
            out.append(None)
        elif numeric:
            try:
                out.append(round(float(current), 2))
            except ValueError:
                out.append(None)
        else:
            out.append(current)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--date", action="append", default=[],
                        help="journée précise (AAAA-MM-JJ), répétable ; "
                             "les nuits d'avril-mai sont les seules où les "
                             "branches nocturnes s'exécutent")
    parser.add_argument("--config", type=Path,
                        default=ROOT.parent / "claude-ha" / "config.env")
    args = parser.parse_args()
    env = load_env(args.config)
    url, token = env["HA_URL"], env["HA_TOKEN"]
    FIXTURES.mkdir(parents=True, exist_ok=True)

    end = datetime.now(TZ).replace(second=0, microsecond=0)
    if args.date:
        starts = [datetime.fromisoformat(d).replace(tzinfo=TZ) for d in args.date]
    else:
        starts = [(end - timedelta(days=day)).replace(hour=0, minute=0)
                  for day in range(args.days, 0, -1)]
    for day_start in starts:
        day_end = day_start + timedelta(days=1)
        series = fetch_day(url, token, list(SOURCES.values()), day_start, day_end)

        steps = int((day_end - day_start) / STEP)
        grid = [day_start + i * STEP for i in range(steps)]
        columns = {
            name: resample(series.get(entity, []), grid, name in NUMERIC)
            for name, entity in SOURCES.items()
        }
        payload = {
            "date": day_start.date().isoformat(),
            "step_minutes": int(STEP.total_seconds() // 60),
            "times": [moment.isoformat() for moment in grid],
            "columns": columns,
        }
        target = FIXTURES / f"{day_start.date().isoformat()}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False) + "\n")
        filled = sum(1 for v in columns["grid_w"] if v is not None)
        print(f"{target.name}  {steps} pas, réseau renseigné sur {filled}")


if __name__ == "__main__":
    main()
