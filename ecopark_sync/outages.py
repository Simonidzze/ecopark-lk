import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime

import requests


DEFAULT_SHUTDOWN_URL = "https://www.eseti.ru/DesktopModules/ResWebApi/API/Shutdown"
DEFAULT_EMERGENCY_URL = "https://www.eseti.ru/DesktopModules/ResWebApi/API/emergency"
PUBLIC_OUTAGE_URL = "https://www.eseti.ru/ShutDownsInf/disconnection.aspx"
ADDRESS_FIELDS = ("region", "city", "street")


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    url: str


def normalize_text(value):
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def matches(event, needle):
    address = " ".join(str(event.get(field) or "") for field in ADDRESS_FIELDS)
    return normalize_text(needle) in normalize_text(address)


def raw_fingerprint(source_key, event):
    payload = json.dumps(
        {"source": source_key, "event": event},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def outage_window_key(source_key, event):
    shutdown = str(event.get("shutdownDate") or "").strip()
    restore = str(event.get("restoreDate") or "").strip()
    if shutdown or restore:
        return shutdown, restore, ""
    return shutdown, restore, raw_fingerprint(source_key, event)


def event_fingerprint(notification_type_key, event):
    group_key = str(event.get("groupKey") or "")
    if not group_key:
        group_key = json.dumps(
            [event.get("shutdownDate"), event.get("restoreDate")],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return hashlib.sha256(f"{notification_type_key}:{group_key}".encode()).hexdigest()


def grouped_payload(group_key, source_keys, match_count):
    shutdown, restore, fallback = group_key
    return {
        "groupKey": json.dumps(group_key, ensure_ascii=False, separators=(",", ":")),
        "shutdownDate": shutdown or None,
        "restoreDate": restore or None,
        "fallbackKey": fallback or None,
        "sourceKeys": sorted(source_keys),
        "matchCount": match_count,
    }


def _display_datetime(value):
    if not value:
        return "не указано"
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.strftime("%d.%m.%Y %H:%M")


def format_message(title, event):
    return "\n".join(
        (
            f"⚡ <b>{html.escape(title)}</b>",
            "",
            f"🕒 Отключение: {_display_datetime(event.get('shutdownDate'))}",
            f"✅ Восстановление: {_display_datetime(event.get('restoreDate'))}",
            "",
            f'🔗 <a href="{PUBLIC_OUTAGE_URL}">Источник: Электросети</a>',
        )
    )


class OutageClient:
    def __init__(self, timeout_seconds, session=None):
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "ecopark-lk-outage-notifier/1.0"})

    def fetch(self, source):
        response = self.session.get(source.url, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError(f"Некорректный ответ источника {source.key}")
        return payload
