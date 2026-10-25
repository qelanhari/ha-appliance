"""The sun still to come, and the washer that stops waiting for one that won't."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.solar_start import SolarStarter
from logic.sun_outlook import (
    RAIN_RATIO,
    ForecastNow,
    best_forecast_w,
    outlook,
    realism,
)

NOON = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
AFTERNOON = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ ratio


def test_the_panels_measure_the_sky():
    assert realism(400.0, 2000.0) == pytest.approx(0.2)


def test_a_sunnier_sky_than_forecast_is_believed_up_to_three():
    assert realism(9000.0, 2000.0) == 3.0


def test_too_little_forecast_to_compare_is_no_measure():
    assert realism(50.0, 100.0) is None


def test_the_rain_stands_in_when_the_panels_cannot_be_compared():
    assert realism(50.0, 100.0, raining=True) == RAIN_RATIO


def test_measured_panels_outrank_the_rain_flag():
    assert realism(1800.0, 2000.0, raining=True) == pytest.approx(0.9)


# ------------------------------------------------------------- the curve


def test_before_the_peak_the_rest_of_the_day_gives_the_peak_to_expect():
    sky = ForecastNow(now_w=800.0, next_hour_kwh=1.2, remaining_today_kwh=12.0,
                      peak_at=NOON + timedelta(hours=1), sunset_at=NOON + timedelta(hours=6))
    assert best_forecast_w(NOON, sky) == pytest.approx(12000 / 6 * 1.5708, rel=0.001)


def test_after_the_peak_production_only_declines():
    sky = ForecastNow(now_w=1500.0, next_hour_kwh=1.2, remaining_today_kwh=12.0,
                      peak_at=NOON, sunset_at=NOON + timedelta(hours=4))
    assert best_forecast_w(AFTERNOON, sky) == 1500.0


def test_nothing_known_is_no_curve():
    assert best_forecast_w(NOON, ForecastNow()) is None


def test_a_grey_sky_scales_the_curve_down():
    sky = ForecastNow(now_w=2000.0, next_hour_kwh=1.8, peak_at=NOON)
    assert outlook(AFTERNOON, sky, 400.0).best_pv_w == pytest.approx(400.0)


def test_production_over_for_the_day_leaves_nothing():
    sky = ForecastNow(now_w=0.0, remaining_today_kwh=0.05, tomorrow_kwh=20.0, peak_at=NOON)
    seen = outlook(AFTERNOON, sky, 0.0)
    assert (seen.best_pv_w, seen.production_over, seen.tomorrow_good) == (0.0, True, True)


# ------------------------------------------------- the washer stops waiting


def grey_afternoon(starter: SolarStarter, minutes: int, sky: ForecastNow, *,
                   start: datetime = AFTERNOON, grid_w: float = 500.0,
                   pv_w: float = 400.0, **options) -> list:
    decisions = []
    for step in range(minutes * 2):
        at = start + timedelta(seconds=30 * step)
        starter.feed_grid(at, grid_w)
        starter.feed_pv(at, pv_w)
        decisions.append(starter.decide(at, armed=True, washer_running=False,
                                        enabled=True, sky=sky, **options))
    return decisions


GREY = ForecastNow(now_w=2000.0, next_hour_kwh=1.8, remaining_today_kwh=4.0,
                   peak_at=NOON, sunset_at=AFTERNOON + timedelta(hours=3))


def test_it_starts_once_the_sun_has_been_out_of_reach_for_a_whole_window():
    decisions = grey_afternoon(SolarStarter(), 70, GREY)
    started = [d for d in decisions if d.start]
    assert started and started[0].reason == "soleil hors d'atteinte aujourd'hui"


def test_a_passing_cloud_is_not_the_day():
    assert not any(d.start for d in grey_afternoon(SolarStarter(), 45, GREY))


def test_on_a_rouge_peak_hour_it_waits_for_the_off_peak():
    decisions = grey_afternoon(SolarStarter(), 70, GREY, expensive_now=True)
    assert not any(d.start for d in decisions)
    assert decisions[-1].reason.endswith("heures creuses attendues")


def test_armed_at_night_before_a_good_day_it_waits_for_tomorrow():
    night = ForecastNow(now_w=0.0, remaining_today_kwh=0.0, tomorrow_kwh=20.0, peak_at=NOON)
    decisions = grey_afternoon(SolarStarter(), 70, night, pv_w=0.0, grid_w=400.0,
                               start=AFTERNOON + timedelta(hours=8))
    assert not any(d.start for d in decisions)
    assert decisions[-1].reason == "soleil de demain attendu"


def test_armed_at_night_before_a_poor_day_it_starts():
    night = ForecastNow(now_w=0.0, remaining_today_kwh=0.0, tomorrow_kwh=6.0, peak_at=NOON)
    decisions = grey_afternoon(SolarStarter(), 70, night, pv_w=0.0, grid_w=400.0,
                               start=AFTERNOON + timedelta(hours=8))
    assert any(d.start for d in decisions)


def test_without_any_forecast_it_keeps_waiting():
    assert not any(d.start for d in grey_afternoon(SolarStarter(), 70, ForecastNow()))


def test_sun_that_can_still_come_is_waited_for():
    bright = ForecastNow(now_w=2000.0, next_hour_kwh=3.5, remaining_today_kwh=15.0,
                         peak_at=AFTERNOON + timedelta(hours=1),
                         sunset_at=AFTERNOON + timedelta(hours=4))
    decisions = grey_afternoon(SolarStarter(), 70, bright, pv_w=2200.0, grid_w=-300.0)
    assert not any(d.start and "hors d'atteinte" in d.reason for d in decisions)


def test_the_lower_bar_is_a_held_export_of_a_few_hundred_watts():
    assert 200.0 < SolarStarter().bar_w(None) < 800.0


def test_before_the_peak_without_the_rest_of_the_day_nothing_is_said():
    sky = ForecastNow(now_w=800.0, next_hour_kwh=1.2, peak_at=NOON + timedelta(hours=1))
    assert best_forecast_w(NOON, sky) is None


def test_without_the_peak_time_nothing_is_said():
    assert best_forecast_w(NOON, ForecastNow(now_w=800.0, next_hour_kwh=1.2)) is None
