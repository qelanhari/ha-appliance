"""The part of the house's consumption that no meter accounts for.

The dishwasher is invisible: nothing measures it. What can be computed is the
total draw minus everything that *is* measured — pool pumps, air conditioning,
garage, water heater, car charger — and the dishwasher's heating element stands
out in what remains, next to the oven's.

The one rule that matters here: a reading that is missing is **not** zero. A
sensor that drops to ``unavailable`` while its appliance keeps drawing 2 kW
would inflate the residual by exactly that much, and the dishwasher band would
start matching the air conditioning. So a missing measured input suspends the
computation instead of guessing.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    """One input to the subtraction."""

    entity_id: str
    watts: float | None  # None when unavailable/unknown — never coerced to 0
    required: bool = True


def residual_watts(total: Reading, measured: list[Reading]) -> float | None:
    """Total minus every measured load, or None when an input is missing.

    Optional inputs (``required=False``) are treated as 0 when absent: that is
    right for a car charger that reports nothing while unplugged, and wrong for
    a pump that simply lost its connection — hence the flag per input.
    """
    if total.watts is None:
        return None
    accounted = 0.0
    for reading in measured:
        if reading.watts is None:
            if reading.required:
                return None
            continue
        accounted += reading.watts
    return total.watts - accounted


def to_watts(value: float | None, unit: str | None) -> float | None:
    """Normalise a power reading to watts, kW being common on car chargers."""
    if value is None:
        return None
    if unit and unit.strip().lower() in {"kw", "kilowatt"}:
        return value * 1000.0
    return value
