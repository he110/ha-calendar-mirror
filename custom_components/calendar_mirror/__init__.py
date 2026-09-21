"""Calendar Mirror — зеркало календарей HA в CalDAV-календарь (Яндекс Календарь)."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .caldav_client import CalDavClient
from .const import SUBENTRY_PAIR
from .coordinator import CalendarMirrorConfigEntry, PairCoordinator

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BUTTON,
]


async def async_setup_entry(
    hass: HomeAssistant, entry: CalendarMirrorConfigEntry
) -> bool:
    """Настроить аккаунт и координаторы всех его пар."""
    client = CalDavClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )

    entry.runtime_data = {
        subentry.subentry_id: PairCoordinator(hass, entry, subentry, client)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_PAIR
    }
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Сначала платформы: switch должен восстановить состояние глушилки
    # до первого цикла, иначе выключенная пара успела бы записать в цель.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    for subentry_id, coordinator in entry.runtime_data.items():
        # Первый цикл не блокирует старт: источник может ещё грузиться,
        # а ошибки пары и так видны в её статусе.
        entry.async_create_background_task(
            hass,
            coordinator.async_refresh(),
            f"calendar_mirror first refresh {subentry_id}",
        )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: CalendarMirrorConfigEntry
) -> bool:
    """Выгрузить config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for coordinator in entry.runtime_data.values():
            await coordinator.async_shutdown()
    return unloaded


async def _async_update_listener(
    hass: HomeAssistant, entry: CalendarMirrorConfigEntry
) -> None:
    """Добавили/изменили/удалили пару или обновили пароль — перезагрузиться."""
    await hass.config_entries.async_reload(entry.entry_id)
