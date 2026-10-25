"""The day's solar curve, hour by hour, against what the house draws then.

A daily total says nothing about *when*: 8 kWh spread thin over a grey day
never lifts the export above the heater's 650 W, while the same 8 kWh in a
four-hour midday window runs it for two hours. So each hour is weighed on its
own: the forecast for that hour, scaled by a learned bias, less what the house
draws at that hour, and the hour counts only if what is left can carry the
load.

Two things are learned, both from the meters:

* the **bias** of the forecast, day by day: real production over forecast. Over
  29 days Forecast.Solar ran at a median 1.31 of the truth, spread 0.33-2.5;
  the learning corrects the steady part, whatever the source;
* the **house profile**: what the house draws each hour, without the heater
  and the pump. Defaults are the medians measured over 30 days.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

# Hourly forecast: start of the hour -> Wh expected in it.
Curve = dict[datetime, float]

# What the house draws by hour of day, heater and pump excluded: medians over
# 30 days (26/09-24/10). Night hours, unmeasured against the sun, at 200 W.
DEFAULT_HOUSE_W: tuple[float, ...] = (
    200, 200, 200, 200, 200, 200,       # 00-05
    201, 384, 344, 372, 324, 293,       # 06-11
    394, 406, 343, 329, 306, 363,       # 12-17
    539, 609, 456, 300, 250, 200,       # 18-23
)
# The pump's baseline, which runs whenever the sun does.
POOL_BASELINE_W: float = 160.0
BIAS_BOUNDS: tuple[float, float] = (0.3, 3.0)
# One day weighs this much in the learned bias: a fortnight's memory.
BIAS_ALPHA: float = 0.15
# One hour weighs this much in its slot of the house profile: ~ten days.
HOUSE_ALPHA: float = 0.1
# A day with less forecast than this teaches nothing about the bias.
MIN_DAY_FOR_BIAS_KWH: float = 1.0


def _hour(at: datetime) -> datetime:
    return at.replace(minute=0, second=0, microsecond=0)


def hours_between(curve: Curve, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """The curve's hours overlapping [start, end), each with its overlapping share."""
    out = []
    for hour, _ in sorted(curve.items()):
        stop = hour + timedelta(hours=1)
        overlap = (min(stop, end) - max(hour, start)).total_seconds() / 3600
        if overlap > 0:
            out.append((hour, overlap))
    return out


@dataclass(frozen=True)
class Sky:
    """A curve, and what has been learned about it and about the house."""

    curve: Curve
    bias: float = 1.0
    house_w: tuple[float, ...] = DEFAULT_HOUSE_W

    def pv_w(self, hour: datetime) -> float:
        """Mean production expected over that hour, corrected."""
        return self.curve.get(_hour(hour), 0.0) * self.bias

    def net_w(self, hour: datetime) -> float:
        """What is left over that hour once the house and the pump's v1 are fed."""
        return self.pv_w(hour) - self.house_w[hour.hour] - POOL_BASELINE_W

    def usable_kwh(self, start: datetime, end: datetime, load_w: float,
                   margin_w: float = 200.0) -> float:
        """Energy the sun can give a ``load_w`` load between ``start`` and ``end``.

        An hour counts only if its surplus carries the whole load with the
        margin the load's start gate asks for: below it, the load would not
        start, or would run on the grid.
        """
        total = 0.0
        for hour, share in hours_between(self.curve, start, end):
            if self.net_w(hour) >= load_w + margin_w:
                total += load_w * share
        return total / 1000

    def best_w(self, start: datetime, end: datetime) -> float | None:
        """The highest production expected from ``start`` until ``end``."""
        hours = hours_between(self.curve, _hour(start), end)
        return max((self.pv_w(hour) for hour, _ in hours), default=None)

    def remaining_kwh(self, start: datetime, end: datetime) -> float:
        return sum(self.pv_w(hour) * share
                   for hour, share in hours_between(self.curve, start, end)) / 1000

    def day_kwh(self, day: date) -> float | None:
        """The day's forecast total, corrected; None if the curve misses it."""
        hours = [hour for hour in self.curve if hour.date() == day]
        if not hours:
            return None
        return sum(self.pv_w(hour) for hour in hours) / 1000


def raw_day_kwh(curve: Curve, day: date) -> float | None:
    """The forecast total for a day, as the source gave it."""
    values = [wh for hour, wh in curve.items() if hour.date() == day]
    return sum(values) / 1000 if values else None


@dataclass
class SkyMemory:
    """What has been learned about the forecast and the house. Persisted."""

    bias: float = 1.0
    house_w: list[float] = field(default_factory=lambda: list(DEFAULT_HOUSE_W))
    # The day being watched: its forecast as known at its start, and what the
    # panels really gave so far.
    day: date | None = None
    day_forecast_kwh: float | None = None
    day_actual_kwh: float = 0.0
    # The hour being averaged into the house profile.
    hour_at: datetime | None = None
    hour_wh: float = 0.0
    hour_s: float = 0.0
    last_at: datetime | None = None

    def sky(self, curve: Curve) -> Sky:
        return Sky(curve=curve, bias=self.bias, house_w=tuple(self.house_w))

    def feed(self, at: datetime, *, pv_w: float | None, house_w: float | None,
             curve: Curve) -> None:
        """One reading of the panels and the house, every tick, local time."""
        elapsed = ((at - self.last_at).total_seconds()
                   if self.last_at is not None else 0.0)
        elapsed = min(max(elapsed, 0.0), 600.0)
        self.last_at = at
        self._roll_day(at.date(), curve)
        if pv_w is not None:
            self.day_actual_kwh += max(0.0, pv_w) * elapsed / 3_600_000
        self._roll_hour(at)
        if house_w is not None:
            self.hour_wh += max(0.0, house_w) * elapsed / 3600
            self.hour_s += elapsed

    def _roll_day(self, today: date, curve: Curve) -> None:
        if self.day == today:
            if self.day_forecast_kwh is None:
                self.day_forecast_kwh = raw_day_kwh(curve, today)
            return
        if (self.day is not None and self.day_forecast_kwh is not None
                and self.day_forecast_kwh >= MIN_DAY_FOR_BIAS_KWH):
            ratio = self.day_actual_kwh / self.day_forecast_kwh
            low, high = BIAS_BOUNDS
            ratio = max(low, min(high, ratio))
            self.bias = max(low, min(high, self.bias + BIAS_ALPHA * (ratio - self.bias)))
        self.day = today
        self.day_forecast_kwh = raw_day_kwh(curve, today)
        self.day_actual_kwh = 0.0

    def _roll_hour(self, at: datetime) -> None:
        hour = _hour(at)
        if self.hour_at == hour:
            return
        # Only an hour watched for most of its length teaches anything.
        if self.hour_at is not None and self.hour_s >= 2700:
            slot = self.hour_at.hour
            mean = self.hour_wh * 3600 / self.hour_s
            self.house_w[slot] += HOUSE_ALPHA * (mean - self.house_w[slot])
        self.hour_at, self.hour_wh, self.hour_s = hour, 0.0, 0.0


def memory_as_dict(memory: SkyMemory) -> dict:
    return {
        "bias": memory.bias,
        "house_w": [round(value, 1) for value in memory.house_w],
        "day": memory.day.isoformat() if memory.day else None,
        "day_forecast_kwh": memory.day_forecast_kwh,
        "day_actual_kwh": memory.day_actual_kwh,
    }


def memory_from_dict(data: dict | None) -> SkyMemory:
    """Restore what was learned; the hour in progress starts afresh."""
    data = data or {}
    house = data.get("house_w")
    low, high = BIAS_BOUNDS
    try:
        day = date.fromisoformat(data["day"]) if data.get("day") else None
    except (TypeError, ValueError):
        day = None
    memory = SkyMemory(
        bias=max(low, min(high, float(data.get("bias", 1.0)))),
        house_w=([float(v) for v in house] if isinstance(house, list) and len(house) == 24
                 else list(DEFAULT_HOUSE_W)),
        day=day,
        day_forecast_kwh=data.get("day_forecast_kwh"),
        day_actual_kwh=float(data.get("day_actual_kwh", 0.0)),
    )
    return replace(memory)


def parse_wh_hours(raw: dict[str, float] | None) -> Curve:
    """The Energy dashboard's ``wh_hours`` payload, as a curve.

    Keys are ISO timestamps of the period start, values Wh over that period.
    Sub-hourly periods are folded into their hour.
    """
    curve: Curve = {}
    for stamp, wh in (raw or {}).items():
        try:
            at = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        hour = _hour(at)
        curve[hour] = curve.get(hour, 0.0) + float(wh or 0.0)
    return curve


__all__ = [
    "DEFAULT_HOUSE_W",
    "Curve",
    "Sky",
    "SkyMemory",
    "hours_between",
    "memory_as_dict",
    "memory_from_dict",
    "parse_wh_hours",
    "raw_day_kwh",
]
