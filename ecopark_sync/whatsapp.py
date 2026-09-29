import base64
import json
import random
import re
import time
from datetime import timedelta
from string import Formatter

import requests
from sqlalchemy import func, select, update

from .claims import render_pretrial_claim_pdf
from .config import env
from .db import make_session_factory
from .models import WhatsAppMessage
from .utils import now_utc_naive


DEFAULT_MESSAGE_TEMPLATE = (
    "Здравствуйте, {owner}!\n\n"
    "Направляем досудебную претензию по участку № {plot_number}, "
    "исх. № {claim_number}. PDF приложен к сообщению."
)
ALLOWED_MESSAGE_FIELDS = {"owner", "plot_number", "claim_number"}


class WhatsAppServiceError(RuntimeError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def normalize_whatsapp_phone(value):
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not 10 <= len(digits) <= 15:
        raise ValueError("Телефон должен содержать от 10 до 15 цифр")
    return digits


def first_whatsapp_phone(values):
    for value in values:
        try:
            return normalize_whatsapp_phone(value)
        except ValueError:
            continue
    return ""


class _StrictFormat(dict):
    def __missing__(self, key):
        raise ValueError(
            "Неизвестное поле в тексте сообщения: "
            f"{{{key}}}. Доступны: {{owner}}, {{plot_number}}, {{claim_number}}"
        )


def format_whatsapp_message(template, **values):
    text = str(template or DEFAULT_MESSAGE_TEMPLATE).strip()
    try:
        for _literal, field_name, format_spec, conversion in Formatter().parse(text):
            if field_name and field_name not in ALLOWED_MESSAGE_FIELDS:
                raise ValueError(
                    f"Неизвестное поле в тексте сообщения: {{{field_name}}}. "
                    "Доступны: {owner}, {plot_number}, {claim_number}"
                )
            if format_spec or conversion:
                raise ValueError(
                    "Форматирование полей в тексте сообщения не поддерживается"
                )
        rendered = text.format_map(_StrictFormat(values))
    except (KeyError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Неизвестное поле"):
            raise
        raise ValueError(f"Некорректный шаблон сообщения: {exc}") from exc
    if not rendered:
        raise ValueError("Текст сообщения не может быть пустым")
    if len(rendered) > 1000:
        raise ValueError("Текст сообщения не должен превышать 1000 символов")
    return rendered


class WhatsAppClient:
    def __init__(self, base_url=None, token=None, timeout=None):
        self.base_url = (base_url or env("WHATSAPP_SERVICE_URL", "http://whatsapp:3000")).rstrip("/")
        self.token = token if token is not None else env("WHATSAPP_SERVICE_TOKEN", "")
        self.timeout = float(timeout or env("WHATSAPP_REQUEST_TIMEOUT_SECONDS", "60"))

    @property
    def headers(self):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method, path, **kwargs):
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise WhatsAppServiceError(
                f"Сервис WhatsApp недоступен: {exc}", retryable=True
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.ok:
            message = payload.get("error") or f"HTTP {response.status_code}"
            retryable = response.status_code >= 500 or response.status_code in {408, 409, 429}
            raise WhatsAppServiceError(message, retryable=retryable)
        return payload

    def status(self):
        return self._request("GET", "/status")

    def logout(self):
        return self._request("POST", "/logout")

    def send_pdf(self, phone, message, filename, document):
        document.seek(0)
        payload = {
            "phone": normalize_whatsapp_phone(phone),
            "message": message,
            "filename": filename,
            "pdf_base64": base64.b64encode(document.read()).decode("ascii"),
        }
        return self._request("POST", "/send", json=payload)


def whatsapp_queue_stats(session):
    return dict(
        session.execute(
            select(WhatsAppMessage.status, func.count(WhatsAppMessage.id)).group_by(
                WhatsAppMessage.status
            )
        ).all()
    )


def fail_stale_processing_messages(session, minutes=30):
    cutoff = now_utc_naive() - timedelta(minutes=minutes)
    result = session.execute(
        update(WhatsAppMessage)
        .where(WhatsAppMessage.status == "processing")
        .where(WhatsAppMessage.started_at < cutoff)
        .values(
            status="failed",
            error_text=(
                "Отправка была прервана. Перед повтором проверьте WhatsApp, "
                "чтобы не отправить документ дважды."
            ),
        )
    )
    return result.rowcount


def process_next_whatsapp_message(client=None, session_factory=None):
    client = client or WhatsAppClient()
    Session = session_factory or make_session_factory()
    with Session() as session:
        message = session.scalar(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.status == "queued")
            .order_by(WhatsAppMessage.created_at, WhatsAppMessage.id)
            .limit(1)
        )
        if message is None:
            return None
        message.status = "processing"
        message.started_at = now_utc_naive()
        message.attempts += 1
        message_id = message.id
        values_json = message.claim_values_json
        phone = message.phone
        text = message.message_text
        filename = message.filename
        session.commit()

    try:
        document = render_pretrial_claim_pdf(json.loads(values_json))
        result = client.send_pdf(phone, text, filename, document)
    except Exception as exc:
        next_status = "failed"
        with Session() as session:
            message = session.get(WhatsAppMessage, message_id)
            if message is not None:
                retryable = isinstance(exc, WhatsAppServiceError) and exc.retryable
                max_attempts = int(env("WHATSAPP_MAX_ATTEMPTS", "1"))
                next_status = "queued" if retryable and message.attempts < max_attempts else "failed"
                message.status = next_status
                message.error_text = str(exc)[:4000]
                session.commit()
        return "retry" if next_status == "queued" else "failed"

    with Session() as session:
        message = session.get(WhatsAppMessage, message_id)
        if message is not None:
            message.status = "sent"
            message.sent_at = now_utc_naive()
            message.external_message_id = str(result.get("message_id") or "")[:255] or None
            message.error_text = None
            session.commit()
    return "sent"


def run_whatsapp_worker():
    from .schema import ensure_whatsapp_schema

    ensure_whatsapp_schema()
    client = WhatsAppClient()
    Session = make_session_factory()
    with Session() as session:
        fail_stale_processing_messages(session)
        session.commit()

    idle_seconds = float(env("WHATSAPP_QUEUE_POLL_SECONDS", "5"))
    send_interval = float(env("WHATSAPP_SEND_INTERVAL_SECONDS", "30"))
    jitter = float(env("WHATSAPP_SEND_JITTER_SECONDS", "10"))
    while True:
        try:
            status = client.status()
            if status.get("state") != "ready":
                time.sleep(idle_seconds)
                continue
            result = process_next_whatsapp_message(client=client, session_factory=Session)
            if result is None:
                time.sleep(idle_seconds)
            else:
                time.sleep(max(0, send_interval + random.uniform(0, jitter)))
        except WhatsAppServiceError:
            time.sleep(idle_seconds)
