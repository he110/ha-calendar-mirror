"""Глушилка пары: выключена — в целевой календарь ничего не пишется."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import CalendarMirrorConfigEntry, PairCoordinator
from .entity import MirrorPairEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalendarMirrorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, coordinator in entry.runtime_data.items():
        async_add_entities(
            [MirrorSwitch(coordinator)], config_subentry_id=subentry_id
        )


class MirrorSwitch(MirrorPairEntity, SwitchEntity, RestoreEntity):
    """Вкл/выкл зеркалирования пары. Состояние переживает рестарт."""

    _attr_icon = "mdi:calendar-sync"

    def __init__(self, coordinator: PairCoordinator) -> None:
        super().__init__(coordinator, "mirror", "switch")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_OFF:
            self.coordinator.mirror_enabled = False

    @property
    def is_on(self) -> bool:
        return self.coordinator.mirror_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.mirror_enabled = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.mirror_enabled = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
