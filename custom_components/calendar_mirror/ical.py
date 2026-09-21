"""Минимальная работа с iCalendar: сборка VEVENT и разбор ответа сервера.

Не зависит от HA. Полноценная библиотека не нужна: мы пишем только собственные
неповторяющиеся события и читаем обратно в основном их же.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRODID = "-//he110//HA Calendar Mirror//RU"


@dataclass(frozen=True)
class MirrorEvent:
    """Событие в том виде, в каком оно лежит в целевом календаре.

    `start`/`end` — `datetime` с таймзоной для событий со временем
    либо `date` для событий на весь день.
    """

    uid: str
    summary: str
    start: dt.date | dt.datetime
    end: dt.date | dt.datetime
    description: str = ""
    location: str = ""

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, dt.datetime)


# --- Сборка ------------------------------------------------------------------


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Сложить строку по 75 октетов (RFC 5545 §3.1), не разрывая UTF-8 символы."""
    parts: list[str] = []
    current = ""
    size = 0
    limit = 75
    for char in line:
        char_size = len(char.encode())
        if size + char_size > limit:
            parts.append(current)
            current, size, limit = "", 0, 74  # продолжение начинается с пробела
        current += char
        size += char_size
    parts.append(current)
    return "\r\n ".join(parts)


def _format_dt(prop: str, value: dt.date | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        # UTC без VTIMEZONE: Яндекс при правке сериалов с TZID сдвигает время
        # на час после перехода на летнее время — у UTC этой проблемы нет.
        utc = value.astimezone(dt.UTC)
        return f"{prop}:{utc.strftime('%Y%m%dT%H%M%SZ')}"
    return f"{prop};VALUE=DATE:{value.strftime('%Y%m%d')}"


def build_vcalendar(event: MirrorEvent, dtstamp: dt.datetime) -> str:
    """Собрать VCALENDAR с одним VEVENT."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{dtstamp.astimezone(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}",
        _format_dt("DTSTART", event.start),
        _format_dt("DTEND", event.end),
        f"SUMMARY:{_escape(event.summary)}",
    ]
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# --- Разбор ------------------------------------------------------------------

_UNESCAPE_RE = re.compile(r"\\([\\;,nN])")


def _unescape(text: str) -> str:
    return _UNESCAPE_RE.sub(
        lambda m: "\n" if m.group(1) in "nN" else m.group(1), text
    )


def _unfold(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text).split("\n")


def _split_prop(line: str) -> tuple[str, dict[str, str], str]:
    """`DTSTART;TZID=Europe/Moscow:20260921T183000` → имя, параметры, значение."""
    head, _, value = line.partition(":")
    name, *raw_params = head.split(";")
    params: dict[str, str] = {}
    for raw in raw_params:
        key, _, val = raw.partition("=")
        params[key.upper()] = val.strip('"')
    return name.upper(), params, value


def _parse_dt(params: dict[str, str], value: str) -> dt.date | dt.datetime:
    value = value.strip()
    if params.get("VALUE") == "DATE" or len(value) == 8:
        return dt.datetime.strptime(value, "%Y%m%d").date()
    if value.endswith("Z"):
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    naive = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    tzid = params.get("TZID")
    if tzid:
        try:
            return naive.replace(tzinfo=ZoneInfo(tzid))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    # Плавающее время или неизвестная зона: для сравнения считаем UTC.
    # Свои события мы всегда пишем в UTC, так что сюда попадают только чужие.
    return naive.replace(tzinfo=dt.UTC)


def parse_vevents(text: str) -> list[MirrorEvent]:
    """Достать все VEVENT из iCalendar-текста. Битые блоки пропускаются."""
    events: list[MirrorEvent] = []
    props: dict[str, tuple[dict[str, str], str]] | None = None
    depth = 0  # вложенность внутри VEVENT (VALARM и т.п.)
    for line in _unfold(text):
        if not line:
            continue
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            props, depth = {}, 0
            continue
        if props is None:
            continue
        if upper.startswith("BEGIN:"):
            depth += 1
            continue
        if upper == "END:VEVENT":
            event = _build_event(props)
            if event is not None:
                events.append(event)
            props = None
            continue
        if upper.startswith("END:"):
            depth -= 1
            continue
        if depth:
            continue
        name, params, value = _split_prop(line)
        props.setdefault(name, (params, value))
    return events


def _build_event(props: dict[str, tuple[dict[str, str], str]]) -> MirrorEvent | None:
    if "UID" not in props or "DTSTART" not in props:
        return None
    try:
        start = _parse_dt(*props["DTSTART"])
        if "DTEND" in props:
            end = _parse_dt(*props["DTEND"])
        elif isinstance(start, dt.datetime):
            end = start
        else:
            end = start + dt.timedelta(days=1)
    except ValueError:
        return None
    return MirrorEvent(
        uid=props["UID"][1].strip(),
        summary=_unescape(props.get("SUMMARY", ({}, ""))[1]),
        start=start,
        end=end,
        description=_unescape(props.get("DESCRIPTION", ({}, ""))[1]),
        location=_unescape(props.get("LOCATION", ({}, ""))[1]),
    )
