"""Координатор одной пары «календарь HA → CalDAV-календарь»."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .caldav_client import CalDavAuthError, CalDavClient, CalDavError
from .const import (
    CONF_HORIZON_DAYS,
    CONF_INTERVAL_MINUTES,
    CONF_SOURCE,
    CONF_SUMMARY_PREFIX,
    CONF_TARGET_URL,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_INTERVAL_MINUTES,
    DOMAIN,
    MAX_WRITES_PER_CYCLE,
    SOURCE_RETRY_SECONDS,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_SUSPICIOUS_EMPTY,
    WRITE_PAUSE_SECONDS,
)
from .ical import MirrorEvent
from .sync import EmptyGuard, build_plan, make_event, owner_prefix

_LOGGER = logging.getLogger(__name__)


@dataclass
class MirrorData:
    """Снимок состояния пары для сущностей."""

    status: str = STATUS_PENDING
    last_error: str | None = None
    last_success: dt.datetime | None = None
    source_fetched_at: dt.datetime | None = None
    events_in_window: int = 0
    last_created: int = 0
    last_updated: int = 0
    last_deleted: int = 0
    pending_writes: int = 0


type CalendarMirrorConfigEntry = ConfigEntry[dict[str, "PairCoordinator"]]


class PairCoordinator(DataUpdateCoordinator[MirrorData]):
    """Цикл синхронизации одной пары.

    Не бросает UpdateFailed: статус ошибки живёт в `MirrorData.status` —
    иначе сущности пары (глушилка, статус) становились бы недоступными
    ровно тогда, когда нужнее всего видеть, что сломалось.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CalendarMirrorConfigEntry,
        subentry: ConfigSubentry,
        client: CalDavClient,
    ) -> None:
        interval = subentry.data.get(CONF_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{subentry.subentry_id}",
            update_interval=dt.timedelta(minutes=interval),
            config_entry=entry,
        )
        self.subentry = subentry
        self.client = client
        self.pair_id = subentry.subentry_id
        self.source_entity_id: str = subentry.data[CONF_SOURCE]
        self.target_url: str = subentry.data[CONF_TARGET_URL]
        self.horizon = dt.timedelta(
            days=subentry.data.get(CONF_HORIZON_DAYS, DEFAULT_HORIZON_DAYS)
        )
        self.summary_prefix: str = subentry.data.get(CONF_SUMMARY_PREFIX) or ""
        # Глушилка. Управляется switch-сущностью, восстанавливается после рестарта.
        self.mirror_enabled = True
        self._guard = EmptyGuard()
        self._state = MirrorData()
        self._retry_unsub: CALLBACK_TYPE | None = None
        # Первый цикл идёт в фоне — до него сущностям нужен валидный снимок.
        self.data = self._snapshot()

    def window(self) -> tuple[dt.datetime, dt.datetime]:
        """С начала сегодняшнего дня — чтобы «что сегодня» видело и утро."""
        return dt_util.start_of_local_day(), dt_util.now() + self.horizon

    async def _async_update_data(self) -> MirrorData:
        state = self._state
        start, end = self.window()

        try:
            events = await self._fetch_source(start, end)
        except HomeAssistantError as err:
            state.status, state.last_error = STATUS_ERROR, f"Источник: {err}"
            _LOGGER.warning("%s: %s", self.source_entity_id, state.last_error)
            return self._snapshot()

        state.source_fetched_at = dt_util.utcnow()
        state.events_in_window = len(events)

        if not self.mirror_enabled:
            state.status, state.last_error = STATUS_DISABLED, None
            return self._snapshot()

        try:
            await self._mirror(events, start, end)
        except CalDavAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CalDavError as err:
            state.status, state.last_error = STATUS_ERROR, f"Цель: {err}"
            _LOGGER.warning("%s → %s: %s", self.source_entity_id, self.target_url, err)
        return self._snapshot()

    def _snapshot(self) -> MirrorData:
        # Новый объект, чтобы слушатели координатора видели изменение.
        return MirrorData(**vars(self._state))

    async def _fetch_source(
        self, start: dt.datetime, end: dt.datetime
    ) -> list[CalendarEvent]:
        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(self.source_entity_id) if component else None
        state = self.hass.states.get(self.source_entity_id)
        if not isinstance(entity, CalendarEntity) or (
            state is not None and state.state == STATE_UNAVAILABLE
        ):
            # Частый случай при старте HA: интеграция-источник ещё грузится.
            self._schedule_retry()
            raise HomeAssistantError("календарь не найден или недоступен")
        return await entity.async_get_events(self.hass, start, end)

    def _schedule_retry(self) -> None:
        if self._retry_unsub is not None:
            return

        async def _retry(_now: dt.datetime) -> None:
            self._retry_unsub = None
            await self.async_request_refresh()

        self._retry_unsub = async_call_later(self.hass, SOURCE_RETRY_SECONDS, _retry)

    async def async_shutdown(self) -> None:
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None
        await super().async_shutdown()

    async def _mirror(
        self,
        events: list[CalendarEvent],
        start: dt.datetime,
        end: dt.datetime,
    ) -> None:
        state = self._state
        desired: dict[str, MirrorEvent] = {}
        for event in events:
            mirror = make_event(
                self.pair_id,
                uid=event.uid,
                recurrence_id=event.recurrence_id,
                start=event.start,
                end=event.end,
                summary=event.summary,
                description=event.description,
                location=event.location,
                summary_prefix=self.summary_prefix,
            )
            desired[mirror.uid] = mirror

        prefix = owner_prefix(self.pair_id)
        remote = await self.client.list_events(self.target_url, start, end)
        existing = [(r.href, r.event) for r in remote if r.event.uid.startswith(prefix)]

        allow_deletes = self._guard.allow_deletes(len(desired), len(existing))
        plan = build_plan(desired, existing, allow_deletes=allow_deletes)

        ops: list[tuple[str, str, MirrorEvent | None]] = [
            *(("create", self.client.event_href(self.target_url, e.uid), e) for e in plan.create),
            *(("update", href, e) for href, e in plan.update),
            *(("delete", href, None) for href in plan.delete),
        ]
        done = {"create": 0, "update": 0, "delete": 0}
        stamp = dt_util.utcnow()
        try:
            for index, (kind, href, event) in enumerate(ops[:MAX_WRITES_PER_CYCLE]):
                if index:
                    await asyncio.sleep(WRITE_PAUSE_SECONDS)
                if event is None:
                    await self.client.delete_event(href)
                else:
                    await self.client.put_event(href, event, stamp)
                done[kind] += 1
        finally:
            # Даже при обрыве на середине фиксируем, что успели.
            state.last_created = done["create"]
            state.last_updated = done["update"]
            state.last_deleted = done["delete"]
            state.pending_writes = len(ops) - sum(done.values())
            if any(done.values()):
                _LOGGER.info(
                    "%s → %s: +%d ~%d -%d (осталось %d)",
                    self.source_entity_id,
                    self.target_url,
                    done["create"],
                    done["update"],
                    done["delete"],
                    state.pending_writes,
                )

        state.last_success = dt_util.utcnow()
        if self._guard.suspicious:
            state.status = STATUS_SUSPICIOUS_EMPTY
            state.last_error = "Источник вернул 0 событий — удаления отложены"
        else:
            state.status, state.last_error = STATUS_OK, None

