"""Starting the armed washer on solar surplus, replayed on recorded days.

Each ``grid_*`` fixture holds the grid meter (negative is export), the
Forecast.Solar figures and the Tempo price over the same day. The replay
asks the question every 30 s, as the coordinator's clock does.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.samples import time_weighted_mean
from logic.solar_start import (
    WASHER_PROFILE,
    Forecast,
    SolarStartConfig,
    SolarStarter,
    best_hour,
    estimate,
    profile_for,
)
from logic.sun_outlook import ForecastNow

FIXTURES = Path(__file__).resolve().parent / "fixtures"
UNAVAILABLE = ("unknown", "unavailable")


def _series(raw: list, parse=float) -> list[tuple[datetime, object]]:
    return [(datetime.fromisoformat(stamp),
             None if value in UNAVAILABLE else parse(value)) for stamp, value in raw]


def _at(series: list, at: datetime):
    value = None
    for stamp, reading in series:
        if stamp > at:
            break
        value = reading
    return value


def day(name: str, *, armed_from: str = "00:00", config: SolarStartConfig | None = None,
        with_sky: bool = False, **conditions) -> list:
    """Every decision of the day, (at, decision), the washer armed from ``armed_from``."""
    payload = json.loads((FIXTURES / f"{name}.json").read_text())
    context = payload["context"]
    pv = _series(context["pv_w"])
    peaks = _series(context["forecast_peak_at"], datetime.fromisoformat)
    now_w, next_kwh = _series(context["forecast_now_w"]), _series(context["forecast_next_hour_kwh"])
    prices = _series(context["price_eur_kwh"])
    starter = SolarStarter(config)
    conditions = {"washer_running": False, "enabled": True} | conditions
    out, next_check, pv_index = [], None, 0
    for stamp, watts in payload["points"]:
        at = datetime.fromisoformat(stamp)
        starter.feed_grid(at, watts)
        while pv_index < len(pv) and pv[pv_index][0] <= at:
            if pv[pv_index][1] is not None:
                starter.feed_pv(*pv[pv_index])
            pv_index += 1
        if next_check is not None and at < next_check:
            continue
        next_check = at + timedelta(seconds=30)
        forecast_now, forecast_next = _at(now_w, at), _at(next_kwh, at)
        forecast = (Forecast(forecast_now, forecast_next * 1000)
                    if forecast_now is not None and forecast_next is not None else None)
        armed = conditions.get("armed", at.strftime("%H:%M") >= armed_from)
        out.append((at, starter.decide(
            at, armed=armed, washer_running=conditions["washer_running"],
            enabled=conditions["enabled"], price=_at(prices, at),
            forecast=forecast, peak_at=_at(peaks, at),
            sky=(ForecastNow(now_w=forecast_now, next_hour_kwh=forecast_next,
                             peak_at=_at(peaks, at)) if with_sky else None))))
    return out


def first_start(name: str, **options) -> datetime | None:
    return next((at for at, decision in day(name, **options) if decision.start), None)


# --------------------------------------------------------------------------
# The profile the estimate rests on
# --------------------------------------------------------------------------

def test_the_profile_is_the_measured_cycle():
    """317 Wh over 84.8 minutes on 3 Oct (thinq_washer_sat_1720)."""
    minutes = sum(m for m, _ in WASHER_PROFILE)
    energy = sum(m * w for m, w in WASHER_PROFILE) / 60
    assert minutes == pytest.approx(84.8, abs=0.1)
    assert energy == pytest.approx(317, rel=0.03)


def test_a_longer_programme_stretches_the_tumbling_not_the_heating():
    longer = profile_for(timedelta(minutes=120))
    assert sum(m for m, _ in longer) == pytest.approx(120)
    assert longer[:-1] == WASHER_PROFILE[:-1]


def test_half_the_cycle_from_the_sun_is_half_the_price():
    """Nothing is sold back: the saving is the solar share."""
    result = estimate(WASHER_PROFILE, 420.0, None, 0.1612)
    assert result.saving == pytest.approx(result.solar_wh / result.energy_wh)
    assert result.grid_cost_eur == pytest.approx(0.317 * 0.1612, rel=0.03)
    assert result.cost_eur == pytest.approx(result.grid_cost_eur * (1 - result.saving),
                                            abs=0.001)


def test_a_sunny_spell_covers_the_heating_too():
    """3 kW of panels on a clear day: 2.5 kW of export held covers it all."""
    assert estimate(WASHER_PROFILE, 2500.0, None, 0.1612).saving == pytest.approx(1.0)


def test_the_heating_is_met_minute_by_minute_not_by_an_hourly_average():
    """2.1 kW for 4.5 min against a 500 W surplus: only 500 W of it is solar."""
    result = estimate(WASHER_PROFILE, 500.0, None, None)
    tumbling = 73.0 * 92.0 / 60
    assert result.solar_wh < tumbling + 500.0 * (1.4 + 1.5 + 4.4 + 4.5) / 60 + 1


def test_a_falling_forecast_counts_against_the_end_of_the_cycle():
    falling = Forecast(now_w=2000.0, next_hour_w=200.0)
    steady = estimate(profile_for(timedelta(minutes=120)), 400.0, None, None)
    dimming = estimate(profile_for(timedelta(minutes=120)), 400.0, falling, None)
    assert dimming.solar_wh < steady.solar_wh


def test_a_rising_forecast_is_not_counted_on():
    rising = Forecast(now_w=200.0, next_hour_w=2000.0)
    assert rising.decline_w == 0



# --------------------------------------------------------------------------
# Recorded days
# --------------------------------------------------------------------------

def realised_saving(name: str, start: datetime) -> float:
    """What the sun actually gave the cycle, laid over the export that followed."""
    payload = json.loads((FIXTURES / f"{name}.json").read_text())
    points = [(datetime.fromisoformat(stamp), watts) for stamp, watts in payload["points"]]
    clock, energy, solar = start, 0.0, 0.0
    for minutes, watts in WASHER_PROFILE:
        for step in [1.0] * int(minutes) + [minutes - int(minutes)]:
            grid = time_weighted_mean(points, clock, clock + timedelta(minutes=step)) or 0.0
            energy += watts * step / 60
            solar += min(watts, max(0.0, -grid)) * step / 60
            clock += timedelta(minutes=step)
    return solar / energy


def test_a_steady_morning_holds_out_for_the_heating_covered():
    """23 Sept, 10:18: half the cycle could already come from the sun, but
    production is steady and the peak ahead — so half is not good enough yet."""
    early = [d for at, d in day("grid_wed_0923") if at.strftime("%H:%M") == "10:18"]
    assert any(d.estimate and d.estimate.saving >= 0.5 for d in early)
    assert not any(d.start for d in early)
    assert all(d.required == 0.9 for d in early if d.estimate)


def test_it_settles_for_half_once_production_turns_unsteady_and_gains_by_waiting():
    """Started at 10:18 the cycle would have been 59 % solar; held back until
    the clouds came at 11:52, 65 %."""
    started = next((at, d) for at, d in day("grid_wed_0923") if d.start)
    assert started[1].reason == "production instable"
    assert realised_saving("grid_wed_0923", started[0]) \
        > realised_saving("grid_wed_0923", started[0].replace(hour=10, minute=18))


def test_a_patchy_afternoon_does_not():
    """3 Oct: ~500 W of export from 14:10, gone by 14:30. Started at any time
    that day, the cycle would have saved 35 % at best."""
    assert first_start("grid_sat_1000") is None


def test_the_same_afternoon_it_stops_waiting_for_a_sun_that_never_came():
    """Read against the sky, 3 Oct had nothing left to offer by mid-afternoon:
    the panels well under a forecast already past its peak."""
    started = next(((at, d) for at, d in day("grid_sat_1000", with_sky=True) if d.start),
                   None)
    assert started is not None
    assert started[1].reason == "soleil hors d'atteinte aujourd'hui"
    assert started[0].hour >= 13


def test_a_sunny_day_starts_as_before_with_the_sky_read():
    plain = next(at for at, d in day("grid_wed_0923") if d.start)
    read = next(at for at, d in day("grid_wed_0923", with_sky=True) if d.start)
    assert read == plain


def test_a_quarter_hour_of_sun_would_have_been_fooled():
    """The guard behind the window: at 15 min the same day starts at 14:25,
    and the cloud that follows leaves it a quarter solar."""
    config = SolarStartConfig(window=timedelta(minutes=15))
    started = first_start("grid_sat_1000", config=config)
    assert started is not None
    assert realised_saving("grid_sat_1000", started) < 0.5


def test_armed_late_it_takes_half_since_the_peak_is_behind():
    """23 Sept, 17:30: an hour and a half of light left — 52 % solar."""
    started = next((at, d) for at, d in day("grid_wed_0923", armed_from="17:30")
                   if d.start)
    assert started[1].reason == "pic de production passé"
    assert realised_saving("grid_wed_0923", started[0]) >= 0.5


def test_armed_after_the_sun_has_gone_it_waits():
    assert first_start("grid_wed_0923", armed_from="18:00") is None


@pytest.mark.parametrize("conditions", [
    {"armed": None}, {"armed": False}, {"washer_running": None},
    {"washer_running": True}, {"enabled": False},
])
def test_nothing_starts_unless_every_condition_is_known(conditions):
    """A flag ThinQ cannot report is no permission to start a machine."""
    assert first_start("grid_wed_0923", **conditions) is None


# --------------------------------------------------------------------------
# The starter on its own
# --------------------------------------------------------------------------

T0 = datetime(2026, 9, 23, 12, 0)


def _exporting(starter: SolarStarter, minutes: int, watts: float = -1500.0) -> datetime:
    at = T0
    for second in range(0, minutes * 60 + 1, 30):
        at = T0 + timedelta(seconds=second)
        starter.feed_grid(at, watts)
    return at


def _ask(starter: SolarStarter, at: datetime):
    return starter.decide(at, armed=True, washer_running=False, enabled=True,
                          price=0.1612)


def _steady_pv(starter: SolarStarter, minutes: int, watts: float = 2500.0) -> None:
    for second in range(0, minutes * 60 + 1, 30):
        starter.feed_pv(T0 + timedelta(seconds=second), watts)


def test_the_heating_covered_starts_whatever_the_outlook():
    starter = SolarStarter()
    _steady_pv(starter, 31)
    at = _exporting(starter, 31, watts=-2300.0)
    decision = starter.decide(at, armed=True, washer_running=False, enabled=True,
                              peak_at=at + timedelta(hours=3))
    assert decision.start and decision.reason == "chauffe couverte"


def test_half_waits_while_steady_before_the_peak():
    starter = SolarStarter()
    _steady_pv(starter, 31)
    at = _exporting(starter, 31, watts=-600.0)
    decision = starter.decide(at, armed=True, washer_running=False, enabled=True,
                              peak_at=at + timedelta(hours=2))
    assert decision.estimate.saving >= 0.5
    assert not decision.start


def test_an_unknown_peak_is_not_a_promise_of_better():
    starter = SolarStarter()
    _steady_pv(starter, 31)
    at = _exporting(starter, 31, watts=-600.0)
    decision = starter.decide(at, armed=True, washer_running=False, enabled=True)
    assert decision.start and decision.reason == "pic de production passé"


def test_a_single_reading_is_not_half_an_hour_of_sun():
    """A restart mid-afternoon must not start the machine on one sample."""
    starter = SolarStarter()
    starter.feed_grid(T0, -2000.0)
    decision = _ask(starter, T0 + timedelta(minutes=1))
    assert decision.waiting and not decision.start and decision.estimate is None


def test_the_command_is_asked_once_then_again_only_after_a_while():
    starter = SolarStarter()
    at = _exporting(starter, 31)
    assert _ask(starter, at).start
    assert not _ask(starter, at + timedelta(minutes=1)).start
    assert _ask(starter, at + timedelta(minutes=5)).start


def test_disarming_resets_the_attempt():
    starter = SolarStarter()
    at = _exporting(starter, 31)
    assert _ask(starter, at).start
    starter.decide(at, armed=False, washer_running=False, enabled=True)
    assert _ask(starter, at + timedelta(seconds=30)).start


def test_the_estimate_is_reported_while_waiting():
    starter = SolarStarter()
    at = _exporting(starter, 31)
    estimate_now = _ask(starter, at).estimate
    assert round(estimate_now.surplus_w) == 1500
    assert estimate_now.cost_eur < estimate_now.grid_cost_eur



def _ask_asleep(starter: SolarStarter, at: datetime):
    return starter.decide(at, armed=True, washer_running=False, enabled=True,
                          asleep=True, price=0.1612)


def test_an_armed_washer_asleep_is_woken_not_started():
    """Armed, it dozes off after ~12 min (10 Oct 2026): start alone would not take."""
    starter = SolarStarter()
    decision = _ask_asleep(starter, _exporting(starter, 31))
    assert decision.wake and not decision.start


def test_it_is_woken_once_then_again_only_after_a_while():
    starter = SolarStarter()
    at = _exporting(starter, 31)
    assert _ask_asleep(starter, at).wake
    assert not _ask_asleep(starter, at + timedelta(minutes=1)).wake
    assert _ask_asleep(starter, at + timedelta(minutes=5)).wake


def test_once_awake_it_starts_without_waiting():
    starter = SolarStarter()
    at = _exporting(starter, 31)
    _ask_asleep(starter, at)
    assert _ask(starter, at + timedelta(seconds=20)).start


def test_a_thin_surplus_does_not_wake_it():
    starter = SolarStarter()
    decision = _ask_asleep(starter, _exporting(starter, 31, watts=-150.0))
    assert not decision.wake


def test_a_programme_not_remembered_is_not_started():
    """ThinQ's own start would run the default programme (26 Oct 2026)."""
    starter = SolarStarter()
    decision = starter.decide(_exporting(starter, 31), armed=True, washer_running=False,
                              enabled=True, price=0.1612, program_ready=False)
    assert not decision.start and decision.reason == "programme non mémorisé"


def test_a_programme_not_remembered_is_not_woken_either():
    starter = SolarStarter()
    decision = starter.decide(_exporting(starter, 31), armed=True, washer_running=False,
                              enabled=True, asleep=True, price=0.1612, program_ready=False)
    assert not decision.wake


# --------------------------------------------------------------------------
# Out of reach of the bar: the best hour ahead
# --------------------------------------------------------------------------

# A late-October day of ~9 kWh, peaking at 1.9 kW at 13:00 (Open-Meteo shape).
DAY = datetime(2026, 10, 27)
HOUSE = (350.0,) * 24


def _october(hour: float) -> float:
    return max(0.0, 1900.0 - 280.0 * abs(hour - 13) ** 1.6)


def _curve(*days: datetime, shift: float = 0.0) -> dict[datetime, float]:
    return {d + timedelta(hours=h): _october(h + 0.5 - shift)
            for d in days for h in range(24)}


CURVE = _curve(DAY - timedelta(days=1), DAY)
FLAT = {hour: 0.0 for hour in CURVE}


def _simulate(start: datetime, hours: float, *, sky_share: float = 1.0,
              curve: dict | None = None, late_curve: dict | None = None,
              expensive_now: bool = False) -> list:
    """Armed from ``start``, the panels at ``sky_share`` of the forecast:
    every decision, (at, decision), up to the first wake or start.
    ``late_curve`` replaces the forecast from 11:00."""
    starter, out = SolarStarter(), []
    for second in range(0, int(hours * 3600), 30):
        at = start + timedelta(seconds=second)
        pv = sky_share * _october(at.hour + 0.5)
        starter.feed_grid(at, HOUSE[at.hour] - pv)
        starter.feed_pv(at, pv)
        sky = CURVE if curve is None else curve
        if late_curve is not None and at.hour >= 11:
            sky = late_curve
        decision = starter.decide(
            at, armed=True, washer_running=False, enabled=True,
            total=timedelta(minutes=86), expensive_now=expensive_now,
            sky=ForecastNow(now_w=sky.get(at.replace(minute=0, second=0)),
                            tomorrow_kwh=9.2, curve=sky, house_w=HOUSE))
        out.append((at, decision))
        if decision.start or decision.wake:
            break
    return out


def _started(decisions: list) -> tuple[datetime, object] | None:
    at, decision = decisions[-1]
    return (at, decision) if decision.start or decision.wake else None


def test_the_best_hour_is_the_peak_less_the_house():
    hour, saving = best_hour(DAY.replace(hour=9), ForecastNow(curve=CURVE), 0.4,
                             profile_for(timedelta(minutes=86)), 0.15)
    assert hour.hour in (12, 13)
    assert 0.3 < saving < 0.6


def test_once_the_day_is_over_the_best_hour_is_tomorrows():
    hour, _ = best_hour((DAY - timedelta(days=1)).replace(hour=21), ForecastNow(curve=CURVE),
                        None, profile_for(timedelta(minutes=86)), 0.15)
    assert hour.date() == DAY.date() and hour.hour in (12, 13)


def test_armed_in_the_evening_it_waits_for_tomorrow():
    """26 Oct: tomorrow at 9 kWh, under the 16 kWh « good day » — before, it
    started at 22:00 on the grid."""
    decisions = _simulate((DAY - timedelta(days=1)).replace(hour=21), 18)
    at, decision = _started(decisions)
    assert at.date() == DAY.date() and decision.estimate.saving >= 0.5
    night = [d for t, d in decisions if t.hour == 23]
    assert night[-1].reason.startswith("meilleure heure prévue : demain ")


def test_a_grey_day_starts_at_its_best_hour_not_as_soon_as_it_gives_up():
    """Panels at a third of the forecast: before, it started mid-morning with
    nothing from the sun; at its best hour the sun covers ~40 % of the cycle."""
    at, decision = _started(_simulate(DAY.replace(hour=9), 8, sky_share=0.35))
    assert decision.reason == "meilleure heure du jour"
    assert at.hour in (12, 13)
    assert decision.estimate.saving > 0.3


def test_the_hour_waited_for_is_reported():
    waiting = [d for _, d in _simulate(DAY.replace(hour=9), 8, sky_share=0.35)
               if d.target_at is not None]
    assert waiting and waiting[0].reason.startswith("meilleure heure prévue : ")
    assert waiting[0].target_saving >= 0.15


def test_nothing_worth_waiting_for_starts_now():
    at, decision = _started(_simulate(DAY.replace(hour=9), 2, sky_share=0.0, curve=FLAT))
    assert decision.reason == "soleil hors d'atteinte aujourd'hui"
    assert at < DAY.replace(hour=10, minute=30)


def test_a_rouge_peak_still_waits_for_the_off_peak_when_nothing_is_worth_it():
    assert _started(_simulate(DAY.replace(hour=9), 2, sky_share=0.0, curve=FLAT,
                              expensive_now=True)) is None


def test_a_later_forecast_does_not_push_the_hour_back():
    """From 11:00 the forecast makes 15:00 a little better than noon, still
    short of the bar: the hour already settled on stands."""
    late = dict(CURVE) | {DAY.replace(hour=15): 1850.0}
    at, decision = _started(_simulate(DAY.replace(hour=9), 8, sky_share=0.35,
                                      late_curve=late))
    assert at.hour == 12 and decision.reason == "meilleure heure du jour"


def test_the_expected_start_is_the_first_hour_that_meets_the_bar():
    """A clear day armed at 8:00: expected as soon as the forecast reaches half."""
    waiting = [d for t, d in _simulate(DAY.replace(hour=8), 1) if d.expected_at]
    assert waiting
    assert all(d.expected_at.date() == DAY.date() and 9 <= d.expected_at.hour <= 12
               for d in waiting)
    assert all(d.expected_saving >= 0.5 for d in waiting)


def test_the_expected_start_is_tomorrow_when_armed_in_the_evening():
    decisions = _simulate((DAY - timedelta(days=1)).replace(hour=20), 3)
    expected = [d.expected_at for _, d in decisions if d.expected_at]
    assert expected and all(at.date() == DAY.date() for at in expected)


def test_the_expected_start_is_never_in_the_past():
    for at, decision in _simulate(DAY.replace(hour=9), 8, sky_share=0.35):
        assert decision.expected_at is None or decision.expected_at >= at


def test_nothing_is_expected_from_a_sunless_forecast():
    assert not any(d.expected_at for _, d in
                   _simulate(DAY.replace(hour=9), 1, sky_share=0.0, curve=FLAT))
