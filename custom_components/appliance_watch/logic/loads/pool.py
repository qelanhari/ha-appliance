"""The pool pump's brain — ported from the `pool_pump` integration.

Kept as close to the original as the corrections allow; every deviation is
marked ``ROUTEUR:``. Pure: no `homeassistant` import, so the suite exercises
it standalone.

Two corrections, both measured:

* the season is now derived from the **water** temperature instead of waiting
  for someone to flip a select by hand;
* the warmth gate on the upper speeds no longer accepts an air reading. The
  outdoor sensor sits in full sun — 33.2 °C against 22.4 °C on the shaded
  terrace at the same moment — so the ``or`` made the water condition
  decorative. On 21 April the pump reached v3 with the water at 22.7 °C,
  which the gate was there to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

# Speed → expected mains draw, in Watts. Calibrated by the user for a
# Hayward VSTD on an Antea VS box.
PUMP_W: dict[int, int] = {0: 0, 1: 160, 2: 500, 3: 1100}

MODE_AUTO = "auto"
MODE_WINTER = "winter"
MODE_OFF = "off"
MODE_V1 = "v1"
MODE_V2 = "v2"
MODE_V3 = "v3"
MODES: tuple[str, ...] = (
    MODE_AUTO,
    MODE_WINTER,
    MODE_OFF,
    MODE_V1,
    MODE_V2,
    MODE_V3,
)
MANUAL_TO_SPEED: dict[str, int] = {MODE_OFF: 0, MODE_V1: 1, MODE_V2: 2, MODE_V3: 3}

# Tempo (RTE) color values. The integration matches `Rouge` exactly to force
# the pump off for the whole Red day; other values are pass-through.
TEMPO_RED = "Rouge"


@dataclass(frozen=True)
class Inputs:
    """Snapshot of everything `decide()` needs.

    The coordinator builds this from `hass.states` on every tick.
    """

    now: datetime
    daylight: bool
    grid_w: float                       # negative = exporting solar
    pump_speed: int                     # 0..3 — what the pump is set to right now
    water_temp_c: float | None
    air_temp_c: float | None
    mode: str                           # "auto" | "winter" | "off" | "v1" | "v2" | "v3"
    v3_started_at: datetime | None      # when current/last v3 session started
    v3_last_ended_at: datetime | None   # when previous v3 session ended
    force_skim_requested: bool = False  # one-shot flag from the button entity
    tempo_color: str | None = None      # "Rouge" / "Blanc" / "Bleu" / None if unread
    # ROUTEUR: off-peak window, so a Rouge *peak* hour can be told from a
    # Rouge night. Daylight is not a substitute: the tariff window is
    # 06:00-22:00 whatever the sun does.
    is_hc: bool = False
    # ROUTEUR: the season decided last tick, persisted by the coordinator.
    # None on a cold start — the thresholds below then decide outright.
    season: str | None = None


@dataclass(frozen=True)
class Thresholds:
    """All tunable knobs. Defaults match the user's CS100 / Hayward VSTD setup."""

    safety_margin_w: float = 200.0
    water_warm_c: float = 24.0
    air_warm_c: float = 28.0
    v3_max_minutes: int = 15
    v3_cooldown_minutes: int = 30
    # Width of the deadband between "worth stepping up" and "give up and step
    # down". Without it the enter and hold tests collapse onto the same
    # condition and the pump cycles at the dwell period all day.
    hysteresis_w: float = 250.0
    # A v3 session shorter than this is worthless (it costs a full cooldown),
    # so ride out short surplus dips instead of aborting immediately.
    v3_min_minutes: int = 3
    # ROUTEUR: automatic season, on water temperature, with a two-degree
    # deadband. Measured over 150 days the water ran 19.4-29.9 °C and sat
    # below 24 °C only between 21 April and 20 May, so the switch lands in
    # the shoulder season where it belongs. Water is slow enough that no
    # hysteresis timer is needed beyond this gap.
    summer_water_c: float = 22.0
    winter_water_c: float = 20.0


SEASON_SUMMER = "ete"
SEASON_WINTER = "hiver"


@dataclass(frozen=True)
class Decision:
    target_speed: int
    reason: str
    enter_v3: bool   # signal: stamp v3_started_at
    leave_v3: bool   # signal: stamp v3_last_ended_at
    season: str = SEASON_SUMMER  # ROUTEUR: to persist, for the hysteresis


def _safe_speed(s: int) -> int:
    """Clamp a possibly-bogus speed reading to the valid 0..3 range."""
    if s < 0:
        return 0
    if s > 3:
        return 3
    return s


def _grid_at(speed: int, i: Inputs) -> float:
    """Projected grid power if the pump ran at `speed`, given the current reading.

        projected = grid_now + (target_W - current_W)

    Negative means we would still be exporting.
    """
    delta_w = PUMP_W[_safe_speed(speed)] - PUMP_W[_safe_speed(i.pump_speed)]
    return i.grid_w + delta_w


def _affordable(speed: int, i: Inputs, thr: Thresholds) -> bool:
    """True if the pump can run at `speed` on solar.

    Deliberately asymmetric. Stepping *up* demands a real surplus (still
    exporting `safety_margin_w` once the new load is picked up); staying put
    or stepping *down* only gives up once we are genuinely importing. The gap
    between the two is `hysteresis_w`.

    Without that gap the two tests are the same condition — entering v2 asks
    `grid + 340 < -margin`, holding v2 asks `grid < -margin`, which in steady
    state is identical — so at the boundary the decision alternates every tick
    and the dwell limiter just paces the resulting 1↔2 cycling.
    """
    target = _safe_speed(speed)
    current = _safe_speed(i.pump_speed)
    limit = -thr.safety_margin_w
    if target > current:
        return _grid_at(target, i) < limit
    return _grid_at(target, i) < limit + thr.hysteresis_w


def _warm_enough(i: Inputs, thr: Thresholds) -> bool:
    """ROUTEUR: water only.

    The air reading comes from a sensor in full sun and was true nearly all
    day in season, which made the water condition decorative. An unavailable
    water probe now blocks the upper speeds rather than letting the air vouch
    for them — the safe direction.
    """
    return i.water_temp_c is not None and i.water_temp_c > thr.water_warm_c


def season_for(i: Inputs, thr: Thresholds) -> str:
    """ROUTEUR: the season, from the water, with a deadband.

    Between the two thresholds the previous season stands; with none known
    yet, the midpoint decides.
    """
    water = i.water_temp_c
    if water is None:
        return i.season or SEASON_SUMMER
    if water >= thr.summer_water_c:
        return SEASON_SUMMER
    if water <= thr.winter_water_c:
        return SEASON_WINTER
    if i.season in (SEASON_SUMMER, SEASON_WINTER):
        return i.season
    return SEASON_SUMMER if water >= (thr.summer_water_c + thr.winter_water_c) / 2 \
        else SEASON_WINTER


def _rouge_peak(i: Inputs) -> bool:
    """ROUTEUR: a Rouge day, outside the off-peak window.

    No controlled load may draw from the grid then — the baseline speed stops
    being free and has to be paid for by the sun like any other step.
    """
    return i.tempo_color == TEMPO_RED and not i.is_hc


def _v3_session_age_s(i: Inputs) -> float | None:
    """Seconds since current v3 session started, or None if no active session."""
    if i.pump_speed != 3 or i.v3_started_at is None:
        return None
    return (i.now - i.v3_started_at).total_seconds()


def _v3_cooldown_remaining_s(i: Inputs, thr: Thresholds) -> float:
    """Seconds remaining before another v3 session is allowed. 0 if ready."""
    if i.v3_last_ended_at is None:
        return 0.0
    elapsed = (i.now - i.v3_last_ended_at).total_seconds()
    cooldown = thr.v3_cooldown_minutes * 60
    return max(0.0, cooldown - elapsed)


def decide(i: Inputs, thr: Thresholds) -> Decision:
    """Map (Inputs, Thresholds) to a Decision. Pure, deterministic."""
    season = season_for(i, thr)
    return replace(_decide_impl(i, thr, season), season=season)


def _decide_impl(i: Inputs, thr: Thresholds, season: str) -> Decision:
    was_v3 = i.pump_speed == 3
    # ROUTEUR: on a Rouge peak hour the baseline speed stops being free —
    # nothing runs unless the sun pays for it.
    floor = 0 if _rouge_peak(i) else 1

    # 1a. Winter: solar-only operation, capped at v1, force-off on Tempo Red.
    # ROUTEUR: reached by the water temperature too, not only by hand.
    if i.mode == MODE_WINTER or (i.mode == MODE_AUTO and season == SEASON_WINTER):
        if i.tempo_color == TEMPO_RED:
            return Decision(
                target_speed=0,
                reason="winter: Tempo Red day → off",
                enter_v3=False,
                leave_v3=was_v3,
            )
        if not i.daylight:
            return Decision(
                target_speed=0,
                reason="winter: night → off",
                enter_v3=False,
                leave_v3=was_v3,
            )
        # Solar covers the bump from current speed up to v1?
        if _affordable(1, i, thr):
            how = "winter" if i.mode == MODE_WINTER else "hiver (eau froide)"
            return Decision(
                target_speed=1,
                reason=f"{how}: solar surplus covers v1 → on",
                enter_v3=False,
                leave_v3=was_v3,
            )
        return Decision(
            target_speed=0,
            reason="winter: insufficient solar → off",
            enter_v3=False,
            leave_v3=was_v3,
        )

    # 1b. Manual mode is sovereign (off / v1 / v2 / v3).
    if i.mode != MODE_AUTO:
        target = MANUAL_TO_SPEED.get(i.mode, 1)
        leaving_v3 = was_v3 and target != 3
        entering_v3 = (not was_v3) and target == 3
        return Decision(
            target_speed=target,
            reason=f"manual={i.mode}",
            enter_v3=entering_v3,
            leave_v3=leaving_v3,
        )

    # 2. Force-skim button — start a v3 session now (still respects cooldown).
    if i.force_skim_requested:
        cooldown = _v3_cooldown_remaining_s(i, thr)
        if cooldown <= 0 and _affordable(3, i, thr):
            return Decision(
                target_speed=3,
                reason="force-skim button → v3",
                enter_v3=not was_v3,
                leave_v3=False,
            )
        # If we cannot honour the request, fall through to normal logic.

    # 3. No daylight → the baseline.
    if not i.daylight:
        return Decision(
            target_speed=floor,
            reason="night → v1" if floor else "Rouge HP · nuit sans réseau",
            enter_v3=False,
            leave_v3=was_v3,
        )

    # 4. Currently in a v3 session.
    age = _v3_session_age_s(i)
    if age is not None:
        max_s = thr.v3_max_minutes * 60
        if age >= max_s:
            # 15-min cap reached — drop to highest sustainable speed.
            best = _best_sustainable_speed(i, thr)
            return Decision(
                target_speed=best,
                reason=f"v3 cap reached ({thr.v3_max_minutes}min) → v{best}",
                enter_v3=False,
                leave_v3=True,
            )
        if not _affordable(3, i, thr):
            # Mid-session abort: surplus dropped (e.g. cloud).
            #
            # But hold through the first `v3_min_minutes`: a 60 s skim does no
            # useful work and still burns the whole v3 cooldown, so riding out
            # a short cloud is strictly better. The escape hatch is a big
            # non-solar load arriving (importing more than the pump's own v3
            # draw) — then we bail immediately regardless of the floor.
            min_s = thr.v3_min_minutes * 60
            big_import = _grid_at(3, i) > PUMP_W[3]
            if age < min_s and not big_import:
                return Decision(
                    target_speed=3,
                    reason=(
                        f"v3 holding through dip "
                        f"({age/60:.1f}/{thr.v3_min_minutes}min floor)"
                    ),
                    enter_v3=False,
                    leave_v3=False,
                )
            best = _best_sustainable_speed(i, thr)
            return Decision(
                target_speed=best,
                reason=f"v3 surplus lost mid-session → v{best}",
                enter_v3=False,
                leave_v3=True,
            )
        return Decision(
            target_speed=3,
            reason=f"v3 holding ({age/60:.1f}/{thr.v3_max_minutes}min)",
            enter_v3=False,
            leave_v3=False,
        )

    # 5. v3 candidate — daylight + cooldown elapsed + warm + surplus covers.
    cooldown = _v3_cooldown_remaining_s(i, thr)
    if cooldown <= 0 and _warm_enough(i, thr) and _affordable(3, i, thr):
        return Decision(
            target_speed=3,
            reason="solar surplus covers v3 + warm → v3 skim session",
            enter_v3=True,
            leave_v3=False,
        )

    # 6. v2 candidate — surplus covers v2 bump AND it's warm enough to justify it.
    #    v2 is sustained-flow, so we only run it when extra filtration is useful.
    if _affordable(2, i, thr) and _warm_enough(i, thr):
        return Decision(
            target_speed=2,
            reason="solar surplus covers v2 + warm → v2",
            enter_v3=False,
            leave_v3=was_v3,
        )

    # 7. Default — the baseline, unless a Rouge peak hour makes us pay for it.
    if floor == 0:
        if _affordable(1, i, thr):
            return Decision(
                target_speed=1,
                reason="Rouge HP · le surplus couvre v1",
                enter_v3=False,
                leave_v3=was_v3,
            )
        return Decision(
            target_speed=0,
            reason="Rouge HP · aucune consommation réseau",
            enter_v3=False,
            leave_v3=was_v3,
        )
    return Decision(
        target_speed=1,
        reason="default → v1 baseline",
        enter_v3=False,
        leave_v3=was_v3,
    )


def _best_sustainable_speed(i: Inputs, thr: Thresholds) -> int:
    """When dropping out of v3, pick the highest still-affordable speed.

    v2 also requires warmth — same gate as the candidate path — so we don't
    leave the pump at v2 with no filtration justification.
    """
    if _affordable(2, i, thr) and _warm_enough(i, thr):
        return 2
    # ROUTEUR: falling back onto the baseline is free on any other day, and
    # paid for on a Rouge peak hour.
    if _rouge_peak(i) and not _affordable(1, i, thr):
        return 0
    return 1
