"""Time-weighted statistics over an unevenly sampled trace.

These meters report on change: the dryer emitted 80 readings for 70 minutes,
the washer 1 300 for 60. Every statistic here therefore integrates over time,
and these tests pin that down — an arithmetic mean would quietly weigh a
one-second blip the same as a ten-minute plateau.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.samples import (  # noqa: E402
    Trace,
    seconds_above,
    spread,
    time_weighted_mean,
    time_weighted_quantile,
)

T0 = datetime(2026, 9, 17, 13, 0)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# Ten minutes at 2 000 W, then one minute at 100 W: nine readings out of ten
# sit in the short tail if you count samples instead of seconds.
TRACE = [(at(0), 2000.0), (at(10), 100.0), (at(10.2), 120.0), (at(10.4), 90.0),
         (at(10.6), 110.0), (at(10.8), 95.0), (at(11), 100.0)]


def test_mean_weighs_by_duration_not_by_sample_count():
    mean = time_weighted_mean(TRACE, at(0), at(11))
    arithmetic = sum(watts for _, watts in TRACE) / len(TRACE)
    assert 1750 < mean < 1850      # ten of eleven minutes were at 2 000 W
    assert arithmetic < 500        # what counting samples would have said


def test_mean_returns_none_on_an_empty_window():
    assert time_weighted_mean(TRACE, at(20), at(20)) is None
    assert time_weighted_mean([], at(0), at(5)) is None


def test_a_reading_holds_until_the_next_one():
    """Silence means unchanged, not zero — that is how these meters report."""
    assert time_weighted_mean(TRACE, at(5), at(9)) == 2000.0


def test_the_window_uses_the_reading_in_force_when_it_opened():
    # Nothing was reported between 13:00 and 13:03, yet the trace was at 2 000 W.
    assert time_weighted_mean(TRACE, at(3), at(4)) == 2000.0


def test_spread_is_the_peak_to_peak_amplitude():
    assert spread(TRACE, at(0), at(11)) == 2000.0 - 90.0
    assert spread(TRACE, at(0), at(9)) == 0.0  # a flat plateau


def test_quantile_ignores_a_brief_excursion():
    """A 12-second spike must not move the baseline a plateau is measured from."""
    trace = [(at(0), 250.0), (at(5), 3000.0), (at(5.2), 250.0), (at(30), 250.0)]
    assert time_weighted_quantile(trace, at(0), at(30), 0.2) == 250.0


def test_quantile_at_zero_is_the_lowest_level_seen():
    assert time_weighted_quantile(TRACE, at(0), at(11), 0.0) == 90.0


def test_seconds_above_counts_time_not_readings():
    assert seconds_above(TRACE, at(0), at(11), 1000.0) == 600.0


def test_trace_keeps_the_reading_that_was_in_force_when_the_window_opened():
    """Dropping it would leave the first minutes of every window unexplained."""
    trace = Trace(keep=timedelta(minutes=10))
    trace.add(at(0), 2000.0)
    trace.add(at(20), 100.0)
    assert len(trace.samples) == 2
    assert trace.mean(at(15), at(20)) == 2000.0


def test_trace_drops_what_has_aged_out():
    trace = Trace(keep=timedelta(minutes=10))
    for minute in range(0, 40):
        trace.add(at(minute), float(minute))
    assert trace.samples[0][0] >= at(29)
    assert trace.last == (at(39), 39.0)
