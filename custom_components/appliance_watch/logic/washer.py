"""The washing machine, as LG ThinQ reports it.

Nothing is inferred here: the machine says when it runs, which stage it is in
and when it will end. What this module adds is what the cloud does not give —
the cycle's real start (``ends_at - total``, so a restart mid-cycle recovers
it), its energy read off the laundry meter, and a single answer to the one
question the dryer needs: *is the washer running?*

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .detector import Cycle, Transition

# ThinQ ``current_state`` values that mean a cycle is in flight, mapped to the
# stage shown on the phone. A pause or an error still has a drum full of
# laundry: the cycle is not over until the machine says so.
STAGES: dict[str, str] = {
    "detecting": "detection",
    "running": "lavage",
    "rinsing": "rincage",
    "spinning": "essorage",
    "drying": "sechage",
    "steam_softening": "vapeur",
    "cool_down": "refroidissement",
    "refreshing": "rafraichissement",
    "rinse_hold": "attente_rincage",
    "pause": "pause",
    "error": "erreur",
}


@dataclass(frozen=True)
class WasherReading:
    """One look at the ThinQ entities. ``None`` is "no answer", never "off"."""

    status: str | None
    ends_at: datetime | None = None
    total: timedelta | None = None


class WasherFollower:
    """Opens and closes the washer's cycles on what the machine reports."""

    def __init__(self, appliance: str = "lave_linge") -> None:
        self.appliance = appliance
        self._cycle: Cycle | None = None
        self._status: str | None = None
        # The last stage reported: a cloud outage does not change what the
        # drum is doing.
        self._stage: str | None = None
        self._ends_at: datetime | None = None
        self._total: timedelta | None = None
        self._previous: tuple[datetime, float, bool] | None = None
        # Whether the start has been read off the machine's plan yet.
        self._dated = False

    @property
    def cycle(self) -> Cycle | None:
        return self._cycle

    @property
    def running(self) -> bool | None:
        """True or False when known; None when ThinQ has nothing to say.

        A cycle already open stays open through a cloud outage: the machine
        did not stop because the API went quiet.
        """
        if self._cycle is not None:
            return True
        return None if self._status is None else False

    @property
    def stage(self) -> str | None:
        return self._stage if self._cycle else None

    @property
    def expected(self) -> timedelta | None:
        """Total length of the cycle in flight, as the machine now predicts it."""
        if self._cycle is None:
            return None
        if self._ends_at is not None:
            return max(self._ends_at - self._cycle.started_at, timedelta())
        return self._total

    def update(self, at: datetime, reading: WasherReading) -> list[Transition]:
        """Take the latest ThinQ state; return the cycle it opened or closed."""
        self._status = reading.status
        if reading.status is None:
            return []
        self._ends_at = reading.ends_at or self._ends_at
        self._total = reading.total or self._total
        active = reading.status in STAGES
        if active:
            self._stage = STAGES[reading.status]
        if active and self._cycle is None:
            return [self._start(at, reading)]
        if active and not self._dated:
            self._date(at)
        if not active and self._cycle is not None:
            return [self._finish(at)]
        return []

    def meter(self, at: datetime, watts: float | None, *, counted: bool) -> None:
        """Integrate the laundry meter into the washer's energy.

        ``counted`` is False while the dryer runs too: the meter cannot split
        the two, and its whole reading goes to the dryer rather than twice.
        """
        previous, self._previous = self._previous, (
            None if watts is None else (at, watts, counted))
        if self._cycle is None or previous is None:
            return
        since, held, was_counted = previous
        if was_counted:
            self._cycle.energy_wh += held * (at - since).total_seconds() / 3600

    def _start(self, at: datetime, reading: WasherReading) -> Transition:
        self._cycle = Cycle(started_at=at, appliance=self.appliance, last_active=at)
        self._dated = False
        self._date(at)
        return Transition(kind="started", at=at, appliance=self.appliance,
                          started_at=self._cycle.started_at,
                          reason=f"LG ThinQ : {reading.status}")

    def _date(self, at: datetime) -> None:
        """Date the cycle from the machine's plan, once it is known.

        The machine counts from the moment it was started, not from when Home
        Assistant noticed: this is what recovers the real start after a
        restart, or behind a slow cloud. ThinQ publishes the status a few
        milliseconds before the end and the length, so the plan often arrives
        on the update *after* the start.
        """
        cycle = self._cycle
        if cycle is None or self._ends_at is None or self._total is None:
            return
        cycle.started_at = min(at, self._ends_at - self._total)
        self._dated = True

    def _finish(self, at: datetime) -> Transition:
        cycle = self._cycle
        assert cycle is not None
        self._cycle = None
        self._ends_at = self._total = None  # the next programme brings its own
        return Transition(kind="finished", at=at, appliance=self.appliance,
                          started_at=cycle.started_at,
                          duration_minutes=(at - cycle.started_at).total_seconds() / 60,
                          energy_wh=round(cycle.energy_wh, 1),
                          reason=f"LG ThinQ : {self._status}")


__all__ = ["STAGES", "WasherFollower", "WasherReading"]
