"""What the sun can still give today — the forecast, corrected by the panels.

Forecast.Solar draws a curve; the panels say what the sky is really doing.
Their ratio, measured now, scales what is left of the curve: the panels are
the best weather station the house has, on the very spot that matters. A grey
day shows up as a low ratio long before any forecast is revised.

When the ratio cannot be measured (dawn, dusk), the rain says enough: over
30 days of history, the panels gave a median 0.22 of the forecast while the
house's « pluie en cours » flag was on. The wet-rain sensor is no substitute —
it stays wet after the shower (median 0.42).

The best power still to come is read off the hourly curve when there is one:
its highest hour from now to the end of the day. Without a curve, off the
curve's shape:

* before the day's peak, a half-sine peaks at π/2 times its mean, so the
  remaining energy over the remaining daylight gives the peak to expect;
* after it, production only declines: now, or the next hour, is the best left.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .solar_curve import Curve, hours_between

# Below this forecast, the ratio is noise (dawn, dusk, a shadow on one panel).
MIN_FORECAST_FOR_RATIO_W: float = 200.0
# The forecast runs low here: dry, the panels gave a median 1.19 of it over
# 30 days, 2.6 at the 90th percentile. Beyond 3, the reading is not trusted.
RATIO_BOUNDS: tuple[float, float] = (0.05, 3.0)
# Median real/forecast ratio while it rains (185 samples, 30 days).
RAIN_RATIO: float = 0.22
# Less than this left today: production is over for the day.
PRODUCTION_OVER_KWH: float = 0.1
# A day worth waiting for. Measured over 45 days: at or above 16 kWh of
# production the median export was 5.0 kWh; under 12 kWh, 0.9 kWh.
GOOD_DAY_KWH: float = 16.0
# Peak power of a half-sine over its mean.
HALF_SINE_PEAK: float = math.pi / 2


@dataclass(frozen=True)
class ForecastNow:
    """Forecast.Solar's sensors, as read now. Any of them may be missing."""

    now_w: float | None = None
    next_hour_kwh: float | None = None
    remaining_today_kwh: float | None = None
    tomorrow_kwh: float | None = None
    peak_at: datetime | None = None
    sunset_at: datetime | None = None
    raining: bool | None = None
    # The hourly curve, as the source gave it, and its learned bias — used
    # when the panels cannot be compared with the forecast.
    curve: Curve | None = None
    bias: float = 1.0


@dataclass(frozen=True)
class Outlook:
    """The sun still to come, as far as can be told."""

    # Real production over forecast, now. None when it cannot be measured.
    ratio: float | None
    # Best production still to expect today, corrected by the ratio.
    best_pv_w: float | None
    production_over: bool
    tomorrow_good: bool


def realism(pv_w: float | None, forecast_w: float | None,
            raining: bool | None = None) -> float | None:
    """How the sky compares with the forecast right now: measured, else rain."""
    if pv_w is None or forecast_w is None or forecast_w < MIN_FORECAST_FOR_RATIO_W:
        return RAIN_RATIO if raining else None
    low, high = RATIO_BOUNDS
    return max(low, min(high, pv_w / forecast_w))


def _end_of_day(at: datetime) -> datetime:
    return at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def best_forecast_w(at: datetime, forecast: ForecastNow) -> float | None:
    """The highest forecast power still ahead today, uncorrected."""
    if forecast.curve:
        hours = hours_between(forecast.curve, at.replace(minute=0, second=0, microsecond=0),
                              _end_of_day(at))
        return max((forecast.curve[hour] for hour, _ in hours), default=0.0)
    candidates = [value for value in (
        forecast.now_w,
        forecast.next_hour_kwh * 1000 if forecast.next_hour_kwh is not None else None,
    ) if value is not None]
    if forecast.peak_at is None:
        return None  # before or after the peak? Without it, no curve.
    if at < forecast.peak_at:
        # Before the peak, now and the next hour understate what is coming:
        # without the rest of the day to read the peak from, say nothing.
        if (forecast.remaining_today_kwh is None or forecast.sunset_at is None
                or forecast.sunset_at <= at):
            return None
        hours = (forecast.sunset_at - at).total_seconds() / 3600
        candidates.append(forecast.remaining_today_kwh * 1000 / hours * HALF_SINE_PEAK)
    return max(candidates) if candidates else None


def outlook(at: datetime, forecast: ForecastNow, pv_w: float | None) -> Outlook:
    ratio = realism(pv_w, forecast.now_w, forecast.raining)
    if ratio is None and forecast.curve:
        ratio = forecast.bias
    best = best_forecast_w(at, forecast)
    remaining = forecast.remaining_today_kwh
    if forecast.curve:
        remaining = sum(forecast.curve[hour] * share for hour, share
                        in hours_between(forecast.curve, at, _end_of_day(at))) / 1000
    over = remaining is not None and remaining < PRODUCTION_OVER_KWH
    if over:
        best = 0.0
    elif best is not None and ratio is not None:
        # Without a ratio (dry, at dawn or dusk) the forecast is taken as is.
        best *= ratio
    return Outlook(
        ratio=ratio,
        best_pv_w=best,
        production_over=over,
        tomorrow_good=(forecast.tomorrow_kwh or 0.0) >= GOOD_DAY_KWH,
    )


__all__ = [
    "GOOD_DAY_KWH",
    "RAIN_RATIO",
    "ForecastNow",
    "Outlook",
    "best_forecast_w",
    "outlook",
    "realism",
]
