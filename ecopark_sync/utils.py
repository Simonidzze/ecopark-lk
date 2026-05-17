from datetime import datetime, timezone
from decimal import Decimal


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def parse_decimal(value):
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def text(value):
    if value is None:
        return ""
    return str(value)


def now_utc_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
