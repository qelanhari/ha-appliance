"""Tests for the pool pump brain, ported from ha-pool.

The cases are the original ones; where the two corrections change an outcome
the test says so and asserts the new behaviour.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from dataclasses import replace
from datetime import timedelta

import pytest

from logic.loads.pool import (  # noqa: E402
    MODE_AUTO,
    MODE_OFF,
    MODE_V1,
    MODE_V2,
    MODE_V3,
    MODE_WINTER,
    TEMPO_RED,
    Decision,
    Inputs,
    decide,
)


# ---------------------------------------------------------------------------
# Manual mode
# ---------------------------------------------------------------------------


class TestManualMode:
    def test_off(self, pool_inputs, pool_thr):
        d = decide(replace(pool_inputs, mode=MODE_OFF), pool_thr)
        assert d.target_speed == 0
        assert "manual=off" in d.reason

    def test_v1(self, pool_inputs, pool_thr):
        d = decide(replace(pool_inputs, mode=MODE_V1), pool_thr)
        assert d.target_speed == 1

    def test_v2(self, pool_inputs, pool_thr):
        d = decide(replace(pool_inputs, mode=MODE_V2), pool_thr)
        assert d.target_speed == 2

    def test_v3(self, pool_inputs, pool_thr):
        d = decide(replace(pool_inputs, mode=MODE_V3), pool_thr)
        assert d.target_speed == 3
        assert d.enter_v3 is True
        assert d.leave_v3 is False

    def test_manual_overrides_no_daylight(self, pool_inputs, pool_thr):
        """Manual mode wins even at night."""
        d = decide(replace(pool_inputs, mode=MODE_V2, daylight=False), pool_thr)
        assert d.target_speed == 2

    def test_leaving_v3_via_manual_drop(self, pool_inputs, pool_thr):
        """If pump was at v3 and user picks manual=v1, mark leave_v3."""
        d = decide(
            replace(pool_inputs, pump_speed=3, mode=MODE_V1),
            pool_thr,
        )
        assert d.target_speed == 1
        assert d.leave_v3 is True


# ---------------------------------------------------------------------------
# Daylight gate
# ---------------------------------------------------------------------------


class TestDaylightGate:
    def test_night_forces_v1(self, pool_inputs, pool_thr):
        d = decide(replace(pool_inputs, daylight=False, grid_w=-3000.0), pool_thr)
        assert d.target_speed == 1
        assert "night" in d.reason

    def test_night_drops_v3(self, pool_inputs, pool_thr):
        """A v3 session at the moment night falls should leave v3."""
        d = decide(
            replace(
                pool_inputs,
                daylight=False,
                pump_speed=3,
                v3_started_at=pool_inputs.now - timedelta(minutes=2),
            ),
            pool_thr,
        )
        assert d.target_speed == 1
        assert d.leave_v3 is True


# ---------------------------------------------------------------------------
# v3 candidate gating: cooldown, warm, surplus
# ---------------------------------------------------------------------------


class TestV3Candidate:
    def test_v3_when_warm_and_surplus(self, pool_inputs, pool_thr):
        # Currently at v1 (160 W). To bump to v3 (1100 W) we'd add 940 W.
        # Need grid + 940 < -200 → grid < -1140.
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,
                water_temp_c=26.0,  # warm
                v3_last_ended_at=None,
            ),
            pool_thr,
        )
        assert d.target_speed == 3
        assert d.enter_v3 is True
        assert "v3" in d.reason

    def test_no_v3_if_not_warm(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,
                water_temp_c=20.0,  # not warm
                air_temp_c=20.0,
                v3_last_ended_at=None,
            ),
            pool_thr,
        )
        # Surplus exists but neither water nor air is warm enough.
        # v2 ALSO requires warmth, so we fall all the way back to v1.
        assert d.target_speed == 1

    def test_no_v3_if_in_cooldown(self, pool_inputs, pool_thr):
        # 15 min cooldown still active (default cooldown is 30 min).
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,
                water_temp_c=26.0,
                v3_last_ended_at=pool_inputs.now - timedelta(minutes=15),
            ),
            pool_thr,
        )
        assert d.target_speed == 2  # surplus enough for v2

    def test_v3_after_cooldown_expires(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,
                water_temp_c=26.0,
                v3_last_ended_at=pool_inputs.now - timedelta(minutes=45),
            ),
            pool_thr,
        )
        assert d.target_speed == 3

    def test_no_v3_if_surplus_too_small(self, pool_inputs, pool_thr):
        # grid -800, pump v1 (160) → bump to v3 = -800+940 = +140 W (importing).
        # Should NOT be allowed.
        d = decide(
            replace(pool_inputs, grid_w=-800.0, water_temp_c=26.0),
            pool_thr,
        )
        assert d.target_speed != 3


# ---------------------------------------------------------------------------
# v3 session in progress
# ---------------------------------------------------------------------------


class TestV3InProgress:
    def test_hold_v3_under_cap(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,        # surplus still present (v3 already on)
                pump_speed=3,
                v3_started_at=pool_inputs.now - timedelta(minutes=5),
            ),
            pool_thr,
        )
        assert d.target_speed == 3
        assert d.enter_v3 is False
        assert d.leave_v3 is False

    def test_drop_at_15min_cap(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,
                pump_speed=3,
                v3_started_at=pool_inputs.now - timedelta(minutes=15, seconds=1),
            ),
            pool_thr,
        )
        assert d.target_speed != 3
        assert d.leave_v3 is True
        assert "cap" in d.reason

    def test_mid_session_abort_when_surplus_drops(self, pool_inputs, pool_thr):
        # While at v3 (1100 W), grid 100 W → if dropped to v2 we'd do 100+(500-1100)=-500 (export OK)
        # But at v3 itself, can_bump_to(3) checks delta=0 → expected = 100, not < -200 → abort.
        d = decide(
            replace(
                pool_inputs,
                grid_w=100.0,
                pump_speed=3,
                v3_started_at=pool_inputs.now - timedelta(minutes=3),
            ),
            pool_thr,
        )
        assert d.target_speed != 3
        assert d.leave_v3 is True
        assert "surplus lost" in d.reason


# ---------------------------------------------------------------------------
# v2 candidate
# ---------------------------------------------------------------------------


class TestV2Candidate:
    def test_v2_with_modest_surplus(self, pool_inputs, pool_thr):
        # At v1 (160W). Bump to v2 (500W) adds 340W. Need grid + 340 < -200 → grid < -540.
        # v2 also requires warmth — provide a warm water_temp.
        d = decide(replace(pool_inputs, grid_w=-700.0, water_temp_c=26.0), pool_thr)
        assert d.target_speed == 2

    def test_no_v2_if_surplus_but_cool(self, pool_inputs, pool_thr):
        """When the day is cool, even with surplus we stay at v1 (no filtration urgency)."""
        d = decide(
            replace(pool_inputs, grid_w=-700.0, water_temp_c=20.0, air_temp_c=20.0),
            pool_thr,
        )
        assert d.target_speed == 1

    def test_v2_drops_to_v1_when_surplus_disappears(self, pool_inputs, pool_thr):
        # At v2 (500W). Bump to v2 itself = no delta. Need grid < -200 to stay.
        # grid 0 → not enough surplus to *justify* v2 but no flap mechanism here:
        # the decision is recomputed each tick, so v2 stays only if can_bump_to(2)
        # is True from the v1 baseline. From v2, asking can_bump_to(2) is delta=0,
        # condition: grid < -200. If grid >= -200, drop to v1.
        d = decide(replace(pool_inputs, grid_w=0.0, pump_speed=2), pool_thr)
        assert d.target_speed == 1


# ---------------------------------------------------------------------------
# Force skim button
# ---------------------------------------------------------------------------


class TestForceSkim:
    def test_force_skim_starts_v3(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,        # surplus available
                force_skim_requested=True,
                v3_last_ended_at=None,
            ),
            pool_thr,
        )
        assert d.target_speed == 3
        assert d.enter_v3 is True
        assert "force-skim" in d.reason

    def test_force_skim_blocked_by_cooldown(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,
                force_skim_requested=True,
                water_temp_c=26.0,
                v3_last_ended_at=pool_inputs.now - timedelta(minutes=10),
            ),
            pool_thr,
        )
        # Falls through to normal logic; v3 candidate also blocked → v2.
        assert d.target_speed == 2
        assert "force-skim" not in d.reason

    def test_force_skim_blocked_by_no_surplus(self, pool_inputs, pool_thr):
        d = decide(
            replace(pool_inputs, grid_w=200.0, force_skim_requested=True),
            pool_thr,
        )
        # Falls through; default → v1.
        assert d.target_speed == 1


# ---------------------------------------------------------------------------
# Edge cases on missing temps
# ---------------------------------------------------------------------------


class TestWinterMode:
    def test_tempo_red_forces_off(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                mode=MODE_WINTER,
                tempo_color=TEMPO_RED,
                grid_w=-3000.0,   # huge surplus, irrelevant
            ),
            pool_thr,
        )
        assert d.target_speed == 0
        assert "Tempo Red" in d.reason

    def test_night_forces_off(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                mode=MODE_WINTER,
                daylight=False,
                grid_w=-3000.0,
            ),
            pool_thr,
        )
        assert d.target_speed == 0
        assert "night" in d.reason

    def test_v1_when_solar_covers(self, pool_inputs, pool_thr):
        # From off (0W). Bump to v1 adds 160W. grid + 160 < -200 → grid < -360.
        d = decide(
            replace(
                pool_inputs,
                mode=MODE_WINTER,
                pump_speed=0,
                grid_w=-500.0,    # comfortably enough surplus
            ),
            pool_thr,
        )
        assert d.target_speed == 1
        assert "solar" in d.reason

    def test_off_when_no_solar(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                mode=MODE_WINTER,
                pump_speed=0,
                grid_w=200.0,     # importing
            ),
            pool_thr,
        )
        assert d.target_speed == 0
        assert "insufficient" in d.reason

    def test_winter_caps_at_v1_even_with_huge_surplus(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                mode=MODE_WINTER,
                pump_speed=1,
                grid_w=-3000.0,   # plenty for v3, but winter caps at v1
                water_temp_c=26.0,
                air_temp_c=30.0,
            ),
            pool_thr,
        )
        assert d.target_speed == 1

    def test_winter_drops_v3_to_off_via_tempo(self, pool_inputs, pool_thr):
        """If we land in winter mode while at v3, we should leave the session cleanly."""
        d = decide(
            replace(
                pool_inputs,
                mode=MODE_WINTER,
                pump_speed=3,
                tempo_color=TEMPO_RED,
                v3_started_at=pool_inputs.now,
            ),
            pool_thr,
        )
        assert d.target_speed == 0
        assert d.leave_v3 is True

    def test_winter_ignores_force_skim(self, pool_inputs, pool_thr):
        """Force-skim is a summer-only concept; winter ignores it."""
        d = decide(
            replace(
                pool_inputs,
                mode=MODE_WINTER,
                grid_w=-3000.0,
                force_skim_requested=True,
            ),
            pool_thr,
        )
        assert d.target_speed == 1   # at most v1 in winter


class TestSpeedClamping:
    """A bogus `pump_speed` reading should not crash _can_bump_to."""

    def test_clamps_overflow(self, pool_inputs, pool_thr):
        # pump_speed=99 → clamped to 3 → from-v3 bump-to-v3 (delta=0)
        # grid -1500 + 0 < -200 → allowed (we're "at v3" already).
        d = decide(replace(pool_inputs, pump_speed=99, grid_w=-1500.0), pool_thr)
        # No crash is the assertion. Output should be deterministic too:
        assert d.target_speed in (1, 2, 3)

    def test_clamps_negative(self, pool_inputs, pool_thr):
        d = decide(replace(pool_inputs, pump_speed=-1, grid_w=0.0), pool_thr)
        assert d.target_speed in (0, 1)


class TestMissingTemps:
    def test_missing_both_temps_blocks_v3(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,
                water_temp_c=None,
                air_temp_c=None,
            ),
            pool_thr,
        )
        # No "warm" signal possible, so neither v3 nor v2 candidate fires.
        # Falls all the way back to v1.
        assert d.target_speed == 1

    def test_a_warm_air_reading_is_no_longer_enough(self, pool_inputs, pool_thr):
        """ROUTEUR: the air half of the gate is gone.

        That sensor sits in full sun — 33.2 °C against 22.4 °C on the shaded
        terrace at the same moment — so it was true nearly all day in season
        and the water condition never mattered. On 21 April the pump reached
        v3 with the water at 22.7 °C, which this gate exists to prevent. A
        missing water probe now blocks the upper speeds instead of letting
        the air vouch for them.
        """
        d = decide(
            replace(
                pool_inputs,
                grid_w=-1500.0,
                water_temp_c=None,
                air_temp_c=30.0,
            ),
            pool_thr,
        )
        assert d.target_speed == 1


# ---------------------------------------------------------------------------
# Hysteresis — the deadband that stops the 1↔2 cycling
# ---------------------------------------------------------------------------


class TestHysteresis:
    """Stepping up stays strict; holding a speed is lenient by `hysteresis_w`."""

    def test_step_up_still_requires_full_margin(self, pool_inputs, pool_thr):
        """At v1, grid -500: bumping to v2 lands at -160, short of the -200
        margin. The deadband must not loosen the *entry* test."""
        d = decide(
            replace(pool_inputs, grid_w=-500.0, pump_speed=1, water_temp_c=26.0),
            pool_thr,
        )
        assert d.target_speed == 1

    def test_holds_v2_inside_the_deadband(self, pool_inputs, pool_thr):
        """At v2 with the grid balanced at 0 W. Old behaviour dropped to v1
        (needed grid < -200); with a 250 W band we hold."""
        d = decide(
            replace(pool_inputs, grid_w=0.0, pump_speed=2, water_temp_c=26.0),
            pool_thr,
        )
        assert d.target_speed == 2

    def test_drops_v2_once_clear_of_the_deadband(self, pool_inputs, pool_thr):
        """Importing 100 W at v2 is outside the band (-200 + 250 = +50)."""
        d = decide(
            replace(pool_inputs, grid_w=100.0, pump_speed=2, water_temp_c=26.0),
            pool_thr,
        )
        assert d.target_speed == 1

    def test_just_entered_v2_survives_its_own_load(self, pool_inputs, pool_thr):
        """Regression for the observed 5-minute 1↔2 cycling.

        Enter v2 right at the boundary, then re-decide against the grid as it
        actually reads once the pump's own 340 W is being drawn, plus a little
        adverse drift. Under the old symmetric test that reading fell on the
        wrong side of -200 and the pump was sent straight back to v1.
        """
        entering = replace(
            pool_inputs, grid_w=-541.0, pump_speed=1, water_temp_c=26.0
        )
        assert decide(entering, pool_thr).target_speed == 2

        settled = replace(entering, pump_speed=2, grid_w=-541.0 + 340.0 + 5.0)
        assert settled.grid_w > -pool_thr.safety_margin_w  # old test would have dropped
        assert decide(settled, pool_thr).target_speed == 2

    def test_winter_keeps_v1_inside_the_deadband(self, pool_inputs, pool_thr):
        """Winter mode ran the same symmetric test, so it flapped on/off too."""
        d = decide(
            replace(pool_inputs, mode=MODE_WINTER, pump_speed=1, grid_w=0.0), pool_thr
        )
        assert d.target_speed == 1

    def test_winter_stops_once_clear_of_the_deadband(self, pool_inputs, pool_thr):
        d = decide(
            replace(pool_inputs, mode=MODE_WINTER, pump_speed=1, grid_w=300.0), pool_thr
        )
        assert d.target_speed == 0


# ---------------------------------------------------------------------------
# v3 minimum session floor
# ---------------------------------------------------------------------------


class TestV3MinSession:
    def test_rides_out_a_short_dip(self, pool_inputs, pool_thr):
        """A 60 s skim does no work and still burns the whole cooldown."""
        d = decide(
            replace(
                pool_inputs,
                grid_w=100.0,          # surplus gone
                pump_speed=3,
                v3_started_at=pool_inputs.now - timedelta(minutes=1),
            ),
            pool_thr,
        )
        assert d.target_speed == 3
        assert d.leave_v3 is False
        assert "floor" in d.reason

    def test_aborts_after_the_floor(self, pool_inputs, pool_thr):
        d = decide(
            replace(
                pool_inputs,
                grid_w=100.0,
                pump_speed=3,
                v3_started_at=pool_inputs.now - timedelta(minutes=4),
            ),
            pool_thr,
        )
        assert d.target_speed != 3
        assert d.leave_v3 is True
        assert "surplus lost" in d.reason

    def test_big_import_escapes_the_floor(self, pool_inputs, pool_thr):
        """A large non-solar load (oven, EV) bails out immediately."""
        d = decide(
            replace(
                pool_inputs,
                grid_w=1500.0,         # importing more than the pump's own v3 draw
                pump_speed=3,
                v3_started_at=pool_inputs.now - timedelta(seconds=30),
            ),
            pool_thr,
        )
        assert d.target_speed != 3
        assert d.leave_v3 is True

    def test_cap_still_wins_over_the_floor(self, pool_inputs, pool_thr):
        """v3_max_minutes is a hard cap; the floor must not extend it."""
        d = decide(
            replace(
                pool_inputs,
                grid_w=100.0,
                pump_speed=3,
                v3_started_at=pool_inputs.now - timedelta(minutes=16),
            ),
            pool_thr,
        )
        assert d.target_speed != 3
        assert "cap" in d.reason


# ---------------------------------------------------------------------------
# ROUTEUR: the season, read off the water
# ---------------------------------------------------------------------------


class TestAutomaticSeason:
    def test_cold_water_puts_auto_mode_into_winter(self, pool_inputs, pool_thr):
        """April: 19 °C water. Winter caps the pump at v1 and stops at night."""
        d = decide(
            replace(pool_inputs, mode=MODE_AUTO, water_temp_c=19.0,
                    grid_w=-1500.0, air_temp_c=35.0),
            pool_thr,
        )
        assert d.season == "hiver"
        assert d.target_speed <= 1

    def test_warm_water_stays_in_summer(self, pool_inputs, pool_thr):
        d = decide(
            replace(pool_inputs, mode=MODE_AUTO, water_temp_c=25.9, grid_w=-1500.0),
            pool_thr,
        )
        assert d.season == "ete"
        assert d.target_speed == 3

    def test_the_deadband_keeps_the_season_still(self, pool_inputs, pool_thr):
        """Between 20 and 22 °C, whatever was decided last tick stands."""
        between = replace(pool_inputs, water_temp_c=21.0)
        assert decide(replace(between, season="ete"), pool_thr).season == "ete"
        assert decide(replace(between, season="hiver"), pool_thr).season == "hiver"

    def test_a_manual_winter_still_wins_over_warm_water(self, pool_inputs, pool_thr):
        d = decide(
            replace(pool_inputs, mode=MODE_WINTER, water_temp_c=28.0, grid_w=-1500.0),
            pool_thr,
        )
        assert d.target_speed <= 1

    def test_an_unreadable_probe_keeps_the_last_season(self, pool_inputs, pool_thr):
        d = decide(replace(pool_inputs, water_temp_c=None, season="hiver"), pool_thr)
        assert d.season == "hiver"


# ---------------------------------------------------------------------------
# ROUTEUR: Tempo Rouge — not a watt of grid, peak hours
# ---------------------------------------------------------------------------


class TestRougePeak:
    def test_the_baseline_speed_stops_being_free(self, pool_inputs, pool_thr):
        """v1 costs 160 W; on a Rouge peak hour that is 0.706 €/kWh."""
        d = decide(
            replace(pool_inputs, tempo_color=TEMPO_RED, is_hc=False, grid_w=200.0),
            pool_thr,
        )
        assert d.target_speed == 0
        assert "Rouge HP" in d.reason

    def test_but_runs_when_the_sun_pays_for_it(self, pool_inputs, pool_thr):
        d = decide(
            replace(pool_inputs, tempo_color=TEMPO_RED, is_hc=False, grid_w=-600.0),
            pool_thr,
        )
        assert d.target_speed >= 1

    def test_off_peak_on_a_red_day_keeps_the_baseline(self, pool_inputs, pool_thr):
        """Rouge off-peak is 0.1575 €/kWh — the night baseline is fine."""
        d = decide(
            replace(pool_inputs, tempo_color=TEMPO_RED, is_hc=True,
                    daylight=False, grid_w=200.0),
            pool_thr,
        )
        assert d.target_speed == 1

    def test_a_blue_day_is_untouched(self, pool_inputs, pool_thr):
        d = decide(replace(pool_inputs, tempo_color="Bleu", grid_w=200.0), pool_thr)
        assert d.target_speed == 1
