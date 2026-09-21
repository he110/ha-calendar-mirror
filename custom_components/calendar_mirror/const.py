"""Константы интеграции Calendar Mirror."""

from __future__ import annotations

DOMAIN = "calendar_mirror"
MANUFACTURER = "Calendar Mirror"

DEFAULT_URL = "https://caldav.yandex.ru/"

SUBENTRY_PAIR = "pair"

# Поля пары (config subentry).
CONF_SOURCE = "source_entity_id"
CONF_TARGET_URL = "target_calendar_url"
CONF_TARGET_NAME = "target_calendar_name"
CONF_SLUG = "slug"
CONF_HORIZON_DAYS = "horizon_days"
CONF_INTERVAL_MINUTES = "interval_minutes"
CONF_SUMMARY_PREFIX = "summary_prefix"

DEFAULT_HORIZON_DAYS = 30
DEFAULT_INTERVAL_MINUTES = 10
MIN_INTERVAL_MINUTES = 5

# Яндекс ограничивает запись через CalDAV (ловим 504), поэтому пишем
# последовательно, с паузой и не больше лимита за цикл — остаток догонит следующий.
WRITE_PAUSE_SECONDS = 1.0
MAX_WRITES_PER_CYCLE = 50

# Если источник ещё не загрузился (старт HA) — повторить раньше интервала.
SOURCE_RETRY_SECONDS = 60

# Статусы пары (sensor.*_status).
STATUS_PENDING = "pending"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
STATUS_SUSPICIOUS_EMPTY = "suspicious_empty"
STATUSES = [
    STATUS_PENDING,
    STATUS_OK,
    STATUS_ERROR,
    STATUS_DISABLED,
    STATUS_SUSPICIOUS_EMPTY,
]
