"""Reads the meters and the washer, runs the detectors, remembers what it learns.

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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DISHWASHER_NOMINAL,
    CONF_DRYER_NOMINAL,
    CONF_DRYER_OFF_DELAY,
    CONF_LAUNDRY_METER,
    CONF_MEASURED,
    CONF_OPTIONAL,
    CONF_STEP_MAX,
    CONF_STEP_MIN,
    CONF_TOTAL,
    CONF_WASHER_STATUS,
    DISHWASHER,
    DOMAIN,
    DRYER,
    LEGACY_LAUNDRY,
    STORAGE_VERSION,
    TICK_SECONDS,
    WASHER,
    entry_value,
)
from .logic.detector import (
    DryerConfig,
    DryerDetector,
    Phase,
    PlateauConfig,
    PlateauDetector,
    Transition,
    elapsed_ratio,
    remaining_minutes,
)
from .logic.fingerprints import Fingerprint, as_dict, expected_duration, from_dict, record
from .logic.residual import Reading, residual_watts, to_watts
from .logic.washer import WasherFollower, WasherReading
from .solar import WasherSolarStart

_LOGGER = logging.getLogger(__name__)

EVENT_CYCLE = f"{DOMAIN}_cycle"
# Translation keys of the ThinQ entities found next to the washer's status.
THINQ_REMAINING = "remain"
THINQ_TOTAL = "total"
THINQ_REMOTE = "remote_control_enabled"
THINQ_OPERATION = "operation_mode"
# Armed for remote start, the washer dozes off: woken before it is started.
THINQ_ASLEEP = "sleep"
UNAVAILABLE = ("unknown", "unavailable", "")
# Store key of the solar-start switch, next to the watchers'.
SOLAR_ENABLED = "solar_start_enabled"


@dataclass
class WatcherState:
    """What one watcher shows right now."""

    appliance: str
    phase: Phase = Phase.IDLE
    started_at: datetime | None = None
    expected: timedelta = timedelta(minutes=60)
    power_w: float = 0.0
    last_duration: float | None = None
    last_energy: float | None = None
    finished_at: datetime | None = None
    total_energy_wh: float = 0.0
    # What the machine is doing inside its cycle: "chauffe" / "cycle" for the
    # dishwasher, ThinQ's stage for the washer.
    running_stage: str | None = None
    heats: int = 0
    fingerprints: dict[str, Fingerprint] = field(default_factory=dict)

    @property
    def expected_heats(self) -> int | None:
        fingerprint = self.fingerprints.get(self.appliance)
        return fingerprint.median_heats if fingerprint else None

    @property
    def stage(self) -> str:
        """Where the cycle is, in words the phone can show."""
        if self.phase is Phase.FINISHED:
            return "termine"
        if self.phase is not Phase.RUNNING:
            return "veille"
        return self.running_stage or "cycle"

    @property
    def expected_end(self) -> datetime | None:
        if self.phase is not Phase.RUNNING or self.started_at is None:
            return None
        return self.started_at + self.expected

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


@dataclass(frozen=True)
class WasherEntities:
    """The ThinQ entities the washer is read from — and started through."""

    status: str | None = None
    remaining: str | None = None
    total: str | None = None
    remote: str | None = None
    operation: str | None = None


class ApplianceCoordinator(DataUpdateCoordinator[dict[str, WatcherState]]):
    """One entry watches one house: a dishwasher, a washer and a dryer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsubscribe: list[Any] = []

        self._dishwasher = PlateauDetector(self._plateau_config())
        self._dryer = DryerDetector(self._dryer_config())
        self._washer = WasherFollower(WASHER)
        self._washer_entities = WasherEntities()
        self.solar = WasherSolarStart(hass, entry, self._state)
        self.states: dict[str, WatcherState] = {
            watcher: WatcherState(appliance=watcher)
            for watcher in (DISHWASHER, WASHER, DRYER)
        }
        self.residual_w: float | None = None
        self._laundry_w: float | None = None

    @property
    def baseline_w(self) -> float | None:
        """Ambient unmeasured load the dishwasher's step is measured against."""
        return self._dishwasher.baseline_w

    # ---------------------------------------------------------------- setup

    async def async_setup(self) -> None:
        """Load what was learned, then start listening."""
        await self._load_state()
        self._washer_entities = self._find_washer_entities()
        self.solar.remote = self._washer_entities.remote
        self.solar.operation = self._washer_entities.operation
        sources = [*self._measured_entities(required=True),
                   *self._measured_entities(required=False),
                   entry_value(self.entry, CONF_TOTAL),
                   entry_value(self.entry, CONF_LAUNDRY_METER),
                   self._washer_entities.status,
                   self._washer_entities.remaining,
                   self._washer_entities.total,
                   # Arming is worth an immediate look, not the next tick.
                   self._washer_entities.remote]
        watched = [entity for entity in sources if entity]
        self._unsubscribe.append(
            async_track_state_change_event(self.hass, watched, self._on_reading))
        self._unsubscribe.append(
            async_track_time_interval(self.hass, self._on_tick,
                                      timedelta(seconds=TICK_SECONDS)))

    async def async_teardown(self) -> None:
        while self._unsubscribe:
            self._unsubscribe.pop()()

    def _find_washer_entities(self) -> WasherEntities:
        """The status sensor from the config, its siblings from its device."""
        status = entry_value(self.entry, CONF_WASHER_STATUS)
        if not status:
            _LOGGER.warning("Aucun capteur d'état de lave-linge configuré : "
                            "le sèche-linge ne sera pas détecté (Options)")
            return WasherEntities()
        registry = er.async_get(self.hass)
        found = registry.async_get(status)
        if found is None or found.device_id is None:
            return WasherEntities(status=status)
        siblings = {entity.translation_key: entity.entity_id
                    for entity in er.async_entries_for_device(registry, found.device_id)
                    if entity.platform == found.platform}
        return WasherEntities(status=status,
                              remaining=siblings.get(THINQ_REMAINING),
                              total=siblings.get(THINQ_TOTAL),
                              remote=siblings.get(THINQ_REMOTE),
                              operation=siblings.get(THINQ_OPERATION))

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

    def _dryer_config(self) -> DryerConfig:
        base = DryerConfig()
        return DryerConfig(
            off_delay=timedelta(minutes=float(entry_value(
                self.entry, CONF_DRYER_OFF_DELAY,
                base.off_delay.total_seconds() / 60))),
            nominal=timedelta(minutes=float(entry_value(
                self.entry, CONF_DRYER_NOMINAL,
                base.nominal.total_seconds() / 60))),
        )

    def _measured_entities(self, *, required: bool) -> list[str]:
        key = CONF_MEASURED if required else CONF_OPTIONAL
        return list(entry_value(self.entry, key, []) or [])

    # ------------------------------------------------------------- reading

    def _state(self, entity_id: str | None) -> Any:
        """The raw state, or None when the entity has nothing to say."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE:
            return None
        return state

    def _read(self, entity_id: str | None) -> float | None:
        """A power reading in watts, or None when the sensor has nothing to say."""
        state = self._state(entity_id)
        if state is None:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        return to_watts(value, state.attributes.get("unit_of_measurement"))

    def _washer_reading(self) -> WasherReading:
        entities = self._washer_entities
        status = self._state(entities.status)
        remaining = self._state(entities.remaining)
        total = self._state(entities.total)
        try:
            minutes = float(total.state) if total else None
        except ValueError:
            minutes = None
        return WasherReading(
            status=status.state if status else None,
            ends_at=dt_util.parse_datetime(remaining.state) if remaining else None,
            total=timedelta(minutes=minutes) if minutes else None,
        )

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
    def _on_reading(self, event: Event[EventStateChangedData]) -> None:
        self.hass.async_create_task(self._refresh(dt_util.utcnow()))

    @callback
    def _on_tick(self, now: datetime) -> None:
        self.hass.async_create_task(self._refresh(dt_util.utcnow(), tick=True))

    async def _refresh(self, now: datetime, *, tick: bool = False) -> None:
        """Process, then tell the entities."""
        await self._process(now, tick=tick)
        self.async_set_updated_data(self.states)

    async def _process(self, now: datetime, *, tick: bool = False) -> None:
        transitions = self._dishwasher_events(now, tick)
        # The washer first: whether it runs is what the dryer is judged by.
        reading = self._washer_reading()
        transitions += self._laundry_events(now, tick, reading)
        for watcher, transition in transitions:
            await self._apply(watcher, transition)
        self._update_live_values()
        await self.solar.process(now, washer_running=self._washer.running,
                                 asleep=reading.status == THINQ_ASLEEP,
                                 total=reading.total)

    def _dishwasher_events(self, now: datetime,
                           tick: bool) -> list[tuple[str, Transition]]:
        self.residual_w = self._residual()
        if self.residual_w is None:
            return []
        events = (self._dishwasher.tick(now) if tick
                  else self._dishwasher.feed(now, self.residual_w))
        return [(DISHWASHER, event) for event in events]

    def _laundry_events(self, now: datetime, tick: bool,
                        reading: WasherReading) -> list[tuple[str, Transition]]:
        out: list[tuple[str, Transition]] = []
        for event in self._washer.update(now, reading):
            out.append((WASHER, event))
            if event.kind == "started" and event.started_at is not None:
                out += [(DRYER, cancelled) for cancelled
                        in self._dryer.washer_started(event.started_at)]
        laundry_w = self._read(entry_value(self.entry, CONF_LAUNDRY_METER))
        washer = self._washer.running
        if laundry_w is not None:
            events = (self._dryer.tick(now, washer=washer) if tick
                      else self._dryer.feed(now, laundry_w, washer=washer))
            out += [(DRYER, event) for event in events]
        self._washer.meter(now, laundry_w, counted=self._dryer.phase is Phase.IDLE)
        self._laundry_w = laundry_w
        return out

    def _update_live_values(self) -> None:
        """Estimated draw and stage per watcher, for the tiles and the phone."""
        dishwasher = self.states[DISHWASHER]
        base = self._dishwasher.baseline_w
        if (dishwasher.phase is Phase.RUNNING and self.residual_w is not None
                and base is not None):
            # Only the step above the ambient base belongs to the appliance;
            # the rest of the residual is the fridge, the lights, the router.
            dishwasher.power_w = max(0.0, round(self.residual_w - base, 1))
        else:
            dishwasher.power_w = 0.0
        dishwasher.running_stage = "chauffe" if self._dishwasher.heating else "cycle"
        dishwasher.heats = self._dishwasher.heats or dishwasher.heats

        # The meter cannot split the two machines: while both run, its whole
        # reading is the dryer's — the same rule as the energy.
        laundry_w = self._laundry_w or 0.0
        dryer, washer = self.states[DRYER], self.states[WASHER]
        dryer.power_w = laundry_w if dryer.phase is Phase.RUNNING else 0.0
        washer.power_w = (laundry_w if washer.phase is Phase.RUNNING
                          and dryer.phase is not Phase.RUNNING else 0.0)
        washer.running_stage = self._washer.stage
        washer.expected = self._washer.expected or washer.expected
        if self._washer.cycle is not None:
            # Dated from the machine's plan, which can land just after the start.
            washer.started_at = self._washer.cycle.started_at

    # ---------------------------------------------------------- transitions

    async def _apply(self, watcher: str, transition: Transition) -> None:
        state = self.states[watcher]
        if transition.kind == "started":
            state.phase = Phase.RUNNING
            state.heats = 0
            state.started_at = transition.started_at
            state.expected = self._expected_for(watcher, state)
        elif transition.kind == "cancelled":
            state.phase = Phase.IDLE
            state.started_at = None
            state.power_w = 0.0
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
        state.running_stage = None
        state.heats = int(transition.heats or state.heats)
        state.finished_at = transition.at
        state.last_duration = (round(transition.duration_minutes, 1)
                               if transition.duration_minutes else None)
        state.last_energy = transition.energy_wh
        state.total_energy_wh += transition.energy_wh or 0.0
        state.power_w = 0.0
        if transition.duration_minutes and state.appliance != WASHER:
            # The washer announces its own length; only the others are learned.
            state.fingerprints[state.appliance] = record(
                state.fingerprints.get(state.appliance, Fingerprint()),
                transition.duration_minutes, transition.energy_wh or 0.0,
                transition.longest_pause_s or 0.0, int(transition.heats or 0))
        self._apply_learned_rhythm()

    def _apply_learned_rhythm(self) -> None:
        """Tell the dryer detector the worst dead time the dryer has taken.

        Below that, a silence is the machine breathing between heats; above it,
        it has been stopped. Nothing else distinguishes the two.
        """
        dryer = self.states[DRYER].fingerprints.get(DRYER)
        self._dryer.learned_pause_s = dryer.longest_pause_s if dryer else 0.0

    def _expected_for(self, watcher: str, state: WatcherState) -> timedelta:
        if watcher == WASHER:
            return self._washer.expected or state.expected
        nominal = (self._dishwasher.config.nominal if watcher == DISHWASHER
                   else self._dryer.config.nominal)
        fingerprint = state.fingerprints.get(state.appliance)
        if fingerprint is None:
            return nominal
        return expected_duration(fingerprint, nominal)

    # --------------------------------------------------------- persistence

    async def _load_state(self) -> None:
        data = await self._store.async_load() or {}
        for watcher, state in self.states.items():
            saved = data.get(watcher) or _legacy(data, watcher)
            state.total_energy_wh = float(saved.get("total_energy_wh") or 0.0)
            state.last_duration = _optional_float(saved.get("last_duration"))
            state.last_energy = _optional_float(saved.get("last_energy"))
            state.finished_at = _parse_utc(saved.get("finished_at"))
            state.fingerprints = {name: from_dict(value) for name, value
                                  in (saved.get("fingerprints") or {}).items()}
        self.solar.enabled = bool(data.get(SOLAR_ENABLED, True))
        self._apply_learned_rhythm()
        # A cycle in flight is deliberately *not* restored: the meter-read
        # detectors have no trace behind them after a restart. The washer needs
        # none — ThinQ reports it running, and its start, on the first refresh.

    async def _save_state(self) -> None:
        await self._store.async_save({
            SOLAR_ENABLED: self.solar.enabled,
            **{watcher: {
                "total_energy_wh": round(state.total_energy_wh, 1),
                "last_duration": state.last_duration,
                "last_energy": state.last_energy,
                "finished_at": state.finished_at.isoformat() if state.finished_at else None,
                "fingerprints": {name: as_dict(value)
                                 for name, value in state.fingerprints.items()},
            }
            for watcher, state in self.states.items()},
        })

    async def async_set_solar_start(self, enabled: bool) -> None:
        """The switch: let the sun start the washer, or not."""
        self.solar.enabled = enabled
        await self._save_state()
        self.async_set_updated_data(self.states)

    async def async_forget_fingerprints(self) -> None:
        """Drop everything learned — the equivalent of dhwp's pattern reset."""
        for state in self.states.values():
            state.fingerprints = {}
        self._apply_learned_rhythm()
        await self._save_state()
        self.async_set_updated_data(self.states)

    async def _async_update_data(self) -> dict[str, WatcherState]:
        """Read everything once, so entities have values before any event fires."""
        await self._process(dt_util.utcnow())
        return self.states


def _legacy(data: dict[str, Any], watcher: str) -> dict[str, Any]:
    """The dryer's learned rhythm, from the store of the shared laundry watcher.

    Only that: the washer it learned alongside has been replaced, and the
    shared energy total cannot be split after the fact.
    """
    laundry = data.get(LEGACY_LAUNDRY) or {}
    learned = (laundry.get("fingerprints") or {}).get(DRYER)
    if watcher != DRYER or not learned:
        return {}
    return {"fingerprints": {DRYER: learned}}


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
