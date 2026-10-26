import re

from sqlalchemy import select

from .calls import normalize_phone
from .expense_allocations import normalize_text
from .models import (
    BotSettings,
    OwnerPlot,
    TelegramBinding,
    TelegramLinkRequest,
    TelegramProfile,
)
from .utils import now_utc_naive


ADMIN_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_telegram_username(value):
    return str(value or "").strip().lstrip("@").casefold()


def validate_telegram_username(value):
    username = normalize_telegram_username(value)
    if username and not ADMIN_USERNAME_RE.fullmatch(username):
        raise ValueError("Укажите корректный Telegram-логин без ссылки")
    return username


def telegram_link_admin_username(session):
    settings = session.get(BotSettings, 1)
    return normalize_telegram_username(
        settings.telegram_link_admin_username if settings is not None else ""
    )


def is_telegram_link_admin(session, telegram_user):
    configured = telegram_link_admin_username(session)
    actual = normalize_telegram_username((telegram_user or {}).get("username"))
    return bool(configured and actual and configured == actual)


def find_telegram_link_admin_profile(session):
    configured = telegram_link_admin_username(session)
    if not configured:
        return None
    return next(
        (
            profile
            for profile in session.scalars(select(TelegramProfile)).all()
            if normalize_telegram_username(profile.username) == configured
        ),
        None,
    )


def owner_plot_match_score(link_request, owner_plot):
    score = 0
    request_plot = normalize_text(link_request.plot_number)
    request_name = normalize_text(link_request.full_name)
    request_phone = normalize_phone(link_request.phone)
    if request_plot and request_plot == normalize_text(owner_plot.plot_number):
        score += 5
    owner_name = normalize_text(owner_plot.owner)
    if request_name and request_name == owner_name:
        score += 5
    elif request_name and (request_name in owner_name or owner_name in request_name):
        score += 2
    owner_phone = normalize_phone(owner_plot.phone)
    if request_phone and owner_phone and request_phone == owner_phone:
        score += 4
    return score


def suggested_owner_plot(session, link_request):
    candidates = session.scalars(
        select(OwnerPlot).order_by(OwnerPlot.owner, OwnerPlot.plot_number, OwnerPlot.id)
    ).all()
    ranked = sorted(
        ((owner_plot_match_score(link_request, row), row) for row in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 5:
        return None
    return ranked[0][1]


def approve_telegram_link_request(session, link_request, owner_plot):
    now = now_utc_naive()
    binding = session.get(TelegramBinding, link_request.telegram_user_id)
    if binding is None:
        binding = TelegramBinding(
            telegram_user_id=link_request.telegram_user_id,
            chat_id=link_request.chat_id,
            owner_id=owner_plot.owner_id,
            owner=owner_plot.owner,
            request_id=link_request.id,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add(binding)
    else:
        binding.chat_id = link_request.chat_id
        binding.owner_id = owner_plot.owner_id
        binding.owner = owner_plot.owner
        binding.request_id = link_request.id
        binding.enabled = True
        binding.updated_at = now

    link_request.status = "approved"
    link_request.owner_id = owner_plot.owner_id
    link_request.owner_plot_id = owner_plot.id
    link_request.reviewed_at = now
    profile = session.get(TelegramProfile, link_request.telegram_user_id)
    if profile is not None:
        profile.state = "idle"
        profile.updated_at = now
    return binding


def reject_telegram_link_request(session, link_request, comment=""):
    now = now_utc_naive()
    link_request.status = "rejected"
    link_request.review_comment = str(comment or "").strip()[:4000]
    link_request.reviewed_at = now
    profile = session.get(TelegramProfile, link_request.telegram_user_id)
    if profile is not None:
        profile.state = "idle"
        profile.updated_at = now


def telegram_link_admin_notification(session, link_request):
    admin = find_telegram_link_admin_profile(session)
    if admin is None:
        return None
    owner_plot = suggested_owner_plot(session, link_request)
    telegram_name = (
        f"@{link_request.username}"
        if link_request.username
        else f"ID {link_request.telegram_user_id}"
    )
    lines = [
        "Новая заявка на привязку Telegram",
        "",
        f"Telegram: {telegram_name}",
        f"ФИО: {link_request.full_name}",
        f"Участок: {link_request.plot_number}",
        f"Телефон: +{link_request.phone}",
    ]
    keyboard = []
    if owner_plot is not None:
        lines.extend(
            [
                "",
                f"Найдено в 1С: {owner_plot.owner}",
                f"Участок в 1С: {owner_plot.plot_number}",
                "При одобрении будет доступ ко всем участкам владельца.",
            ]
        )
        keyboard.append(
            [
                {
                    "text": "✅ Одобрить",
                    "callback_data": f"telegram-link:approve:{link_request.id}",
                }
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Подходящий владелец автоматически не найден. Одобрить заявку можно в ЛК.",
            ]
        )
    keyboard.append(
        [
            {
                "text": "❌ Отклонить",
                "callback_data": f"telegram-link:reject:{link_request.id}",
            }
        ]
    )
    return admin.chat_id, "\n".join(lines), {"inline_keyboard": keyboard}
