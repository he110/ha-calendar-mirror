"""Внеочередная синхронизация пары."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CalendarMirrorConfigEntry, PairCoordinator
from .entity import MirrorPairEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalendarMirrorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, coordinator in entry.runtime_data.items():
        async_add_entities(
            [MirrorSyncButton(coordinator)], config_subentry_id=subentry_id
        )


class MirrorSyncButton(MirrorPairEntity, ButtonEntity):
    _attr_icon = "mdi:sync"

    def __init__(self, coordinator: PairCoordinator) -> None:
        super().__init__(coordinator, "sync_now", "button", "_sync_now")

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()
