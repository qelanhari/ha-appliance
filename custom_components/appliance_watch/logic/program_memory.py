"""Keep SmartThinQ's copy of the armed programme in step with the machine.

LG's official integration starts a washer that dozed off on its *default*
programme: on 26 Oct 2026, armed on Quick 30 (20 °C, 800 rpm), it ran Cotton
(40 °C, 1400 rpm, 189 min) once woken. SmartThinQ sends the programme along
with "start" — the one its first poll after arming saw. Its polling is off,
so the coordinator polls it on demand, and this says when:

* armed, awake and idle for a few seconds, with no sleep in between: REMEMBER
  — the programme the user chose. A poll after a sleep would see the default
  one, so a washer seen asleep before it was remembered is never ready;
* disarmed: FORGET — otherwise the next arming would keep this programme.

Pure module: no Home Assistant import, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class Refresh(Enum):
    REMEMBER = "remember"
    FORGET = "forget"


@dataclass(frozen=True)
class ProgramMemoryConfig:
    # Arming comes last, once the programme is chosen; a short wait lets the
    # cloud catch up with the panel before the programme is read.
    settle: timedelta = timedelta(seconds=30)
    # A poll the cloud dropped is asked again — not every reading.
    retry_after: timedelta = timedelta(minutes=5)


class ProgramMemory:
    """Whether SmartThinQ holds the programme of the current arming."""

    def __init__(self, config: ProgramMemoryConfig | None = None) -> None:
        self.config = config or ProgramMemoryConfig()
        self._was_armed: bool | None = None
        self._armed_since: datetime | None = None
        self._slept = False
        # A disarming SmartThinQ has not been told about yet.
        self._stale = False
        self._asked_at: datetime | None = None
        self.ready = False

    def observe(self, at: datetime, *, armed: bool | None, asleep: bool,
                idle: bool, wanted: bool = True) -> Refresh | None:
        """The poll to make now, if any.

        ``wanted`` is the solar start being on: without it there is nothing to
        remember, but a disarming is still forgotten.
        """
        if armed is None:
            return None
        was_armed, self._was_armed = self._was_armed, armed
        if not armed:
            self._armed_since, self._slept, self.ready = None, False, False
            if was_armed:
                self._stale, self._asked_at = True, None
            if not self._stale or self._recent(at):
                return None
            self._asked_at = at
            return Refresh.FORGET
        if self._armed_since is None:
            self._armed_since, self._asked_at = at, None
        if asleep and not self.ready:
            self._slept = True
        # Still holding the previous programme: this arming cannot be trusted.
        if self.ready or self._slept or self._stale or not (wanted and idle):
            return None
        if at - self._armed_since < self.config.settle or self._recent(at):
            return None
        self._asked_at = at
        return Refresh.REMEMBER

    def remembered(self) -> None:
        """SmartThinQ's poll saw the machine armed and offers remote start."""
        if self._was_armed:
            self.ready = True

    def forgotten(self) -> None:
        """SmartThinQ's poll saw the machine disarmed."""
        self._stale = False

    def _recent(self, at: datetime) -> bool:
        return self._asked_at is not None and at - self._asked_at < self.config.retry_after


__all__ = ["ProgramMemory", "ProgramMemoryConfig", "Refresh"]
