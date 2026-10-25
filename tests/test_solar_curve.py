"""The hourly curve against the house: when the surplus can carry a load."""

import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.loads import water_heater as water_load
from logic.solar_curve import (
    DEFAULT_HOUSE_W,
    Sky,
    SkyMemory,
    memory_as_dict,
    memory_from_dict,
    parse_wh_hours,
    raw_day_kwh,
)
from logic.sun_outlook import ForecastNow, outlook

TZ = timezone(timedelta(hours=2))
DAY = date(2026, 10, 20)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def bell(peak_wh: float, day: date = DAY) -> dict:
    """A clear day: 9h to 18h, peaking at 14h."""
    shape = {9: .15, 10: .4, 11: .65, 12: .85, 13: .97, 14: 1.0, 15: .9, 16: .7, 17: .45, 18: .15}
    return {at(h, day=day): peak_wh * k for h, k in shape.items()}


def flat(wh: float, day: date = DAY) -> dict:
    """The same energy spread thin over a grey day."""
    return {at(h, day=day): wh for h in range(9, 19)}


# --------------------------------------------------------------- the curve


def test_an_hour_counts_only_if_its_surplus_carries_the_whole_load():
    sky = Sky(curve={at(13): 1500.0})   # 1500 − 406 house − 160 pump = 934 W
    assert sky.usable_kwh(at(13), at(14), load_w=650.0) == pytest.approx(0.65)
    assert sky.usable_kwh(at(13), at(14), load_w=800.0) == 0.0


def test_the_same_total_counts_only_where_it_peaks():
    """10 kWh in a midday bell runs the heater; 10 kWh spread thin never does."""
    peaky, grey = Sky(curve=bell(1700.0)), Sky(curve=flat(1000.0))
    assert raw_day_kwh(peaky.curve, DAY) == pytest.approx(raw_day_kwh(grey.curve, DAY), rel=0.1)
    assert peaky.usable_kwh(at(0), at(23), 650.0) > 2.0
    assert grey.usable_kwh(at(0), at(23), 650.0) == 0.0


def test_a_partial_hour_counts_its_share():
    sky = Sky(curve={at(13): 1500.0})
    assert sky.usable_kwh(at(13, 30), at(14), 650.0) == pytest.approx(0.325)


def test_the_learned_bias_scales_the_curve():
    sky = Sky(curve={at(13): 1000.0}, bias=1.5)
    assert sky.pv_w(at(13, 20)) == 1500.0


def test_a_hungrier_house_leaves_less():
    house = tuple(800.0 for _ in range(24))
    sky = Sky(curve={at(13): 1500.0}, house_w=house)
    assert sky.usable_kwh(at(13), at(14), 650.0) == 0.0


def test_best_and_remaining_from_now():
    sky = Sky(curve=bell(2000.0))
    assert sky.best_w(at(15, 10), at(23)) == pytest.approx(1800.0)
    assert sky.remaining_kwh(at(18), at(23)) == pytest.approx(0.3)


def test_a_day_the_curve_misses_has_no_total():
    assert Sky(curve=bell(2000.0)).day_kwh(DAY + timedelta(days=1)) is None


def test_the_dashboard_payload_folds_into_hours():
    curve = parse_wh_hours({"2026-10-20T13:00:00+02:00": 300, "2026-10-20T13:30:00+02:00": 400,
                            "garbage": 5})
    assert curve == {at(13): 700.0}


# ---------------------------------------------------------------- learning


def feed_day(memory: SkyMemory, curve: dict, pv_w: float, house_w: float,
             day: date = DAY) -> None:
    moment = at(0, day=day)
    while moment.date() == day:
        memory.feed(moment, pv_w=pv_w if 9 <= moment.hour < 19 else 0.0,
                    house_w=house_w, curve=curve)
        moment += timedelta(minutes=5)


def test_the_bias_learns_from_a_day_that_beat_its_forecast():
    memory, curve = SkyMemory(), flat(500.0)          # 5 kWh forecast
    feed_day(memory, curve, pv_w=1000.0, house_w=300.0)   # ~10 kWh real
    feed_day(memory, flat(500.0, DAY + timedelta(days=1)), 0, 300.0,
             day=DAY + timedelta(days=1))
    assert 1.0 < memory.bias < 1.3


def test_a_day_without_forecast_teaches_nothing():
    memory = SkyMemory()
    feed_day(memory, {}, pv_w=1000.0, house_w=300.0)
    feed_day(memory, {}, 0, 300.0, day=DAY + timedelta(days=1))
    assert memory.bias == 1.0


def test_the_house_profile_learns_each_hour():
    memory = SkyMemory()
    feed_day(memory, {}, pv_w=0, house_w=1000.0)
    assert memory.house_w[13] == pytest.approx(DEFAULT_HOUSE_W[13] + 0.1 * (1000 - 406))


def test_the_memory_round_trips():
    memory = SkyMemory(bias=1.3, house_w=[300.0] * 24, day=DAY, day_forecast_kwh=5.0,
                       day_actual_kwh=2.0)
    assert memory_as_dict(memory_from_dict(memory_as_dict(memory))) == memory_as_dict(memory)


@pytest.mark.parametrize("bad", [None, {}, {"bias": 99, "house_w": [1, 2], "day": "x"}])
def test_a_damaged_store_falls_back_to_defaults(bad):
    memory = memory_from_dict(bad)
    assert memory.bias <= 3.0 and len(memory.house_w) == 24


# ------------------------------------------------- the heater reads the curve


def test_tomorrow_s_usable_sun_decides_the_night(base_inputs, thr):
    night = replace(base_inputs, now=base_inputs.now.replace(hour=1, minute=0), is_hc=True,
                    tank_middle_c=46.0, energy_needed_kwh=2.0, forecast_tomorrow_kwh=25.0)
    sunny = water_load.decide(replace(night, solar_usable_tomorrow_kwh=3.5), thr)
    grey = water_load.decide(replace(night, solar_usable_tomorrow_kwh=1.0), thr)
    assert (sunny.signal_switch_on, grey.signal_switch_on) == (False, True)


def test_a_hold_the_curve_cannot_carry_is_not_committed(base_inputs, thr):
    noon = replace(base_inputs, grid_smooth_w=-1500.0, tank_middle_c=45.0, energy_needed_kwh=2.0)
    holds = water_load.decide(replace(noon, solar_usable_hold_kwh=1.2), thr)
    fades = water_load.decide(replace(noon, solar_usable_hold_kwh=0.2), thr)
    assert (holds.signal_switch_on, fades.signal_switch_on) == (True, False)


# ------------------------------------------------ the washer reads the curve


def test_the_washer_s_outlook_reads_the_curve():
    sky = ForecastNow(curve=bell(2000.0), now_w=1000.0)
    seen = outlook(at(15, 10), sky, pv_w=500.0)       # ratio 0.5
    assert seen.best_pv_w == pytest.approx(900.0)


def test_without_a_measure_the_learned_bias_corrects_the_curve():
    sky = ForecastNow(curve=bell(2000.0), now_w=100.0, bias=1.2)
    assert outlook(at(15, 10), sky, pv_w=50.0).best_pv_w == pytest.approx(2160.0)


def test_the_curve_says_when_the_day_is_over():
    sky = ForecastNow(curve=bell(2000.0), tomorrow_kwh=20.0)
    assert outlook(at(19, 30), sky, pv_w=0.0).production_over is True
