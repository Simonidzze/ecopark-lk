import logging
import random
import re
import time
from datetime import timedelta
from decimal import Decimal
from string import Formatter
import uuid

from sqlalchemy import func, select, update

from .client import fetch_receipt_pdf
from .config import env
from .db import make_session_factory
from .models import Balance, OwnerPlot, TelegramBinding, TelegramReceiptDelivery
from .telegram_bot import TelegramBotClient
from .utils import now_utc_naive


LOGGER = logging.getLogger(__name__)
DEFAULT_RECEIPT_MESSAGE = (
    "Здравствуйте, {owner}!\n\n"
    "Направляем квитанцию по участку № {plot_number}. "
    "Текущая сумма к оплате: {total}.\n\n"
    "Если оплата уже произведена, пожалуйста, не учитывайте это сообщение."
)
ALLOWED_RECEIPT_FIELDS = {"owner", "plot_number", "total", "date"}


def format_money(value):
    amount = Decimal(value or 0)
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def format_receipt_message(template, **values):
    text = str(template or DEFAULT_RECEIPT_MESSAGE).strip()
    try:
        for _literal, field_name, format_spec, conversion in Formatter().parse(text):
            if field_name and field_name not in ALLOWED_RECEIPT_FIELDS:
                raise ValueError(
                    f"Неизвестное поле: {{{field_name}}}. "
                    "Доступны: {owner}, {plot_number}, {total}, {date}"
                )
            if format_spec or conversion:
                raise ValueError("Форматирование полей не поддерживается")
        rendered = text.format_map(values)
    except KeyError as exc:
        raise ValueError(f"Не задано значение поля: {exc}") from exc
    if not rendered:
        raise ValueError("Текст сообщения не может быть пустым")
    if len(rendered) > 1024:
        raise ValueError("Подпись к квитанции не должна превышать 1024 символа")
    return rendered


def receipt_filename(plot_number):
    safe_plot = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", str(plot_number or "")).strip("-")
    return f"Квитанция-участок-{safe_plot or 'без-номера'}.pdf"


def telegram_receipt_queue_stats(session):
    return dict(
        session.execute(
            select(TelegramReceiptDelivery.status, func.count(TelegramReceiptDelivery.id))
            .group_by(TelegramReceiptDelivery.status)
        ).all()
    )


def enqueue_user_receipts(session, binding):
    if not isinstance(binding, TelegramBinding) or not binding.enabled:
        return 0, 0
    rows = session.execute(
        select(OwnerPlot, Balance)
        .outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
        .where(OwnerPlot.owner_id == binding.owner_id)
        .order_by(OwnerPlot.plot_number, OwnerPlot.id)
    ).all()
    existing_plot_ids = set(
        session.scalars(
            select(TelegramReceiptDelivery.owner_plot_id).where(
                TelegramReceiptDelivery.telegram_user_id == binding.telegram_user_id,
                TelegramReceiptDelivery.status.in_(("queued", "processing")),
            )
        ).all()
    )
    batch_id = f"request-{now_utc_naive().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    queued = 0
    skipped = 0
    for owner_plot, balance in rows:
        if owner_plot.id in existing_plot_ids:
            skipped += 1
            continue
        total = balance.total if balance is not None else 0
        data_date = balance.synced_at if balance is not None else owner_plot.synced_at
        session.add(
            TelegramReceiptDelivery(
                batch_id=batch_id,
                telegram_user_id=binding.telegram_user_id,
                chat_id=binding.chat_id,
                owner_plot_id=owner_plot.id,
                owner_name=owner_plot.owner,
                plot_number=owner_plot.plot_number,
                message_text=format_receipt_message(
                    DEFAULT_RECEIPT_MESSAGE,
                    owner=owner_plot.owner,
                    plot_number=owner_plot.plot_number,
                    total=format_money(total),
                    date=data_date.strftime("%d.%m.%Y"),
                ),
                filename=receipt_filename(owner_plot.plot_number),
                status="queued",
                attempts=0,
                created_at=now_utc_naive(),
            )
        )
        queued += 1
    return queued, skipped


def fail_stale_receipt_deliveries(session, minutes=30):
    cutoff = now_utc_naive() - timedelta(minutes=minutes)
    result = session.execute(
        update(TelegramReceiptDelivery)
        .where(TelegramReceiptDelivery.status == "processing")
        .where(TelegramReceiptDelivery.started_at < cutoff)
        .values(
            status="failed",
            error_text=(
                "Отправка была прервана. Перед повтором проверьте Telegram, "
                "чтобы не отправить квитанцию дважды."
            ),
        )
    )
    return result.rowcount


def process_next_telegram_receipt(client=None, session_factory=None, receipt_loader=None):
    client = client or TelegramBotClient()
    receipt_loader = receipt_loader or fetch_receipt_pdf
    Session = session_factory or make_session_factory()
    with Session() as session:
        delivery = session.scalar(
            select(TelegramReceiptDelivery)
            .where(TelegramReceiptDelivery.status == "queued")
            .order_by(TelegramReceiptDelivery.created_at, TelegramReceiptDelivery.id)
            .limit(1)
        )
        if delivery is None:
            return None
        delivery.status = "processing"
        delivery.started_at = now_utc_naive()
        delivery.attempts += 1
        delivery_id = delivery.id
        chat_id = delivery.chat_id
        owner_plot_id = delivery.owner_plot_id
        filename = delivery.filename
        caption = delivery.message_text
        session.commit()

    try:
        document = receipt_loader(owner_plot_id)
        result = client.send_document(chat_id, filename, document, caption)
    except Exception as exc:
        with Session() as session:
            delivery = session.get(TelegramReceiptDelivery, delivery_id)
            if delivery is not None:
                max_attempts = int(env("TELEGRAM_RECEIPT_MAX_ATTEMPTS", "2"))
                delivery.status = "queued" if delivery.attempts < max_attempts else "failed"
                delivery.error_text = str(exc)[:4000]
                session.commit()
                return "retry" if delivery.status == "queued" else "failed"
        return "failed"

    with Session() as session:
        delivery = session.get(TelegramReceiptDelivery, delivery_id)
        if delivery is not None:
            delivery.status = "sent"
            delivery.sent_at = now_utc_naive()
            delivery.external_message_id = str((result or {}).get("message_id") or "")[:255] or None
            delivery.error_text = None
            session.commit()
    return "sent"


def run_telegram_receipt_worker():
    from .schema import ensure_telegram_schema

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_telegram_schema()
    client = TelegramBotClient()
    Session = make_session_factory()
    with Session() as session:
        fail_stale_receipt_deliveries(session)
        session.commit()

    idle_seconds = float(env("TELEGRAM_RECEIPT_QUEUE_POLL_SECONDS", "5"))
    send_interval = float(env("TELEGRAM_RECEIPT_SEND_INTERVAL_SECONDS", "1"))
    jitter = float(env("TELEGRAM_RECEIPT_SEND_JITTER_SECONDS", "1"))
    LOGGER.info("Telegram receipt worker started")
    while True:
        try:
            result = process_next_telegram_receipt(client=client, session_factory=Session)
            if result is None:
                time.sleep(idle_seconds)
            else:
                time.sleep(max(0, send_interval + random.uniform(0, jitter)))
        except Exception:
            LOGGER.exception("Telegram receipt worker failed")
            time.sleep(idle_seconds)
