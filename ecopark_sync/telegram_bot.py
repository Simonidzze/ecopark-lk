import logging
import time
from decimal import Decimal

import requests
from sqlalchemy import select

from .calls import normalize_phone
from .config import env
from .db import make_session_factory
from .models import (
    Balance,
    OwnerPlot,
    TelegramBinding,
    TelegramLinkRequest,
    TelegramProfile,
)
from .utils import now_utc_naive


LOGGER = logging.getLogger(__name__)
DEBT_BUTTON = "Узнать задолженность"
MENU_MARKUP = {
    "keyboard": [[{"text": DEBT_BUTTON}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}
PHONE_MARKUP = {
    "keyboard": [[{"text": "Отправить мой телефон", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}


class TelegramAPIError(RuntimeError):
    pass


class TelegramBotClient:
    def __init__(self, token=None, timeout=None, session=None):
        self.token = token or env("TELEGRAM_BOT_TOKEN", required=True)
        self.timeout = float(timeout or env("TELEGRAM_REQUEST_TIMEOUT_SECONDS", "60"))
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.session = session or requests.Session()

    def call(self, method, payload=None, timeout=None):
        try:
            response = self.session.post(
                f"{self.base_url}/{method}",
                json=payload or {},
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            # Request exception messages can contain the Bot API URL and token.
            raise TelegramAPIError(
                f"Telegram API недоступен ({type(exc).__name__})"
            ) from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise TelegramAPIError(
                f"Telegram API вернул некорректный ответ (HTTP {response.status_code})"
            ) from exc
        if not response.ok:
            raise TelegramAPIError(
                result.get("description") or f"Ошибка Telegram API: HTTP {response.status_code}"
            )
        if not result.get("ok"):
            raise TelegramAPIError(result.get("description") or "Ошибка Telegram API")
        return result.get("result")

    def configure(self):
        self.call("deleteWebhook", {"drop_pending_updates": False})
        self.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Открыть меню"},
                    {"command": "debt", "description": DEBT_BUTTON},
                ]
            },
        )

    def get_updates(self, offset=None, poll_timeout=30):
        payload = {
            "timeout": poll_timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=poll_timeout + self.timeout)

    def send_message(self, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)


def money(value):
    amount = Decimal(value or 0)
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def get_or_create_profile(session, message):
    sender = message.get("from") or {}
    telegram_user_id = str(sender.get("id") or "")
    chat_id = str((message.get("chat") or {}).get("id") or telegram_user_id)
    if not telegram_user_id:
        return None

    now = now_utc_naive()
    profile = session.get(TelegramProfile, telegram_user_id)
    if profile is None:
        profile = TelegramProfile(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            username=str(sender.get("username") or ""),
            first_name=str(sender.get("first_name") or ""),
            last_name=str(sender.get("last_name") or ""),
            state="idle",
            draft_full_name="",
            draft_plot_number="",
            draft_phone="",
            created_at=now,
            updated_at=now,
        )
        session.add(profile)
    else:
        profile.chat_id = chat_id
        profile.username = str(sender.get("username") or "")
        profile.first_name = str(sender.get("first_name") or "")
        profile.last_name = str(sender.get("last_name") or "")
        profile.updated_at = now
    return profile


def active_binding(session, telegram_user_id):
    binding = session.get(TelegramBinding, telegram_user_id)
    return binding if binding is not None and binding.enabled else None


def pending_request(session, telegram_user_id):
    return session.scalar(
        select(TelegramLinkRequest)
        .where(TelegramLinkRequest.telegram_user_id == telegram_user_id)
        .where(TelegramLinkRequest.status == "pending")
        .order_by(TelegramLinkRequest.created_at.desc(), TelegramLinkRequest.id.desc())
        .limit(1)
    )


def start_registration(profile):
    profile.state = "awaiting_full_name"
    profile.draft_full_name = ""
    profile.draft_plot_number = ""
    profile.draft_phone = ""


def debt_message(session, telegram_user_id):
    binding = active_binding(session, telegram_user_id)
    if binding is None:
        if pending_request(session, telegram_user_id) is not None:
            return "Ваша заявка на привязку ещё проверяется администратором."
        return "Telegram пока не привязан к владельцу. Отправьте /start, чтобы подать заявку."

    rows = session.execute(
        select(OwnerPlot, Balance)
        .outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
        .where(OwnerPlot.owner_id == binding.owner_id)
        .order_by(OwnerPlot.plot_number, OwnerPlot.id)
    ).all()
    if not rows:
        return "Привязка найдена, но участки владельца отсутствуют в последней выгрузке."

    latest_sync = max(
        (balance.synced_at for _owner_plot, balance in rows if balance is not None),
        default=None,
    )
    lines = [f"{binding.owner}", ""]
    if latest_sync is not None:
        lines.append(f"Данные на {latest_sync.strftime('%d.%m.%Y %H:%M')}")
        lines.append("")

    grand_total = Decimal("0")
    for owner_plot, balance in rows:
        debt = Decimal(balance.debt or 0) if balance else Decimal("0")
        penalty = Decimal(balance.penalty or 0) if balance else Decimal("0")
        overpayment = Decimal(balance.overpayment or 0) if balance else Decimal("0")
        total = Decimal(balance.total or 0) if balance else Decimal("0")
        grand_total += total
        lines.extend(
            [
                f"Участок № {owner_plot.plot_number}",
                f"Задолженность: {money(debt)}",
                f"Пени: {money(penalty)}",
                f"Переплата: {money(overpayment)}",
                f"Итого к оплате: {money(total)}",
                "",
            ]
        )
    if len(rows) > 1:
        lines.append(f"Всего по участкам: {money(grand_total)}")
    return "\n".join(lines).strip()


def submit_registration(session, profile, phone):
    now = now_utc_naive()
    request_row = TelegramLinkRequest(
        telegram_user_id=profile.telegram_user_id,
        chat_id=profile.chat_id,
        username=profile.username,
        full_name=profile.draft_full_name,
        plot_number=profile.draft_plot_number,
        phone=phone,
        status="pending",
        owner_id=None,
        owner_plot_id=None,
        review_comment="",
        created_at=now,
        reviewed_at=None,
    )
    session.add(request_row)
    profile.draft_phone = phone
    profile.state = "idle"
    profile.updated_at = now
    return request_row


def handle_update(update, client, session_factory=None):
    message = update.get("message") or {}
    if not message or (message.get("chat") or {}).get("type") not in {None, "private"}:
        return False

    text = str(message.get("text") or "").strip()
    Session = session_factory or make_session_factory()
    with Session() as session:
        profile = get_or_create_profile(session, message)
        if profile is None:
            return False
        session.flush()

        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        binding = active_binding(session, profile.telegram_user_id)

        if command == "/cancel":
            profile.state = "idle"
            profile.draft_full_name = ""
            profile.draft_plot_number = ""
            profile.draft_phone = ""
            session.commit()
            client.send_message(profile.chat_id, "Заполнение заявки отменено.", MENU_MARKUP)
            return True

        if command == "/start":
            if binding is not None:
                session.commit()
                client.send_message(
                    profile.chat_id,
                    f"Здравствуйте, {profile.first_name or binding.owner}! Выберите действие.",
                    MENU_MARKUP,
                )
                return True
            if pending_request(session, profile.telegram_user_id) is not None:
                profile.state = "idle"
                session.commit()
                client.send_message(
                    profile.chat_id,
                    "Ваша заявка уже отправлена и ожидает проверки администратора.",
                    MENU_MARKUP,
                )
                return True
            start_registration(profile)
            session.commit()
            client.send_message(
                profile.chat_id,
                "Для привязки к данным ТСН напишите ваши ФИО полностью.",
                REMOVE_KEYBOARD,
            )
            return True

        if command == "/debt" or text.casefold() == DEBT_BUTTON.casefold():
            response_text = debt_message(session, profile.telegram_user_id)
            session.commit()
            client.send_message(profile.chat_id, response_text, MENU_MARKUP)
            return True

        if profile.state == "awaiting_full_name":
            if len(text) < 5 or len(text) > 255:
                session.commit()
                client.send_message(profile.chat_id, "Укажите ФИО полностью, одной строкой.")
                return True
            profile.draft_full_name = text
            profile.state = "awaiting_plot_number"
            session.commit()
            client.send_message(profile.chat_id, "Теперь укажите номер участка.")
            return True

        if profile.state == "awaiting_plot_number":
            if not text or len(text) > 64:
                session.commit()
                client.send_message(profile.chat_id, "Укажите номер участка.")
                return True
            profile.draft_plot_number = text
            profile.state = "awaiting_phone"
            session.commit()
            client.send_message(
                profile.chat_id,
                "Отправьте ваш номер телефона кнопкой ниже или напишите его сообщением.",
                PHONE_MARKUP,
            )
            return True

        if profile.state == "awaiting_phone":
            contact = message.get("contact") or {}
            contact_user_id = str(contact.get("user_id") or "")
            if contact_user_id and contact_user_id != profile.telegram_user_id:
                session.commit()
                client.send_message(profile.chat_id, "Пожалуйста, отправьте именно свой номер телефона.")
                return True
            phone = normalize_phone(str(contact.get("phone_number") or text))
            if not 10 <= len(phone) <= 15:
                session.commit()
                client.send_message(profile.chat_id, "Не удалось распознать телефон. Укажите номер с кодом страны.")
                return True
            request_row = submit_registration(session, profile, phone)
            session.commit()
            client.send_message(
                profile.chat_id,
                "Заявка отправлена администратору.\n\n"
                f"ФИО: {request_row.full_name}\n"
                f"Участок: {request_row.plot_number}\n"
                f"Телефон: +{request_row.phone}\n\n"
                "После проверки вы сможете узнать задолженность в меню.",
                MENU_MARKUP,
            )
            return True

        session.commit()
        if binding is not None:
            client.send_message(profile.chat_id, "Выберите действие в меню.", MENU_MARKUP)
        elif pending_request(session, profile.telegram_user_id) is not None:
            client.send_message(
                profile.chat_id,
                "Ваша заявка ожидает проверки администратора.",
                MENU_MARKUP,
            )
        else:
            client.send_message(
                profile.chat_id,
                "Отправьте /start, чтобы начать привязку к данным ТСН.",
                MENU_MARKUP,
            )
        return True


def run_telegram_bot(client=None, session_factory=None):
    from .schema import ensure_telegram_schema

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_telegram_schema()
    client = client or TelegramBotClient()
    client.configure()
    offset = None
    poll_timeout = int(env("TELEGRAM_POLL_TIMEOUT_SECONDS", "30"))
    retry_delay = float(env("TELEGRAM_RETRY_DELAY_SECONDS", "5"))
    LOGGER.info("Telegram bot started")

    while True:
        try:
            updates = client.get_updates(offset=offset, poll_timeout=poll_timeout)
            for update in updates:
                handle_update(update, client, session_factory=session_factory)
                offset = int(update["update_id"]) + 1
        except TelegramAPIError:
            LOGGER.exception("Telegram API error")
            time.sleep(retry_delay)
        except Exception:
            LOGGER.exception("Telegram bot update failed")
            time.sleep(retry_delay)
