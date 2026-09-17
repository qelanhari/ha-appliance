"""What gets remembered about each appliance, and how carefully."""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.fingerprints import (  # noqa: E402
    FULL_CONFIDENCE_SAMPLES,
    MAX_SAMPLES,
    Fingerprint,
    as_dict,
    expected_duration,
    from_dict,
    record,
)

NOMINAL = timedelta(minutes=65)


def learned(*durations: float) -> Fingerprint:
    fingerprint = Fingerprint()
    for duration in durations:
        fingerprint = record(fingerprint, duration, 900.0)
    return fingerprint


def test_with_no_history_the_nominal_is_used_as_is():
    assert expected_duration(Fingerprint(), NOMINAL) == NOMINAL


def test_the_learned_duration_takes_over_gradually():
    """One odd cycle must not move the countdown far; eight should own it."""
    one = expected_duration(learned(120), NOMINAL)
    many = expected_duration(learned(*[120] * FULL_CONFIDENCE_SAMPLES), NOMINAL)
    assert NOMINAL < one < many
    assert many == timedelta(minutes=120)


def test_a_wildly_different_cycle_is_kept_out_of_the_model():
    """Two appliances counted as one, or a truncated observation."""
    fingerprint = learned(60, 62, 58, 61)
    with_outlier = record(fingerprint, 400.0, 3000.0)
    assert with_outlier.durations == fingerprint.durations
    assert with_outlier.rejected == 1


def test_the_first_cycle_is_always_accepted():
    assert learned(120).samples == 1


def test_history_stays_a_rolling_window():
    fingerprint = learned(*[60] * (MAX_SAMPLES + 5))
    assert fingerprint.samples == MAX_SAMPLES


def test_a_zero_length_cycle_teaches_nothing():
    assert record(Fingerprint(), 0, 0).samples == 0


def test_storage_round_trip_restores_the_tuples():
    """JSON gives lists back; the medians would still work, equality would not."""
    fingerprint = learned(60, 62, 58)
    restored = from_dict(as_dict(fingerprint))
    assert restored == fingerprint


def test_corrupt_storage_falls_back_instead_of_raising():
    restored = from_dict({"durations": ["soixante", 61], "energies": None,
                          "rejected": "x"})
    assert restored.durations == (61.0,)
    assert restored.rejected == 0
    assert from_dict(None) == Fingerprint()


def test_a_thirty_second_run_teaches_nothing():
    """A manual test is not a wash; learning from it would skew the countdown."""
    assert record(Fingerprint(), 0.5, 20.0).samples == 0
    assert record(learned(60, 62), 0.5, 20.0).durations == (60.0, 62.0)
