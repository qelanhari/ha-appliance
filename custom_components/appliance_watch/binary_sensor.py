"""One binary sensor per watcher — is a cycle running? — and the washer's
"waiting for the sun".

The running ones are what the Live Activity automations trigger on — a discrete
transition, never the power sensor itself, which changes several times a minute
and would have iOS throttle the activity away.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, WASHER, WATCHERS
from .coordinator import ApplianceCoordinator
from .entity import ApplianceEntity
from .logic.detector import Phase


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ApplianceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [RunningBinarySensor(coordinator, watcher)
                                          for watcher in WATCHERS]
    entities.append(WaitingForSunBinarySensor(coordinator))
    async_add_entities(entities)


class RunningBinarySensor(ApplianceEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: ApplianceCoordinator, watcher: str) -> None:
        super().__init__(coordinator, watcher, "en_marche")

    @property
    def is_on(self) -> bool:
        return self.watcher_state.phase is Phase.RUNNING

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.watcher_state
        return {
            "debut": state.started_at.isoformat() if state.started_at else None,
            "duree_attendue_min": round(state.expected.total_seconds() / 60),
            # The longest dead time ever seen inside a cycle: what tells a
            # machine that has been stopped from one between two heats.
            "temps_mort_max_s": {
                name: round(fingerprint.longest_pause_s)
                for name, fingerprint in state.fingerprints.items()
                if fingerprint.longest_pause_s
            },
        }


class WaitingForSunBinarySensor(ApplianceEntity, BinarySensorEntity):
    """The washer is armed for remote start and waits for the sun.

    Its attributes say why it has not started yet: the surplus the house has
    held, and what a cycle started now would save.
    """

    _attr_icon = "mdi:weather-sunny-alert"

    def __init__(self, coordinator: ApplianceCoordinator) -> None:
        super().__init__(coordinator, WASHER, "attente_soleil")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.solar.configured

    @property
    def is_on(self) -> bool:
        return self.coordinator.solar.decision.waiting

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        decision = self.coordinator.solar.decision
        estimate = decision.estimate
        return {
            "economie_requise_pct": (round(decision.required * 100)
                                     if decision.required is not None else None),
            "raison": decision.reason or None,
            "surplus_tenu_w": round(estimate.surplus_w) if estimate else None,
            "economie_estimee_pct": round(estimate.saving * 100) if estimate else None,
            "cout_estime_eur": estimate.cost_eur if estimate else None,
            "cout_reseau_eur": estimate.grid_cost_eur if estimate else None,
        }
