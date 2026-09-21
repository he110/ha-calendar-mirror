"""Базовая сущность пары синхронизации."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SLUG, CONF_TARGET_NAME, DOMAIN, MANUFACTURER
from .coordinator import PairCoordinator


class MirrorPairEntity(CoordinatorEntity[PairCoordinator]):
    """Сущность, привязанная к паре (config subentry).

    `entity_id` задаём явно из slug пары (`switch.calendar_mirror_family`),
    чтобы не зависеть от того, как HA транслитерирует русское имя устройства.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PairCoordinator,
        key: str,
        platform: str,
        object_suffix: str = "",
    ) -> None:
        super().__init__(coordinator)
        subentry = coordinator.subentry
        slug = subentry.data[CONF_SLUG]
        self._attr_unique_id = f"{subentry.subentry_id}_{key}"
        self._attr_translation_key = key
        self.entity_id = f"{platform}.{DOMAIN}_{slug}{object_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer=MANUFACTURER,
            model=f"→ {subentry.data.get(CONF_TARGET_NAME, '')}".strip(),
            entry_type=DeviceEntryType.SERVICE,
        )
