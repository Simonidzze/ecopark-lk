import html
import logging
import re
from datetime import timedelta

from sqlalchemy import func, select

from .config import env
from .models import (
    BotSettings,
    CommunityMessage,
    CommunityUser,
    DirectoryEntry,
    NotificationDestination,
    NotificationSource,
    NotificationType,
    ReputationEvent,
)
from .outages import (
    DEFAULT_EMERGENCY_URL,
    DEFAULT_SHUTDOWN_URL,
    PUBLIC_OUTAGE_URL,
    normalize_text,
)
from .utils import now_utc_naive


LOGGER = logging.getLogger(__name__)
DEFAULT_TYPE_KEY = "electricity"
DEFAULT_TYPE_NAME = "В Экопарке не будет электричества"
DEFAULT_THANKS = "спасибо\nблагодарю\nспасибо за помощь\nвыручил\nвыручила"


def seed_community_defaults(session):
    now = now_utc_naive()
    settings = session.get(BotSettings, 1)
    if settings is None:
        settings = BotSettings(
            id=1,
            telegram_token=env("TELEGRAM_BOT_TOKEN", ""),
            telegram_api_base_url=env(
                "TELEGRAM_API_BASE_URL", "https://api.telegram.org/bot"
            ),
            request_timeout_seconds=int(env("TELEGRAM_REQUEST_TIMEOUT_SECONDS", "60")),
            send_existing_on_first_run=True,
            telegram_update_offset=0,
            telegram_link_admin_username="",
            updated_at=now,
        )
        session.add(settings)

    notification_type = session.scalar(
        select(NotificationType).where(NotificationType.source_key == DEFAULT_TYPE_KEY)
    )
    if notification_type is None:
        notification_type = NotificationType(
            source_key=DEFAULT_TYPE_KEY,
            name=DEFAULT_TYPE_NAME,
            api_url=PUBLIC_OUTAGE_URL,
            match_text="экопарк",
            poll_interval_seconds=3600,
            enabled=True,
            initialized_at=None,
            force_check_requested=False,
            next_check_at=now,
            last_check_at=None,
            last_check_status=None,
            last_check_message=None,
            created_at=now,
            updated_at=now,
        )
        session.add(notification_type)
        session.flush()

    defaults = (
        ("shutdown", "Плановые работы", DEFAULT_SHUTDOWN_URL),
        ("emergency", "Аварийные отключения", DEFAULT_EMERGENCY_URL),
    )
    existing = {
        row.source_key
        for row in session.scalars(select(NotificationSource)).all()
    }
    for source_key, name, api_url in defaults:
        if source_key in existing:
            continue
        session.add(
            NotificationSource(
                notification_type_id=notification_type.id,
                source_key=source_key,
                name=name,
                api_url=api_url,
                enabled=True,
                initialized_at=None,
                created_at=now,
                updated_at=now,
            )
        )
    session.commit()
    return settings


def _user_name(user):
    if isinstance(user, CommunityUser):
        first_name, last_name = user.first_name, user.last_name
        username, user_id = user.username, user.telegram_user_id
    else:
        first_name = str(user.get("first_name") or "")
        last_name = str(user.get("last_name") or "")
        username, user_id = user.get("username"), user.get("id")
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    return full_name or (f"@{username}" if username else f"Участник {user_id}")


def _mention(user):
    user_id = user.telegram_user_id if isinstance(user, CommunityUser) else int(user["id"])
    return f'<a href="tg://user?id={user_id}">{html.escape(_user_name(user))}</a>'


def _split_values(value):
    return [
        normalize_text(item)
        for item in re.split(r"[\n,;]+", value or "")
        if normalize_text(item)
    ]


def _contains_phrase(text, phrase):
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


class CommunityProcessor:
    def __init__(self, session, sender, bot_user_id):
        self.session = session
        self.sender = sender
        self.bot_user_id = int(bot_user_id)

    def process_update(self, update):
        if isinstance(update.get("message"), dict):
            self._process_message(update["message"])
        elif isinstance(update.get("message_reaction"), dict):
            self._process_reaction(update["message_reaction"])
        elif isinstance(update.get("callback_query"), dict):
            self._process_callback(update["callback_query"])

    def _find_destination(self, chat_id, message_thread_id=None):
        candidates = self.session.scalars(
            select(NotificationDestination)
            .where(
                NotificationDestination.chat_id == str(chat_id),
                NotificationDestination.enabled.is_(True),
            )
            .order_by(NotificationDestination.id)
        ).all()
        thread_id = int(message_thread_id) if message_thread_id is not None else None
        return next(
            (row for row in candidates if row.message_thread_id == thread_id), None
        )

    def _upsert_user(self, destination, user):
        telegram_user_id = int(user["id"])
        stored = self.session.scalar(
            select(CommunityUser).where(
                CommunityUser.destination_id == destination.id,
                CommunityUser.telegram_user_id == telegram_user_id,
            )
        )
        now = now_utc_naive()
        if stored is None:
            stored = CommunityUser(
                destination_id=destination.id,
                telegram_user_id=telegram_user_id,
                username=None,
                first_name="",
                last_name="",
                reputation=0,
                created_at=now,
                updated_at=now,
            )
            self.session.add(stored)
        stored.username = str(user.get("username") or "") or None
        stored.first_name = str(user.get("first_name") or "")
        stored.last_name = str(user.get("last_name") or "")
        stored.updated_at = now
        self.session.flush()
        return stored

    def _remember_message(self, destination, message, author):
        chat_id, message_id = str(message["chat"]["id"]), int(message["message_id"])
        exists = self.session.scalar(
            select(CommunityMessage.id).where(
                CommunityMessage.chat_id == chat_id,
                CommunityMessage.message_id == message_id,
            )
        )
        if exists is None:
            self.session.add(
                CommunityMessage(
                    destination_id=destination.id,
                    chat_id=chat_id,
                    message_id=message_id,
                    telegram_user_id=int(author["id"]),
                    message_thread_id=message.get("message_thread_id"),
                    created_at=now_utc_naive(),
                )
            )
            self.session.flush()

    def _process_message(self, message):
        chat = message.get("chat") or {}
        if chat.get("type") == "private":
            return
        destination = self._find_destination(chat.get("id"), message.get("message_thread_id"))
        author = message.get("from")
        if destination is None or not isinstance(author, dict) or author.get("is_bot"):
            return
        if destination.reputation_enabled:
            self._upsert_user(destination, author)
            self._remember_message(destination, message, author)
        text_value = str(message.get("text") or message.get("caption") or "").strip()
        if not text_value:
            return
        command, argument = self._parse_command(text_value)
        if command and self._process_command(destination, message, author, command, argument):
            return
        if destination.reputation_enabled:
            self._process_thanks(destination, message, author, text_value)
        if destination.directory_enabled and not text_value.startswith("/"):
            self._process_directory_query(destination, message, text_value)

    @staticmethod
    def _parse_command(text_value):
        if not text_value.startswith("/"):
            return None, ""
        token, _, argument = text_value.partition(" ")
        return token[1:].partition("@")[0].casefold(), argument.strip()

    def _process_command(self, destination, message, author, command, argument):
        if destination.reputation_enabled and command in {"rating", "rating_all", "rating_month"}:
            since, title = None, "Рейтинг добрых соседей"
            if command == "rating_month":
                now = now_utc_naive()
                since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                title = "Рейтинг за текущий месяц"
            self._send_rating(destination, message, title, since)
            return True
        if destination.reputation_enabled and command == "me":
            user = self._upsert_user(destination, author)
            self._safe_send(
                destination,
                f"⭐ {_mention(user)}, ваша репутация: <b>{user.reputation}</b>",
                reply_to_message_id=message["message_id"],
            )
            return True
        if destination.reputation_enabled and command == "thanks":
            reply = message.get("reply_to_message")
            if isinstance(reply, dict) and isinstance(reply.get("from"), dict):
                self._award_reputation(
                    destination, author, reply["from"], "thanks",
                    str(message["chat"]["id"]), int(message["message_id"]),
                    int(reply["message_id"]),
                )
            return True
        if destination.directory_enabled and command == "contacts":
            self._send_contact_menu(destination, message)
            return True
        if destination.directory_enabled and command == "phone":
            if argument:
                self._send_contact_matches(destination, message, argument)
            else:
                self._send_contact_menu(destination, message)
            return True
        return False

    def _process_thanks(self, destination, message, author, text_value):
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict) or not isinstance(reply.get("from"), dict):
            return
        normalized = normalize_text(text_value)
        if any(_contains_phrase(normalized, phrase) for phrase in _split_values(destination.thanks_phrases)):
            self._award_reputation(
                destination, author, reply["from"], "thanks",
                str(message["chat"]["id"]), int(message["message_id"]),
                int(reply["message_id"]),
            )

    def _process_reaction(self, reaction):
        giver = reaction.get("user")
        if not isinstance(giver, dict) or giver.get("is_bot"):
            return
        old, new = reaction.get("old_reaction") or [], reaction.get("new_reaction") or []
        if self._has_thumb(old) or not self._has_thumb(new):
            return
        chat_id, message_id = str((reaction.get("chat") or {}).get("id")), int(reaction["message_id"])
        stored = self.session.scalar(
            select(CommunityMessage).where(
                CommunityMessage.chat_id == chat_id,
                CommunityMessage.message_id == message_id,
            )
        )
        if stored is None:
            return
        destination = self.session.get(NotificationDestination, stored.destination_id)
        if destination is None or not destination.enabled or not destination.reputation_enabled:
            return
        receiver = self.session.scalar(
            select(CommunityUser).where(
                CommunityUser.destination_id == destination.id,
                CommunityUser.telegram_user_id == stored.telegram_user_id,
            )
        )
        if receiver is None:
            return
        self._award_reputation(
            destination, giver,
            {"id": receiver.telegram_user_id, "username": receiver.username,
             "first_name": receiver.first_name, "last_name": receiver.last_name},
            "reaction", chat_id, message_id, message_id,
        )

    @staticmethod
    def _has_thumb(reactions):
        return any(
            item.get("type") == "emoji" and item.get("emoji") == "👍"
            for item in reactions if isinstance(item, dict)
        )

    def _award_reputation(self, destination, giver, receiver, source_kind,
                          source_chat_id, source_message_id, reply_to_message_id):
        giver_id, receiver_id = int(giver["id"]), int(receiver["id"])
        if giver_id == receiver_id or receiver_id == self.bot_user_id or receiver.get("is_bot"):
            return False
        duplicate = self.session.scalar(
            select(ReputationEvent.id).where(
                ReputationEvent.destination_id == destination.id,
                ReputationEvent.giver_user_id == giver_id,
                ReputationEvent.source_kind == source_kind,
                ReputationEvent.source_chat_id == source_chat_id,
                ReputationEvent.source_message_id == source_message_id,
            )
        )
        if duplicate is not None:
            return False
        cutoff = now_utc_naive() - timedelta(hours=max(0, destination.reputation_cooldown_hours))
        recent = self.session.scalar(
            select(ReputationEvent.id).where(
                ReputationEvent.destination_id == destination.id,
                ReputationEvent.giver_user_id == giver_id,
                ReputationEvent.receiver_user_id == receiver_id,
                ReputationEvent.created_at >= cutoff,
            )
        )
        if recent is not None:
            return False
        self._upsert_user(destination, giver)
        receiver_user = self._upsert_user(destination, receiver)
        receiver_user.reputation += 1
        self.session.add(
            ReputationEvent(
                destination_id=destination.id,
                giver_user_id=giver_id,
                receiver_user_id=receiver_id,
                points=1,
                source_kind=source_kind,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                created_at=now_utc_naive(),
            )
        )
        self.session.flush()
        self._safe_send(
            destination,
            f"🙏 {_mention(giver)} благодарит {_mention(receiver_user)}\n⭐ Репутация: <b>{receiver_user.reputation}</b>",
            reply_to_message_id=reply_to_message_id,
        )
        return True

    def _send_rating(self, destination, message, title, since):
        if since is None:
            users = self.session.scalars(
                select(CommunityUser)
                .where(CommunityUser.destination_id == destination.id, CommunityUser.reputation > 0)
                .order_by(CommunityUser.reputation.desc(), CommunityUser.id).limit(10)
            ).all()
            rows = [(user, user.reputation) for user in users]
        else:
            rows = self.session.execute(
                select(CommunityUser, func.sum(ReputationEvent.points).label("points"))
                .join(ReputationEvent, ReputationEvent.receiver_user_id == CommunityUser.telegram_user_id)
                .where(
                    CommunityUser.destination_id == destination.id,
                    ReputationEvent.destination_id == destination.id,
                    ReputationEvent.created_at >= since,
                )
                .group_by(CommunityUser.id).order_by(func.sum(ReputationEvent.points).desc()).limit(10)
            ).all()
        body = "Пока никто не получил благодарностей." if not rows else "\n".join(
            f"{index}. {_mention(user)} — <b>{points}</b>"
            for index, (user, points) in enumerate(rows, start=1)
        )
        self._safe_send(destination, f"🏆 <b>{html.escape(title)}</b>\n\n{body}",
                        reply_to_message_id=message["message_id"])

    def _enabled_entries(self, destination):
        return self.session.scalars(
            select(DirectoryEntry).where(
                DirectoryEntry.destination_id == destination.id,
                DirectoryEntry.enabled.is_(True),
            ).order_by(DirectoryEntry.position, DirectoryEntry.id)
        ).all()

    def _process_directory_query(self, destination, message, text_value):
        normalized, found = normalize_text(text_value), []
        for entry in self._enabled_entries(destination):
            phrases = [normalize_text(entry.name), *_split_values(entry.keywords)]
            if any(phrase and _contains_phrase(normalized, phrase) for phrase in phrases):
                found.append(entry)
        if found:
            self._send_entries(destination, message, found)

    def _send_contact_matches(self, destination, message, query):
        normalized = normalize_text(query)
        found = [
            entry for entry in self._enabled_entries(destination)
            if normalized in normalize_text(" ".join((entry.name, entry.category, entry.keywords or "")))
        ]
        if not found:
            self._safe_send(destination, "☎️ Контакт не найден. Используйте /contacts.",
                            reply_to_message_id=message["message_id"])
        else:
            self._send_entries(destination, message, found)

    def _send_entries(self, destination, message, entries):
        if len(entries) == 1:
            self._safe_send(destination, self._format_entry(entries[0]),
                            reply_to_message_id=message["message_id"])
        else:
            self._send_contact_menu(destination, message, entries)

    def _send_contact_menu(self, destination, message, entries=None):
        entries = entries if entries is not None else self._enabled_entries(destination)
        if not entries:
            self._safe_send(destination, "☎️ Справочник полезных контактов пока пуст.",
                            reply_to_message_id=message["message_id"])
            return
        keyboard = {"inline_keyboard": [[{"text": item.name[:64], "callback_data": f"directory:{item.id}"}] for item in entries[:30]]}
        self._safe_send(destination, "☎️ <b>Полезные контакты</b>\n\nВыберите нужную службу:",
                        reply_to_message_id=message["message_id"], reply_markup=keyboard)

    def _process_callback(self, callback):
        callback_id = str(callback.get("id") or "")
        try:
            data = str(callback.get("data") or "")
            if not data.startswith("directory:"):
                return
            try:
                entry_id = int(data.partition(":")[2])
            except ValueError:
                return
            message, chat = callback.get("message") or {}, (callback.get("message") or {}).get("chat") or {}
            destination = self._find_destination(chat.get("id"), message.get("message_thread_id"))
            entry = self.session.get(DirectoryEntry, entry_id)
            if destination is None or not destination.directory_enabled or entry is None or not entry.enabled or entry.destination_id != destination.id:
                return
            self._safe_send(destination, self._format_entry(entry), reply_to_message_id=message.get("message_id"))
        finally:
            if callback_id:
                self.sender.answer_callback_query(callback_id)

    @staticmethod
    def _format_entry(entry):
        lines = [f"☎️ <b>{html.escape(entry.name)}</b>", f"Категория: {html.escape(entry.category)}", f"Телефон: <code>{html.escape(entry.phone)}</code>"]
        if entry.description:
            lines.extend(("", html.escape(entry.description)))
        if entry.url:
            lines.extend(("", f'<a href="{html.escape(entry.url, quote=True)}">Открыть ссылку</a>'))
        return "\n".join(lines)

    def _safe_send(self, destination, text_value, *, reply_to_message_id=None, reply_markup=None):
        try:
            self.sender.send_message(
                destination.chat_id,
                text_value,
                reply_markup,
                message_thread_id=destination.message_thread_id,
                reply_to_message_id=reply_to_message_id,
                parse_mode="HTML",
            )
        except Exception:
            LOGGER.exception("Failed to send community response to destination %s", destination.id)
