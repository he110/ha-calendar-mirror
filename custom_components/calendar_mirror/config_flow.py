"""Config flow: CalDAV-аккаунт + пары синхронизации (subentries)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import slugify

from .caldav_client import CalDavAuthError, CalDavClient, CalDavError, CalendarInfo
from .const import (
    CONF_HORIZON_DAYS,
    CONF_INTERVAL_MINUTES,
    CONF_SLUG,
    CONF_SOURCE,
    CONF_SUMMARY_PREFIX,
    CONF_TARGET_NAME,
    CONF_TARGET_URL,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_URL,
    DOMAIN,
    MIN_INTERVAL_MINUTES,
    SUBENTRY_PAIR,
)

_LOGGER = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9_]+$")


async def _list_calendars(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> list[CalendarInfo]:
    client = CalDavClient(
        async_get_clientsession(hass),
        data[CONF_URL],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )
    return await client.list_calendars()


async def _validate_account(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> dict[str, str]:
    """Проверить логин/пароль. Возвращает errors для формы."""
    try:
        calendars = await _list_calendars(hass, data)
    except CalDavAuthError:
        return {"base": "invalid_auth"}
    except CalDavError as err:
        _LOGGER.debug("CalDAV недоступен: %s", err)
        return {"base": "cannot_connect"}
    if not calendars:
        return {"base": "no_calendars"}
    return {}


class CalendarMirrorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Добавление CalDAV-аккаунта."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_USERNAME] = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(
                f"{user_input[CONF_URL]}|{user_input[CONF_USERNAME]}".lower()
            )
            self._abort_if_unique_id_configured()
            errors = await _validate_account(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME], data=user_input
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=DEFAULT_URL): str,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors = await _validate_account(self.hass, data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_PAIR: PairSubentryFlow}


class PairSubentryFlow(ConfigSubentryFlow):
    """Пара «календарь HA → календарь CalDAV»."""

    _calendars: list[CalendarInfo]

    async def _load_calendars(self) -> str | None:
        """Подтянуть календари аккаунта. Возвращает причину abort или None."""
        try:
            self._calendars = await _list_calendars(self.hass, self._get_entry().data)
        except CalDavAuthError:
            return "invalid_auth"
        except CalDavError:
            return "cannot_connect"
        return None if self._calendars else "no_calendars"

    def _schema(self, *, with_slug: bool) -> vol.Schema:
        fields: dict[Any, Any] = {
            vol.Required(CONF_SOURCE): EntitySelector(
                EntitySelectorConfig(domain="calendar")
            ),
            vol.Required(CONF_TARGET_URL): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=c.url, label=c.name)
                        for c in self._calendars
                    ]
                )
            ),
        }
        if with_slug:
            fields[vol.Optional(CONF_SLUG)] = str
        fields |= {
            vol.Required(
                CONF_HORIZON_DAYS, default=DEFAULT_HORIZON_DAYS
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=365, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_INTERVAL_MINUTES, default=DEFAULT_INTERVAL_MINUTES
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_INTERVAL_MINUTES, max=1440, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Optional(CONF_SUMMARY_PREFIX): str,
        }
        return vol.Schema(fields)

    def _validate(
        self, user_input: dict[str, Any], *, current_subentry_id: str | None
    ) -> dict[str, str]:
        errors: dict[str, str] = {}
        slug = user_input.get(CONF_SLUG)
        if slug is not None:
            if not SLUG_RE.match(slug):
                errors[CONF_SLUG] = "invalid_slug"
            elif any(
                sub.data.get(CONF_SLUG) == slug
                for sub_id, sub in self._get_entry().subentries.items()
                if sub_id != current_subentry_id
            ):
                errors[CONF_SLUG] = "slug_taken"
        return errors

    def _finalize(self, user_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        target = next(c for c in self._calendars if c.url == user_input[CONF_TARGET_URL])
        state = self.hass.states.get(user_input[CONF_SOURCE])
        source_name = state.name if state else user_input[CONF_SOURCE]
        data = {
            **user_input,
            CONF_TARGET_NAME: target.name,
            CONF_HORIZON_DAYS: int(user_input[CONF_HORIZON_DAYS]),
            CONF_INTERVAL_MINUTES: int(user_input[CONF_INTERVAL_MINUTES]),
            CONF_SUMMARY_PREFIX: user_input.get(CONF_SUMMARY_PREFIX, ""),
        }
        return f"{source_name} → {target.name}", data

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if reason := await self._load_calendars():
            return self.async_abort(reason=reason)

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_SLUG):
                # calendar.family → family
                user_input[CONF_SLUG] = slugify(user_input[CONF_SOURCE].split(".", 1)[1])
            errors = self._validate(user_input, current_subentry_id=None)
            if not errors:
                title, data = self._finalize(user_input)
                unique_id = f"{data[CONF_SOURCE]}|{data[CONF_TARGET_URL]}"
                if any(
                    sub.unique_id == unique_id
                    for sub in self._get_entry().subentries.values()
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_create_entry(
                    title=title, data=data, unique_id=unique_id
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                self._schema(with_slug=True), user_input
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if reason := await self._load_calendars():
            return self.async_abort(reason=reason)

        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate(user_input, current_subentry_id=subentry.subentry_id)
            if not errors:
                # slug не меняем: от него зависят entity_id.
                user_input[CONF_SLUG] = subentry.data[CONF_SLUG]
                title, data = self._finalize(user_input)
                # Перезагрузку сделает update listener интеграции.
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=title,
                    data=data,
                    unique_id=f"{data[CONF_SOURCE]}|{data[CONF_TARGET_URL]}",
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                self._schema(with_slug=False), user_input or dict(subentry.data)
            ),
            errors=errors,
        )
