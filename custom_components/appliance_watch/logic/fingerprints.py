"""What each appliance has been observed to do, remembered across restarts.

A cycle's expected length is the one number the Live Activity cannot fake: it
drives the progress bar and the on-device countdown. Starting from a nominal
value is fine, but this washing machine runs 60 minutes on one programme and
120 on another, so the nominal is blended with what has actually been measured
— gradually, so a single odd cycle never takes over.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from statistics import median

# Cycles needed before the learned duration is trusted on its own. Below that
# it is mixed with the nominal in proportion, the way dhwp_smart blends its
# usage pattern.
FULL_CONFIDENCE_SAMPLES = 8
# Keep a rolling window rather than a lifetime average: a new programme habit
# should show through within a couple of weeks.
MAX_SAMPLES = 20
# A cycle this far from the known median is kept out of the model — it is
# almost always two appliances counted as one, or a truncated observation.
OUTLIER_RATIO = 2.5
# Nothing shorter than this is a wash: it is a manual test, or a machine
# switched on and straight back off. Learning from it would drag the expected
# duration — and the countdown — down with it.
MIN_LEARNABLE_MINUTES = 5.0


@dataclass(frozen=True)
class Fingerprint:
    """The rolling record of one appliance's cycles."""

    durations: tuple[float, ...] = ()  # minutes, oldest first
    energies: tuple[float, ...] = ()  # Wh
    rejected: int = 0

    @property
    def samples(self) -> int:
        return len(self.durations)

    @property
    def confidence(self) -> float:
        """0 with no history, 1 once enough cycles have been seen."""
        return min(1.0, self.samples / FULL_CONFIDENCE_SAMPLES)

    @property
    def median_duration(self) -> float | None:
        return median(self.durations) if self.durations else None

    @property
    def median_energy(self) -> float | None:
        return median(self.energies) if self.energies else None


def record(fingerprint: Fingerprint, duration_minutes: float,
           energy_wh: float) -> Fingerprint:
    """Add one finished cycle, unless it is wildly unlike the others."""
    if duration_minutes < MIN_LEARNABLE_MINUTES:
        return fingerprint
    known = fingerprint.median_duration
    if known and not (known / OUTLIER_RATIO <= duration_minutes <= known * OUTLIER_RATIO):
        return replace(fingerprint, rejected=fingerprint.rejected + 1)
    return replace(
        fingerprint,
        durations=(fingerprint.durations + (round(duration_minutes, 1),))[-MAX_SAMPLES:],
        energies=(fingerprint.energies + (round(energy_wh, 1),))[-MAX_SAMPLES:],
    )


def expected_duration(fingerprint: Fingerprint, nominal: timedelta) -> timedelta:
    """Blend what was measured with the nominal, by how much history there is."""
    learned = fingerprint.median_duration
    if learned is None:
        return nominal
    weight = fingerprint.confidence
    minutes = weight * learned + (1 - weight) * nominal.total_seconds() / 60
    return timedelta(minutes=round(minutes, 1))


def as_dict(fingerprint: Fingerprint) -> dict:
    """JSON-ready. Tuples come back from storage as lists, hence :func:`from_dict`."""
    return {
        "durations": list(fingerprint.durations),
        "energies": list(fingerprint.energies),
        "rejected": fingerprint.rejected,
    }


def from_dict(data: dict | None) -> Fingerprint:
    """Rebuild field by field, falling back rather than raising on junk."""
    if not data:
        return Fingerprint()
    return Fingerprint(
        durations=tuple(_floats(data.get("durations"))),
        energies=tuple(_floats(data.get("energies"))),
        rejected=_count(data.get("rejected")),
    )


def _count(value: object) -> int:
    """A corrupt counter must not take the whole integration down at startup."""
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _floats(values: object) -> list[float]:
    if not isinstance(values, (list, tuple)):
        return []
    out = []
    for value in values:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out
