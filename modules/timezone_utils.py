from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc
SAO_PAULO_TZ = ZoneInfo("America/Sao_Paulo")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_sao_paulo() -> datetime:
    return datetime.now(SAO_PAULO_TZ)


def today_sao_paulo() -> date:
    return now_sao_paulo().date()


def ensure_utc_datetime(value: Any) -> Any:
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_utc_datetimes(value: Any) -> Any:
    if isinstance(value, datetime):
        return ensure_utc_datetime(value)
    if isinstance(value, dict):
        return {k: normalize_utc_datetimes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_utc_datetimes(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_utc_datetimes(item) for item in value)
    return value
