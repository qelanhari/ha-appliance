"""Time-weighted helpers over a power trace.

Power meters report *on change*: a dryer holding 1 934 W for eight minutes emits
one sample, while the same eight minutes of a washing machine's drum emit
sixty. Averaging the samples arithmetically would weigh those sixty readings
sixty times more than the one — so every window statistic here integrates over
*time*, treating each sample as holding until the next one.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from datetime import datetime, timedelta
from typing import Iterable

Sample = tuple[datetime, float]


class Trace:
    """A bounded, chronological window of ``(timestamp, watts)`` readings."""

    def __init__(self, keep: timedelta) -> None:
        self._keep = keep
        self._samples: deque[Sample] = deque()

    def add(self, at: datetime, watts: float) -> None:
        """Append a reading and drop what has aged out of the window.

        One sample *before* the cut-off is kept: it carries the value that was
        in force when the window opened, which every integral below needs.
        """
        self._samples.append((at, watts))
        cutoff = at - self._keep
        while len(self._samples) > 1 and self._samples[1][0] <= cutoff:
            self._samples.popleft()

    @property
    def samples(self) -> list[Sample]:
        return list(self._samples)

    @property
    def last(self) -> Sample | None:
        return self._samples[-1] if self._samples else None

    def clear(self) -> None:
        self._samples.clear()

    def mean(self, start: datetime, end: datetime) -> float | None:
        return time_weighted_mean(self._samples, start, end)

    def spread(self, start: datetime, end: datetime) -> float | None:
        return spread(self._samples, start, end)

    def quantile(self, start: datetime, end: datetime, q: float) -> float | None:
        return time_weighted_quantile(self._samples, start, end, q)


def _segments(samples: Iterable[Sample], start: datetime,
              end: datetime) -> list[tuple[float, float]]:
    """Clip the step function to ``[start, end]`` as ``(seconds, watts)`` pieces."""
    points = list(samples)
    if end <= start or not points:
        return []

    times = [stamp for stamp, _ in points]
    first = max(0, bisect_right(times, start) - 1)
    out: list[tuple[float, float]] = []
    for index in range(first, len(points)):
        stamp, watts = points[index]
        piece_start = max(stamp, start)
        piece_end = points[index + 1][0] if index + 1 < len(points) else end
        piece_end = min(piece_end, end)
        if piece_end > piece_start:
            out.append(((piece_end - piece_start).total_seconds(), watts))
    return out


def time_weighted_mean(samples: Iterable[Sample], start: datetime,
                       end: datetime) -> float | None:
    """Mean power over the window, or None when the window holds no reading."""
    pieces = _segments(samples, start, end)
    total = sum(seconds for seconds, _ in pieces)
    if not total:
        return None
    return sum(seconds * watts for seconds, watts in pieces) / total


def spread(samples: Iterable[Sample], start: datetime,
           end: datetime) -> float | None:
    """Peak-to-peak amplitude over the window — the flatness test for a plateau."""
    pieces = _segments(samples, start, end)
    if not pieces:
        return None
    values = [watts for _, watts in pieces]
    return max(values) - min(values)


def time_weighted_quantile(samples: Iterable[Sample], start: datetime,
                           end: datetime, q: float) -> float | None:
    """Quantile by duration — used for a baseline that ignores brief bursts."""
    pieces = sorted(_segments(samples, start, end), key=lambda piece: piece[1])
    total = sum(seconds for seconds, _ in pieces)
    if not total:
        return None
    target = total * q
    seen = 0.0
    for seconds, watts in pieces:
        seen += seconds
        if seen >= target:
            return watts
    return pieces[-1][1]


def seconds_above(samples: Iterable[Sample], start: datetime, end: datetime,
                  threshold: float) -> float:
    """How long the trace stayed strictly above ``threshold`` in the window."""
    return sum(seconds for seconds, watts in _segments(samples, start, end)
               if watts > threshold)
