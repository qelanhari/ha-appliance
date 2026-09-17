"""Reads the meters, runs the detectors, remembers what it learns.

All the judgement lives in :mod:`logic`; this module is the Home Assistant
plumbing around it — state subscriptions, a clock tick, persistence, and the
snapshot the entities render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BOTH,
    CONF_CLASSIFY_W,
    CONF_DISHWASHER_NOMINAL,
    CONF_LAUNDRY_METER,
    CONF_LAUNDRY_NOMINAL,
    CONF_MEASURED,
    CONF_OPTIONAL,
    CONF_STEP_MAX,
    CONF_STEP_MIN,
    CONF_TOTAL,
    DISHWASHER,
    DOMAIN,
    LAUNDRY,
    STORAGE_VERSION,
    TICK_SECONDS,
    UNKNOWN,
    entry_value,
)
from .logic.detector import (
    Phase,
    PlateauConfig,
    PlateauDetector,
    SharedMeterConfig,
    SharedMeterDetector,
    Transition,
    elapsed_ratio,
    remaining_minutes,
)
from .logic.fingerprints import Fingerprint, as_dict, expected_duration, from_dict, record
from .logic.residual import Reading, residual_watts, to_watts

_LOGGER = logging.getLogger(__name__)

EVENT_CYCLE = f"{DOMAIN}_cycle"


@dataclass
class WatcherState:
    """What one watcher shows right now."""

    phase: Phase = Phase.IDLE
    appliance: str = UNKNOWN
    started_at: datetime | None = None
    expected: timedelta = timedelta(minutes=60)
    power_w: float = 0.0
    last_duration: float | None = None
    last_energy: float | None = None
    finished_at: datetime | None = None
    total_energy_wh: float = 0.0
    fingerprints: dict[str, Fingerprint] = field(default_factory=dict)

    @property
    def remaining(self) -> float | None:
        if self.phase is not Phase.RUNNING or self.started_at is None:
            return None
        return remaining_minutes(self.started_at, dt_util.utcnow(), self.expected)

    @property
    def progress(self) -> float | None:
        if self.phase is not Phase.RUNNING or self.started_at is None:
            return None
        return elapsed_ratio(self.started_at, dt_util.utcnow(), self.expected)


class ApplianceCoordinator(DataUpdateCoordinator[dict[str, WatcherState]]):
    """One entry watches one house: a dishwasher and a laundry meter."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.entry = entry
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsubscribe: list[Any] = []

        self._dishwasher = PlateauDetector(self._plateau_config())
        self._laundry = SharedMeterDetector(self._shared_config())
        self.states: dict[str, WatcherState] = {
            DISHWASHER: WatcherState(appliance=DISHWASHER),
            LAUNDRY: WatcherState(),
        }
        self.residual_w: float | None = None

    @property
    def baseline_w(self) -> float | None:
        """Ambient unmeasured load the dishwasher's step is measured against."""
        return self._dishwasher.baseline_w

    # ---------------------------------------------------------------- setup

    async def async_setup(self) -> None:
        """Load what was learned, then start listening."""
        await self._load_state()
        sources = [*self._measured_entities(required=True),
                   *self._measured_entities(required=False)]
        sources += [entry_value(self.entry, CONF_TOTAL),
                    entry_value(self.entry, CONF_LAUNDRY_METER)]
        watched = [entity for entity in sources if entity]
        self._unsubscribe.append(
            async_track_state_change_event(self.hass, watched, self._on_reading))
        self._unsubscribe.append(
            async_track_time_interval(self.hass, self._on_tick,
                                      timedelta(seconds=TICK_SECONDS)))

    async def async_teardown(self) -> None:
        while self._unsubscribe:
            self._unsubscribe.pop()()

    # ------------------------------------------------------------ settings

    def _plateau_config(self) -> PlateauConfig:
        base = PlateauConfig()
        return PlateauConfig(
            step_min_w=float(entry_value(self.entry, CONF_STEP_MIN, base.step_min_w)),
            step_max_w=float(entry_value(self.entry, CONF_STEP_MAX, base.step_max_w)),
            nominal=timedelta(minutes=float(entry_value(
                self.entry, CONF_DISHWASHER_NOMINAL,
                base.nominal.total_seconds() / 60))),
        )

    def _shared_config(self) -> SharedMeterConfig:
        base = SharedMeterConfig()
        return SharedMeterConfig(
            classify_w=float(entry_value(self.entry, CONF_CLASSIFY_W, base.classify_w)),
            nominal=timedelta(minutes=float(entry_value(
                self.entry, CONF_LAUNDRY_NOMINAL,
                base.nominal.total_seconds() / 60))),
        )

    def _measured_entities(self, *, required: bool) -> list[str]:
        key = CONF_MEASURED if required else CONF_OPTIONAL
        return list(entry_value(self.entry, key, []) or [])

    # ------------------------------------------------------------- reading

    def _read(self, entity_id: str | None) -> float | None:
        """A power reading in watts, or None when the sensor has nothing to say."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        return to_watts(value, state.attributes.get("unit_of_measurement"))

    def _residual(self) -> float | None:
        total_entity = entry_value(self.entry, CONF_TOTAL)
        total = Reading(total_entity, self._read(total_entity))
        required = list(self._measured_entities(required=True))
        # The laundry meter is a measured load like any other: leaving it in
        # would push a 2 kW washing machine straight into the dishwasher's
        # band. Subtracted here whether or not it was also listed above.
        laundry = entry_value(self.entry, CONF_LAUNDRY_METER)
        if laundry and laundry not in required:
            required.append(laundry)
        measured = [Reading(entity, self._read(entity), required=True)
                    for entity in required]
        measured += [Reading(entity, self._read(entity), required=False)
                     for entity in self._measured_entities(required=False)]
        return residual_watts(total, measured)

    @callback
    def _on_reading(self, event: Event) -> None:
        self.hass.async_create_task(self._refresh(dt_util.utcnow()))

    @callback
    def _on_tick(self, now: datetime) -> None:
        self.hass.async_create_task(self._refresh(dt_util.utcnow(), tick=True))

    async def _refresh(self, now: datetime, *, tick: bool = False) -> None:
        """Process, then tell the entities."""
        await self._process(now, tick=tick)
        self.async_set_updated_data(self.states)

    async def _process(self, now: datetime, *, tick: bool = False) -> None:
        transitions: list[tuple[str, Transition]] = []

        self.residual_w = self._residual()
        if self.residual_w is not None:
            events = (self._dishwasher.tick(now) if tick
                      else self._dishwasher.feed(now, self.residual_w))
            transitions += [(DISHWASHER, event) for event in events]

        laundry_w = self._read(entry_value(self.entry, CONF_LAUNDRY_METER))
        if laundry_w is not None:
            events = (self._laundry.tick(now) if tick
                      else self._laundry.feed(now, laundry_w))
            transitions += [(LAUNDRY, event) for event in events]

        for watcher, transition in transitions:
            await self._apply(watcher, transition)
        self._update_live_values(laundry_w)

    def _update_live_values(self, laundry_w: float | None) -> None:
        """Estimated draw per watcher, for the Energy dashboard and the tiles."""
        dishwasher = self.states[DISHWASHER]
        base = self._dishwasher.baseline_w
        if (dishwasher.phase is Phase.RUNNING and self.residual_w is not None
                and base is not None):
            # Only the step above the ambient base belongs to the appliance;
            # the rest of the residual is the fridge, the lights, the router.
            dishwasher.power_w = max(0.0, round(self.residual_w - base, 1))
        else:
            dishwasher.power_w = 0.0
        laundry = self.states[LAUNDRY]
        laundry.power_w = laundry_w if laundry.phase is Phase.RUNNING and laundry_w else 0.0

    # ---------------------------------------------------------- transitions

    async def _apply(self, watcher: str, transition: Transition) -> None:
        state = self.states[watcher]
        if transition.kind == "started":
            state.phase = Phase.RUNNING
            state.started_at = transition.started_at
            state.appliance = transition.appliance or UNKNOWN
            state.expected = self._expected_for(state)
        elif transition.kind == "identified":
            state.appliance = transition.appliance
            state.expected = self._expected_for(state)
        elif transition.kind == "peer":
            state.appliance = BOTH
        elif transition.kind == "finished":
            self._close(state, transition)
            await self._save_state()  # learned, so it must survive a restart

        self.hass.bus.async_fire(EVENT_CYCLE, {
            "watcher": watcher,
            "kind": transition.kind,
            "appliance": state.appliance,
            "reason": transition.reason,
        })
        _LOGGER.debug("%s: %s (%s)", watcher, transition.kind, transition.reason)

    def _close(self, state: WatcherState, transition: Transition) -> None:
        state.phase = Phase.FINISHED
        state.finished_at = transition.at
        state.last_duration = (round(transition.duration_minutes, 1)
                               if transition.duration_minutes else None)
        state.last_energy = transition.energy_wh
        state.total_energy_wh += transition.energy_wh or 0.0
        state.power_w = 0.0
        key = transition.appliance or state.appliance
        if transition.duration_minutes and key not in (UNKNOWN, BOTH):
            # Two appliances counted as one teaches nothing about either.
            state.fingerprints[key] = record(
                state.fingerprints.get(key, Fingerprint()),
                transition.duration_minutes, transition.energy_wh or 0.0)

    def _expected_for(self, state: WatcherState) -> timedelta:
        nominal = (self._dishwasher.config.nominal if state.appliance == DISHWASHER
                   else self._laundry.config.nominal)
        fingerprint = state.fingerprints.get(state.appliance)
        if fingerprint is None:
            return nominal
        return expected_duration(fingerprint, nominal)

    # --------------------------------------------------------- persistence

    async def _load_state(self) -> None:
        data = await self._store.async_load() or {}
        for watcher, state in self.states.items():
            saved = data.get(watcher) or {}
            state.total_energy_wh = float(saved.get("total_energy_wh") or 0.0)
            state.last_duration = _optional_float(saved.get("last_duration"))
            state.last_energy = _optional_float(saved.get("last_energy"))
            state.finished_at = _parse_utc(saved.get("finished_at"))
            state.fingerprints = {name: from_dict(value) for name, value
                                  in (saved.get("fingerprints") or {}).items()}
        # A cycle in flight is deliberately *not* restored: the detector has no
        # trace behind it after a restart, so it would count a countdown it can
        # neither confirm nor end.

    async def _save_state(self) -> None:
        await self._store.async_save({
            watcher: {
                "total_energy_wh": round(state.total_energy_wh, 1),
                "last_duration": state.last_duration,
                "last_energy": state.last_energy,
                "finished_at": state.finished_at.isoformat() if state.finished_at else None,
                "fingerprints": {name: as_dict(value)
                                 for name, value in state.fingerprints.items()},
            }
            for watcher, state in self.states.items()
        })

    async def async_forget_fingerprints(self) -> None:
        """Drop everything learned — the equivalent of dhwp's pattern reset."""
        for state in self.states.values():
            state.fingerprints = {}
        await self._save_state()
        self.async_set_updated_data(self.states)

    async def _async_update_data(self) -> dict[str, WatcherState]:
        """Read the meters once, so entities have values before any event fires."""
        await self._process(dt_util.utcnow())
        return self.states


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_utc(value: Any) -> datetime | None:
    """Parse a stored timestamp, rejecting a naive one rather than guessing."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = ["ApplianceCoordinator", "EVENT_CYCLE", "WatcherState"]
