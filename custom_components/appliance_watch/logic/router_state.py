"""What the router must remember between ticks, and the small clocks it runs.

The two brains under :mod:`loads` are pure functions of a snapshot. Around
them, the integrations they came from kept state: a smoothed grid, the start
of the contactor's two-hour hold, the pump's v3 session, the learned daily
hot-water usage. This module holds that state and the arithmetic on it, so the
Home Assistant side only reads entities and calls services.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import Any

from .cost import CycleAccumulator, accumulate
from .fingerprints import Fingerprint
from .loads import pool as pool_load
from .loads import water_heater as water_load
from .pattern import PatternState, update_pattern

# Grid EMA time constant, in seconds. `pool_pump` used alpha 0.2 per 30 s,
# i.e. tau = −30/ln(0.8) ≈ 134 s; the replay settled on the same 2.2 min.
GRID_TAU_S: float = 132.0
# Changes of the contactor faster than this are refused (dhwp_smart default).
CONTACTOR_DWELL_S: float = 60.0
# Speed changes of the pump faster than this are refused (pool_pump default).
PUMP_DWELL_S: float = 300.0
# A mean draw is only trusted once a few whole cycles have been watched.
MIN_CYCLES_FOR_DRAW: int = 3


def smooth(previous: float | None, reading: float, elapsed_s: float,
           tau_s: float = GRID_TAU_S) -> float:
    """Time-based EMA: the same smoothing however chatty the meter is."""
    if previous is None:
        return reading
    elapsed_s = min(max(elapsed_s, 0.0), 10 * tau_s)
    weight = 1.0 - math.exp(-elapsed_s / tau_s)
    return previous + weight * (reading - previous)


def dwell_elapsed(last_change: datetime | None, now: datetime,
                  dwell_s: float) -> bool:
    """True once enough time has passed since the last change."""
    return last_change is None or (now - last_change).total_seconds() >= dwell_s


def mean_draw_w(fingerprint: Fingerprint | None) -> float | None:
    """Average draw over a whole cycle: median energy over median duration."""
    if fingerprint is None or fingerprint.samples < MIN_CYCLES_FOR_DRAW:
        return None
    if not fingerprint.energies or not fingerprint.durations:
        return None
    energy = sorted(fingerprint.energies)[len(fingerprint.energies) // 2]
    minutes = sorted(fingerprint.durations)[len(fingerprint.durations) // 2]
    return energy * 60.0 / minutes if minutes > 0 else None


def solar_share(grid_w: float, heater_w: float) -> float:
    """Fraction of the heater's draw covered by the sun right now."""
    if heater_w <= 0:
        return 0.0
    if grid_w <= 0:
        return 1.0
    return max(0.0, min(1.0, (heater_w - grid_w) / heater_w))


@dataclass
class WaterMemory:
    """The water heater's side: the contactor hold, the cycle, the habits."""

    mode: str = water_load.MODE_AUTO
    pattern: PatternState = field(default_factory=PatternState)
    cycle: CycleAccumulator = field(default_factory=CycleAccumulator)
    signal_on_at: datetime | None = None
    last_switch_change_at: datetime | None = None
    switch_was_on: bool = False
    boost_requested: bool = False
    boosts_today: int = 0
    abundant_since: datetime | None = None
    last_meter_wh: float | None = None
    day: date | None = None
    day_kwh: float = 0.0
    outdoor_sum: float = 0.0
    outdoor_count: int = 0

    def roll_day(self, today: date) -> None:
        """Fold yesterday's usage into the weekday pattern, once per day."""
        if self.day is not None and self.day != today:
            outdoor = (self.outdoor_sum / self.outdoor_count
                       if self.outdoor_count else 15.0)
            self.pattern = update_pattern(
                self.pattern, weekday=self.day.weekday(),
                observed_kwh=self.day_kwh, outdoor_avg_c=outdoor)
            self.day_kwh = 0.0
            self.outdoor_sum, self.outdoor_count = 0.0, 0
            self.boosts_today = 0
        self.day = today

    def meter(self, meter_wh: float | None, grid_w: float, heater_w: float,
              tempo: str, is_hc: bool) -> None:
        """Book what the heater drew since the last reading, by source."""
        if (meter_wh is not None and self.last_meter_wh is not None
                and heater_w > 0):
            kwh = max(0.0, meter_wh - self.last_meter_wh) / 1000.0
            if kwh > 0:
                self.cycle = accumulate(self.cycle, kwh,
                                        solar_share(grid_w, heater_w), tempo, is_hc)
                self.day_kwh += kwh
        self.last_meter_wh = meter_wh

    def abundant_minutes(self, now: datetime, abundant: bool) -> float:
        """How long the boost-level surplus has held without a break."""
        if not abundant:
            self.abundant_since = None
            return 0.0
        if self.abundant_since is None:
            self.abundant_since = now
        return (now - self.abundant_since).total_seconds() / 60.0

    def contactor_changed(self, now: datetime, on: bool) -> None:
        """A cycle starts on the rising edge: a fresh tally, a fresh hold."""
        if on and not self.switch_was_on:
            self.cycle = CycleAccumulator()
            self.signal_on_at = now
        elif not on:
            self.signal_on_at = None
            self.boost_requested = False
        self.last_switch_change_at = now
        self.switch_was_on = on


@dataclass
class PoolMemory:
    """The pump's side: its v3 session, its season, its last step."""

    mode: str = pool_load.MODE_AUTO
    v3_started_at: datetime | None = None
    v3_last_ended_at: datetime | None = None
    last_speed_change_at: datetime | None = None
    season: str | None = None

    def speed_changed(self, now: datetime, decision: pool_load.Decision) -> None:
        if decision.enter_v3:
            self.v3_started_at = now
        if decision.leave_v3:
            self.v3_last_ended_at = now
            self.v3_started_at = None
        self.last_speed_change_at = now


@dataclass
class RouterMemory:
    """Everything that survives a restart. Off by default: watch first."""

    piloting: bool = False
    grid_smooth_w: float | None = None
    grid_at: datetime | None = None
    water: WaterMemory = field(default_factory=WaterMemory)
    pool: PoolMemory = field(default_factory=PoolMemory)

    def feed_grid(self, now: datetime, grid_w: float) -> float:
        elapsed = (now - self.grid_at).total_seconds() if self.grid_at else 0.0
        self.grid_smooth_w = smooth(self.grid_smooth_w, grid_w, elapsed)
        self.grid_at = now
        return self.grid_smooth_w

    def book_pump_step(self, before: int, after: int) -> None:
        """Feed-forward: the meter lags the pump's own step by a minute."""
        if self.grid_smooth_w is not None:
            self.grid_smooth_w += pool_load.PUMP_W[after] - pool_load.PUMP_W[before]


# ------------------------------------------------------------- persistence


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


def _when(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    # A naive timestamp cannot be compared with an aware clock: drop it.
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def pattern_from_dict(data: dict[str, Any] | None) -> PatternState:
    """The weekday habits, in the shape dhwp_smart stored them too."""
    if not data:
        return PatternState()
    base = PatternState()
    return PatternState(
        daily_kwh_ema=tuple(float(v) for v in data.get("daily_kwh_ema", base.daily_kwh_ema)),
        outdoor_coef=float(data.get("outdoor_coef", base.outdoor_coef)),
        samples_n=int(data.get("samples_n", base.samples_n)),
    )


def as_dict(memory: RouterMemory) -> dict[str, Any]:
    water, pool = memory.water, memory.pool
    return {
        "piloting": memory.piloting,
        "grid_smooth_w": memory.grid_smooth_w,
        "water": {
            "mode": water.mode,
            "pattern": {"daily_kwh_ema": list(water.pattern.daily_kwh_ema),
                        "outdoor_coef": water.pattern.outdoor_coef,
                        "samples_n": water.pattern.samples_n},
            "cycle": asdict(water.cycle),
            "signal_on_at": _iso(water.signal_on_at),
            "last_switch_change_at": _iso(water.last_switch_change_at),
            "switch_was_on": water.switch_was_on,
            "boost_requested": water.boost_requested,
            "boosts_today": water.boosts_today,
            "last_meter_wh": water.last_meter_wh,
            "day": _iso(water.day),
            "day_kwh": water.day_kwh,
            "outdoor_sum": water.outdoor_sum,
            "outdoor_count": water.outdoor_count,
        },
        "pool": {
            "mode": pool.mode,
            "v3_started_at": _iso(pool.v3_started_at),
            "v3_last_ended_at": _iso(pool.v3_last_ended_at),
            "last_speed_change_at": _iso(pool.last_speed_change_at),
            "season": pool.season,
        },
    }


def from_dict(data: dict[str, Any] | None) -> RouterMemory:
    """Restore, tolerating anything missing: an empty store is a fresh start.

    Deliberately not restored: when the grid was last smoothed, and the start
    of an abundant-surplus streak. They describe the last minutes, and after
    a restart they would be a lie.
    """
    data = data or {}
    water, pool = data.get("water") or {}, data.get("pool") or {}
    cycle = water.get("cycle") or {}
    return RouterMemory(
        piloting=bool(data.get("piloting", False)),
        grid_smooth_w=data.get("grid_smooth_w"),
        water=WaterMemory(
            mode=water.get("mode") if water.get("mode") in water_load.MODES
            else water_load.MODE_AUTO,
            pattern=pattern_from_dict(water.get("pattern")),
            cycle=replace(CycleAccumulator(), **{
                key: float(value) for key, value in cycle.items()
                if key in CycleAccumulator.__dataclass_fields__}),
            signal_on_at=_when(water.get("signal_on_at")),
            last_switch_change_at=_when(water.get("last_switch_change_at")),
            switch_was_on=bool(water.get("switch_was_on", False)),
            boost_requested=bool(water.get("boost_requested", False)),
            boosts_today=int(water.get("boosts_today", 0)),
            last_meter_wh=water.get("last_meter_wh"),
            day=_day(water.get("day")),
            day_kwh=float(water.get("day_kwh", 0.0)),
            outdoor_sum=float(water.get("outdoor_sum", 0.0)),
            outdoor_count=int(water.get("outdoor_count", 0)),
        ),
        pool=PoolMemory(
            mode=pool.get("mode") if pool.get("mode") in pool_load.MODES
            else pool_load.MODE_AUTO,
            v3_started_at=_when(pool.get("v3_started_at")),
            v3_last_ended_at=_when(pool.get("v3_last_ended_at")),
            last_speed_change_at=_when(pool.get("last_speed_change_at")),
            season=pool.get("season"),
        ),
    )


__all__ = [
    "CONTACTOR_DWELL_S",
    "PUMP_DWELL_S",
    "PoolMemory",
    "RouterMemory",
    "WaterMemory",
    "as_dict",
    "dwell_elapsed",
    "from_dict",
    "mean_draw_w",
    "pattern_from_dict",
    "smooth",
    "solar_share",
]
