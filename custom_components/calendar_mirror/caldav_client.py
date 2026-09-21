"""Минимальный асинхронный CalDAV-клиент поверх aiohttp.

Не зависит от HA. Нужно всего пять операций: discovery, список календарей,
REPORT по диапазону дат, PUT и DELETE.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, urljoin

import aiohttp

from .ical import MirrorEvent, build_vcalendar, parse_vevents

_LOGGER = logging.getLogger(__name__)

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
TIMEOUT = aiohttp.ClientTimeout(total=30)


class CalDavError(Exception):
    """Любая ошибка обмена с CalDAV-сервером."""


class CalDavAuthError(CalDavError):
    """Логин или пароль приложения не подошли (401/403)."""


class CalDavServerError(CalDavError):
    """Сервер перегружен или ограничивает запросы (429/5xx) — повторить позже."""


@dataclass(frozen=True)
class CalendarInfo:
    url: str
    name: str


@dataclass(frozen=True)
class RemoteEvent:
    href: str
    event: MirrorEvent


_PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""

_PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><c:calendar-home-set/></d:prop></d:propfind>"""

_PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:displayname/><d:resourcetype/><c:supported-calendar-component-set/></d:prop>
</d:propfind>"""

_REPORT_RANGE = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:getetag/><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
<c:time-range start="{start}" end="{end}"/>
</c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""


def _utc_stamp(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


class CalDavClient:
    """Клиент одного CalDAV-аккаунта."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._base = url if url.endswith("/") else f"{url}/"
        self._username = username
        # Заголовок собираем сами: aiohttp.BasicAuth объявлен устаревшим.
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_header = f"Basic {token}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200, 201, 204, 207),
    ) -> tuple[int, str]:
        try:
            async with self._session.request(
                method,
                url,
                data=body.encode() if body is not None else None,
                headers={**(headers or {}), "Authorization": self._auth_header},
                timeout=TIMEOUT,
                allow_redirects=True,
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CalDavServerError(f"{method} {url}: {err!r}") from err

        if status in (401, 403):
            raise CalDavAuthError(f"{method} {url}: HTTP {status}")
        if status == 429 or status >= 500:
            raise CalDavServerError(f"{method} {url}: HTTP {status}")
        if status not in ok:
            raise CalDavError(f"{method} {url}: HTTP {status}: {text[:200]}")
        return status, text

    async def _propfind(self, url: str, body: str, depth: str) -> ET.Element:
        _, text = await self._request(
            "PROPFIND",
            url,
            body=body,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        try:
            return ET.fromstring(text)
        except ET.ParseError as err:
            raise CalDavError(f"PROPFIND {url}: некорректный XML") from err

    def _abs(self, href: str) -> str:
        return urljoin(self._base, href)

    async def _calendar_home(self) -> str:
        """principal → calendar-home-set. Фолбэк — стандартный путь Яндекса."""
        try:
            root = await self._propfind(self._base, _PROPFIND_PRINCIPAL, "0")
            principal = root.find(".//d:current-user-principal/d:href", NS)
            if principal is not None and principal.text:
                root = await self._propfind(
                    self._abs(principal.text.strip()), _PROPFIND_HOME, "0"
                )
                home = root.find(".//c:calendar-home-set/d:href", NS)
                if home is not None and home.text:
                    return self._abs(home.text.strip())
        except CalDavAuthError:
            raise
        except CalDavError as err:
            _LOGGER.debug("Discovery не удался, пробуем стандартный путь: %s", err)
        return self._abs(f"/calendars/{quote(self._username)}/")

    async def list_calendars(self) -> list[CalendarInfo]:
        """Календари аккаунта, в которые можно класть события (VEVENT)."""
        home = await self._calendar_home()
        root = await self._propfind(home, _PROPFIND_CALENDARS, "1")
        calendars: list[CalendarInfo] = []
        for response in root.findall("d:response", NS):
            href = response.findtext("d:href", default="", namespaces=NS).strip()
            if response.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            components = response.findall(".//c:supported-calendar-component-set/c:comp", NS)
            if components and not any(c.get("name") == "VEVENT" for c in components):
                continue  # календарь только для задач
            name = response.findtext(".//d:displayname", default="", namespaces=NS)
            url = self._abs(href)
            calendars.append(CalendarInfo(url=url, name=name.strip() or href))
        return calendars

    async def list_events(
        self, calendar_url: str, start: dt.datetime, end: dt.datetime
    ) -> list[RemoteEvent]:
        """Все события календаря, пересекающие [start, end)."""
        body = _REPORT_RANGE.format(start=_utc_stamp(start), end=_utc_stamp(end))
        _, text = await self._request(
            "REPORT",
            calendar_url,
            body=body,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        try:
            root = ET.fromstring(text)
        except ET.ParseError as err:
            raise CalDavError(f"REPORT {calendar_url}: некорректный XML") from err

        result: list[RemoteEvent] = []
        for response in root.findall("d:response", NS):
            href = response.findtext("d:href", default="", namespaces=NS).strip()
            data = response.findtext(".//c:calendar-data", default="", namespaces=NS)
            if not href or not data:
                continue
            result.extend(
                RemoteEvent(href=self._abs(href), event=event)
                for event in parse_vevents(data)
            )
        return result

    def event_href(self, calendar_url: str, uid: str) -> str:
        base = calendar_url if calendar_url.endswith("/") else f"{calendar_url}/"
        return urljoin(base, f"{quote(uid)}.ics")

    async def put_event(
        self, href: str, event: MirrorEvent, dtstamp: dt.datetime
    ) -> None:
        """Создать или перезаписать событие по href."""
        await self._request(
            "PUT",
            href,
            body=build_vcalendar(event, dtstamp),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )

    async def delete_event(self, href: str) -> None:
        # 404 — уже удалено, цель достигнута.
        await self._request("DELETE", href, ok=(200, 204, 404))
