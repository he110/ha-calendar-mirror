"""Логика зеркалирования: идентичность событий, диф, защита от массового удаления.

Не зависит от HA — тестируется отдельно.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

from .ical import MirrorEvent

UID_PREFIX = "ha-mirror"


def owner_prefix(pair_id: str) -> str:
    """Префикс UID всех событий одной пары. По нему определяем «своё»."""
    return f"{UID_PREFIX}-{pair_id.lower()}-"


def source_key(
    uid: str | None,
    recurrence_id: str | None,
    start: dt.date | dt.datetime,
    summary: str,
) -> str:
    """Стабильный ключ события источника.

    `recurrence_id` не меняется при переносе одного вхождения серии —
    поэтому перенос становится обновлением, а не удалением+созданием.
    Без `uid` опираемся на название и старт: перенос = пересоздание.
    """
    if uid:
        return f"{uid}|{recurrence_id or start.isoformat()}"
    return f"nouid|{summary}|{start.isoformat()}"


def target_uid(pair_id: str, key: str) -> str:
    digest = hashlib.sha1(key.encode()).hexdigest()[:20]
    return f"{owner_prefix(pair_id)}{digest}"


def make_event(
    pair_id: str,
    *,
    uid: str | None,
    recurrence_id: str | None,
    start: dt.date | dt.datetime,
    end: dt.date | dt.datetime,
    summary: str,
    description: str | None,
    location: str | None,
    summary_prefix: str = "",
) -> MirrorEvent:
    """Событие источника → событие для целевого календаря."""
    key = source_key(uid, recurrence_id, start, summary)
    return MirrorEvent(
        uid=target_uid(pair_id, key),
        summary=f"{summary_prefix}{summary}".strip(),
        start=_to_minute(start),
        end=_to_minute(end),
        description=(description or "").strip(),
        location=(location or "").strip(),
    )


def _to_minute(value: dt.date | dt.datetime) -> dt.date | dt.datetime:
    """Яндекс хранит время с точностью до минуты: 15:51:37 читается обратно как 15:51.
    Без этого событие с секундами считалось бы изменённым в каждом цикле."""
    if isinstance(value, dt.datetime):
        return value.replace(second=0, microsecond=0)
    return value


def _norm_time(value: dt.date | dt.datetime) -> str:
    value = _to_minute(value)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat()
    return value.isoformat()


def _norm_text(value: str) -> str:
    return value.replace("\r\n", "\n").strip()


def fingerprint(event: MirrorEvent) -> tuple[str, ...]:
    """Представление для сравнения: одинаковые события — одинаковый отпечаток,
    независимо от того, как сервер записал таймзону или переносы строк."""
    return (
        _norm_text(event.summary),
        _norm_time(event.start),
        _norm_time(event.end),
        _norm_text(event.description),
        _norm_text(event.location),
    )


@dataclass
class Plan:
    """Что нужно сделать с целевым календарём."""

    create: list[MirrorEvent] = field(default_factory=list)
    update: list[tuple[str, MirrorEvent]] = field(default_factory=list)  # (href, событие)
    delete: list[str] = field(default_factory=list)  # href

    @property
    def empty(self) -> bool:
        return not (self.create or self.update or self.delete)


def build_plan(
    desired: dict[str, MirrorEvent],
    existing: list[tuple[str, MirrorEvent]],
    *,
    allow_deletes: bool = True,
) -> Plan:
    """Сравнить желаемое состояние с тем, что лежит на сервере.

    `existing` — только НАШИ события (с префиксом пары), пары (href, событие).
    """
    plan = Plan()
    seen: set[str] = set()
    for href, current in existing:
        if current.uid in seen:
            # Дубль с тем же UID (например, после сбоя посреди записи) — лишний.
            if allow_deletes:
                plan.delete.append(href)
            continue
        seen.add(current.uid)
        wanted = desired.get(current.uid)
        if wanted is None:
            if allow_deletes:
                plan.delete.append(href)
        elif fingerprint(wanted) != fingerprint(current):
            plan.update.append((href, wanted))
    plan.create = [event for uid, event in desired.items() if uid not in seen]
    return plan


class EmptyGuard:
    """Защита от массового удаления при «пустом» ответе флапающего источника.

    Если источник вернул 0 событий, а на сервере есть хотя бы `min_existing`
    наших, удаления разрешаются только после `cycles` таких циклов подряд.
    """

    def __init__(self, min_existing: int = 3, cycles: int = 3) -> None:
        self._min_existing = min_existing
        self._cycles = cycles
        self._streak = 0

    def allow_deletes(self, desired_count: int, existing_count: int) -> bool:
        if desired_count == 0 and existing_count >= self._min_existing:
            self._streak += 1
            return self._streak >= self._cycles
        self._streak = 0
        return True

    @property
    def suspicious(self) -> bool:
        return 0 < self._streak < self._cycles
