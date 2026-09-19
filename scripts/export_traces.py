#!/usr/bin/env python3
"""Export real power traces from Home Assistant into test fixtures.

The detector is tuned against *recorded* cycles rather than invented numbers,
so the test suite replays what the appliances actually drew. This script pulls
the windows listed in ``WINDOWS`` from the recorder and writes one JSON file
per window into ``tests/fixtures/``.

Two kinds of trace:

* ``garage`` — a single measured sensor (washer + dryer share that Shelly);
* ``residual`` — the *unmeasured* house load, i.e. total consumption minus every
  measured appliance. That is where the dishwasher hides, next to the oven.

Usage (needs the HA token from the claude-ha repo):

    python3 scripts/export_traces.py [--config ../claude-ha/config.env]
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
FIXTURES = ROOT / "tests" / "fixtures"

GARAGE = "sensor.shellyproem50_a0dd6ca07f48_em1_power"
TOTAL = "sensor.load"
# Everything already metered elsewhere, with the factor needed to reach watts:
# the Tesla wall connector reports kW and goes unavailable when idle.
MEASURED = {
    "sensor.controleur_pompes_switch_0_power": 1.0,
    "sensor.controleur_pompes_switch_1_power": 1.0,
    "sensor.shellyproem50_a0dd6ca07f48_em0_power": 1.0,
    GARAGE: 1.0,
    "sensor.dhwp_actuator_electric_power_consumption_q": 1.0,
    "sensor.tesla_wall_connector_total_power": 1000.0,
}

# (name, kind, start, end, what it is) — local time, Europe/Paris.
WINDOWS: list[tuple[str, str, str, str, str]] = [
    # --- dishwasher: three real cycles, the positives ---------------------
    ("dishwasher_thu_1342", "residual", "2026-09-17T13:15", "2026-09-17T15:15",
     "cycle lave-vaisselle, 63 min"),
    ("dishwasher_wed_0142", "residual", "2026-09-16T01:15", "2026-09-16T03:15",
     "cycle lave-vaisselle de nuit (heures creuses)"),
    ("dishwasher_mon_1700", "residual", "2026-09-14T16:40", "2026-09-14T17:35",
     "cycle lave-vaisselle, début seulement (cuisson enchaînée après)"),
    # --- the negatives that must never fire -------------------------------
    ("oven_mon_1804", "residual", "2026-09-14T17:50", "2026-09-14T18:40",
     "four: paliers 2540-2658 W, 300 W au-dessus du lave-vaisselle"),
    ("cooking_wed_1850", "residual", "2026-09-16T18:40", "2026-09-16T20:05",
     "plaques + four: alternance 1 min, pointes 3700 W"),
    # --- garage: washer vs dryer ------------------------------------------
    ("washer_wed_0835", "garage", "2026-09-16T08:25", "2026-09-16T09:40",
     "lave-linge, 60 min"),
    ("washer_sat_1015", "garage", "2026-09-12T10:05", "2026-09-12T12:20",
     "lave-linge, programme long 120 min"),
    ("dryer_wed_1745", "garage", "2026-09-16T17:40", "2026-09-16T19:20",
     "sèche-linge, 70 min"),
    # Étiquetée "sèche-linge" jusqu'au 19/09, à tort : après son bloc de
    # chauffe elle tourne 42 min à 150 W médians sans jamais repasser au-dessus
    # de 1000 W. Un sèche-linge qui cesse de chauffer à T+24 sortirait du linge
    # mouillé. C'est un lavage à chaud, et le seuil de classification avait été
    # calé pour satisfaire cette étiquette fausse.
    ("washer_mon_1250", "garage", "2026-09-14T12:40", "2026-09-14T13:55",
     "lave-linge, lavage à chaud, 60 min"),
    ("washer_sat_1120", "garage", "2026-09-19T11:15", "2026-09-19T12:20",
     "lave-linge, lavage à chaud — le cycle pris pour un sèche-linge"),
    # --- garage noise: 15 min around 180 W, must not start a cycle --------
    ("garage_blip_wed_0745", "garage", "2026-09-16T07:35", "2026-09-16T08:10",
     "parasite 180 W / 12 min"),
    ("garage_blip_tue_0845", "garage", "2026-09-15T08:35", "2026-09-15T09:10",
     "parasite 180 W / 14 min"),
    ("garage_blip_mon_1730", "garage", "2026-09-14T17:20", "2026-09-14T17:55",
     "parasite 180 W / 10 min"),
]


def load_token(config: Path) -> tuple[str, str]:
    env: dict[str, str] = {}
    for line in config.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env["HA_URL"], env["HA_TOKEN"]


def fetch(url: str, token: str, entities: list[str],
          start: datetime, end: datetime) -> dict[str, list[tuple[datetime, float]]]:
    query = (f"{url}/api/history/period/{urllib.parse.quote(start.isoformat())}"
             f"?filter_entity_id={','.join(entities)}"
             f"&end_time={urllib.parse.quote(end.isoformat())}"
             "&minimal_response&no_attributes")
    request = urllib.request.Request(query, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request) as response:
        blocks = json.load(response)

    series: dict[str, list[tuple[datetime, float]]] = {}
    for block in blocks:
        if not block:
            continue
        points = []
        for state in block:
            try:
                value = float(state["state"])
            except (ValueError, KeyError):
                continue  # unavailable / unknown: no reading, not a zero
            stamp = state.get("last_changed") or state["last_updated"]
            points.append((datetime.fromisoformat(stamp).astimezone(TZ), value))
        series[block[0]["entity_id"]] = points
    return series


def residual_trace(series: dict[str, list[tuple[datetime, float]]]) -> list[tuple[datetime, float]]:
    """Total minus every measured load, sampled at each total-power reading."""
    cursors = {entity: 0 for entity in MEASURED}
    latest = {entity: 0.0 for entity in MEASURED}
    out = []
    for stamp, total in series.get(TOTAL, []):
        for entity, factor in MEASURED.items():
            points = series.get(entity, [])
            index = cursors[entity]
            while index < len(points) and points[index][0] <= stamp:
                latest[entity] = points[index][1] * factor
                index += 1
            cursors[entity] = index
        out.append((stamp, total - sum(latest.values())))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=ROOT.parent / "claude-ha" / "config.env")
    parser.add_argument("--only", action="append", default=[],
                        help="n'exporter que ces fenêtres (répétable) ; "
                             "sans lui, toutes sont réexportées")
    args = parser.parse_args()
    url, token = load_token(args.config)
    FIXTURES.mkdir(parents=True, exist_ok=True)

    for name, kind, start_s, end_s, label in WINDOWS:
        if args.only and name not in args.only:
            continue
        start = datetime.fromisoformat(start_s).replace(tzinfo=TZ)
        end = datetime.fromisoformat(end_s).replace(tzinfo=TZ)
        entities = [GARAGE] if kind == "garage" else [TOTAL, *MEASURED]
        series = fetch(url, token, entities, start, end)
        points = series.get(GARAGE, []) if kind == "garage" else residual_trace(series)
        payload = {
            "name": name,
            "kind": kind,
            "label": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "points": [[stamp.isoformat(), round(value, 1)] for stamp, value in points],
        }
        target = FIXTURES / f"{name}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
        print(f"{name:24s} {kind:9s} {len(points):5d} points  {label}")


if __name__ == "__main__":
    main()
