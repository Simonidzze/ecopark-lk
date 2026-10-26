import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from .community import seed_community_defaults
from .config import env
from .db import make_session_factory
from .models import (
    BotSettings,
    NotificationDelivery,
    NotificationDestination,
    NotificationSource,
    NotificationSubscription,
    NotificationType,
    OutageEvent,
)
from .outages import (
    OutageClient,
    Source,
    event_fingerprint,
    format_message,
    grouped_payload,
    matches,
    outage_window_key,
)
from .telegram_bot import TelegramBotClient
from .utils import now_utc_naive


LOGGER = logging.getLogger(__name__)


@dataclass
class CheckResult:
    fetched: int = 0
    matched: int = 0
    discovered: int = 0
    sent: int = 0
    failed_sources: int = 0
    failed_messages: int = 0
    skipped_reason: str | None = None


def check_notification_type(
    session,
    notification_type_id,
    *,
    client_factory=OutageClient,
    sender=None,
):
    result = CheckResult()
    settings = session.get(BotSettings, 1)
    notification_type = session.get(NotificationType, notification_type_id)
    if settings is None or notification_type is None or not notification_type.enabled:
        result.skipped_reason = "Тип оповещения не найден или отключён"
        return result

    destination_ids = session.scalars(
        select(NotificationSubscription.destination_id).where(
            NotificationSubscription.notification_type_id == notification_type.id
        )
    ).all()
    destinations = session.scalars(
        select(NotificationDestination).where(
            NotificationDestination.id.in_(destination_ids),
            NotificationDestination.enabled.is_(True),
        )
    ).all() if destination_ids else []
    sources = session.scalars(
        select(NotificationSource).where(
            NotificationSource.notification_type_id == notification_type.id,
            NotificationSource.enabled.is_(True),
        ).order_by(NotificationSource.id)
    ).all()
    if not destinations:
        result.skipped_reason = "К типу не подключены активные каналы"
        return result
    if not sources:
        result.skipped_reason = "У типа нет активных источников"
        return result

    timeout = int(settings.request_timeout_seconds or env("TELEGRAM_REQUEST_TIMEOUT_SECONDS", "60"))
    outage_client = client_factory(timeout)
    sender = sender or TelegramBotClient(
        timeout=timeout,
        api_base_url=settings.telegram_api_base_url,
    )
    groups = {}
    now = now_utc_naive()

    for stored_source in sources:
        source = Source(stored_source.source_key, stored_source.name, stored_source.api_url)
        try:
            events = outage_client.fetch(source)
        except Exception:
            result.failed_sources += 1
            LOGGER.exception("Failed to read outage source %s", stored_source.source_key)
            continue
        result.fetched += len(events)
        relevant = [event for event in events if matches(event, notification_type.match_text)]
        result.matched += len(relevant)
        deliver_existing = not (
            stored_source.initialized_at is None and not settings.send_existing_on_first_run
        )
        for event in relevant:
            key = outage_window_key(stored_source.source_key, event)
            group = groups.setdefault(
                key, {"source_keys": set(), "match_count": 0, "create_deliveries": False}
            )
            group["source_keys"].add(stored_source.source_key)
            group["match_count"] += 1
            group["create_deliveries"] = group["create_deliveries"] or deliver_existing
        stored_source.initialized_at = now
        stored_source.updated_at = now

    for key, group in groups.items():
        payload = grouped_payload(key, group["source_keys"], group["match_count"])
        fingerprint = event_fingerprint(notification_type.source_key, payload)
        event = session.scalar(
            select(OutageEvent).where(
                OutageEvent.notification_type_id == notification_type.id,
                OutageEvent.fingerprint == fingerprint,
            )
        )
        if event is not None:
            event.last_seen_at = now
            event.payload = payload
            continue
        event = OutageEvent(
            notification_type_id=notification_type.id,
            fingerprint=fingerprint,
            payload=payload,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(event)
        session.flush()
        result.discovered += 1
        if group["create_deliveries"]:
            for destination in destinations:
                session.add(
                    NotificationDelivery(
                        event_id=event.id,
                        destination_id=destination.id,
                        status="pending",
                        attempts=0,
                        last_error=None,
                        sent_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )

    if all(source.initialized_at is not None for source in sources):
        notification_type.initialized_at = now
    notification_type.updated_at = now
    session.commit()

    destination_by_id = {item.id: item for item in destinations}
    deliveries = session.scalars(
        select(NotificationDelivery)
        .join(OutageEvent, OutageEvent.id == NotificationDelivery.event_id)
        .where(
            OutageEvent.notification_type_id == notification_type.id,
            NotificationDelivery.destination_id.in_(destination_by_id),
            NotificationDelivery.status.in_(("pending", "failed")),
        )
        .order_by(NotificationDelivery.destination_id, NotificationDelivery.id)
    ).all()
    for delivery in deliveries:
        event = session.get(OutageEvent, delivery.event_id)
        destination = destination_by_id[delivery.destination_id]
        try:
            sender.send_message(
                destination.chat_id,
                format_message(notification_type.name, event.payload),
                message_thread_id=destination.message_thread_id,
                parse_mode="HTML",
            )
        except Exception as exc:
            result.failed_messages += 1
            delivery.status = "failed"
            delivery.attempts += 1
            delivery.last_error = str(exc)[:2000]
            delivery.updated_at = now_utc_naive()
            session.commit()
            LOGGER.exception("Failed to send outage delivery %s", delivery.id)
            continue
        delivery.status = "sent"
        delivery.attempts += 1
        delivery.last_error = None
        delivery.sent_at = now_utc_naive()
        delivery.updated_at = delivery.sent_at
        session.commit()
        result.sent += 1
    return result


def run_due_checks_once(session_factory=None):
    Session = session_factory or make_session_factory()
    with Session() as session:
        seed_community_defaults(session)
        now = now_utc_naive()
        type_ids = session.scalars(
            select(NotificationType.id)
            .where(NotificationType.enabled.is_(True))
            .where(
                (NotificationType.force_check_requested.is_(True))
                | (NotificationType.next_check_at.is_(None))
                | (NotificationType.next_check_at <= now)
            )
            .order_by(NotificationType.id)
        ).all()
        results = []
        for type_id in type_ids:
            row = session.get(NotificationType, type_id)
            row.force_check_requested = False
            row.next_check_at = now + timedelta(
                seconds=max(10, row.poll_interval_seconds)
            )
            session.commit()
            result = check_notification_type(session, type_id)
            row = session.get(NotificationType, type_id)
            row.last_check_at = now_utc_naive()
            row.last_check_status = "warning" if result.failed_sources or result.failed_messages else "ok"
            row.last_check_message = (
                result.skipped_reason
                or f"Получено: {result.fetched}; совпадений: {result.matched}; "
                f"новых: {result.discovered}; доставлено: {result.sent}; "
                f"ошибок источников: {result.failed_sources}; ошибок отправки: {result.failed_messages}"
            )
            row.updated_at = now_utc_naive()
            session.commit()
            results.append(result)
        return results


def run_outage_worker():
    from .schema import ensure_community_schema

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_community_schema()
    delay = float(env("OUTAGE_WORKER_POLL_SECONDS", "5"))
    LOGGER.info("Outage worker started")
    while True:
        try:
            run_due_checks_once()
        except Exception:
            LOGGER.exception("Outage worker cycle failed")
        time.sleep(delay)
