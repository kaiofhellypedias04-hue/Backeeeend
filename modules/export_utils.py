from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def serialize_export_value(value: Any, missing: str = "—") -> str:
    if value is None:
        return missing
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else missing
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)

