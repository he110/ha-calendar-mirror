"""Статус пары синхронизации."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATUSES
from .coordinator import CalendarMirrorConfigEntry, PairCoordinator
from .entity import MirrorPairEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalendarMirrorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, coordinator in entry.runtime_data.items():
        async_add_entities(
            [MirrorStatusSensor(coordinator)], config_subentry_id=subentry_id
        )


class MirrorStatusSensor(MirrorPairEntity, SensorEntity):
    """ok / error / disabled / suspicious_empty / pending + детали в атрибутах."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PairCoordinator) -> None:
        super().__init__(coordinator, "status", "sensor", "_status")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "source": self.coordinator.source_entity_id,
            "last_success": data.last_success,
            "last_error": data.last_error,
            "source_fetched_at": data.source_fetched_at,
            "events_in_window": data.events_in_window,
            "last_created": data.last_created,
            "last_updated": data.last_updated,
            "last_deleted": data.last_deleted,
            "pending_writes": data.pending_writes,
        }
