from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .config import env
from .db import make_session_factory
from .models import (
    BotSettings,
    DirectoryEntry,
    NotificationDelivery,
    NotificationDestination,
    NotificationSource,
    NotificationSubscription,
    NotificationType,
    OutageEvent,
)
from .telegram_bot import TelegramBotClient
from .utils import now_utc_naive


def _positive(value, label, minimum=1):
    try:
        result = int(str(value or "").strip())
    except ValueError as exc:
        raise ValueError(f"{label}: требуется целое число") from exc
    if result < minimum:
        raise ValueError(f"{label}: минимальное значение — {minimum}")
    return result


def _optional_positive(value, label):
    if not str(value or "").strip():
        return None
    return _positive(value, label)


def register_community_routes(app):
    from flask import abort, flash, redirect, render_template, request, url_for

    @app.get("/admin/community")
    def community_admin():
        Session = make_session_factory()
        with Session() as session:
            settings = session.get(BotSettings, 1)
            notification_types = session.scalars(
                select(NotificationType).order_by(NotificationType.id)
            ).all()
            sources = session.scalars(
                select(NotificationSource).order_by(NotificationSource.id)
            ).all()
            destinations = session.scalars(
                select(NotificationDestination).order_by(NotificationDestination.id)
            ).all()
            subscriptions = session.execute(
                select(NotificationSubscription.notification_type_id,
                       NotificationSubscription.destination_id)
            ).all()
            entries = session.scalars(
                select(DirectoryEntry).order_by(
                    DirectoryEntry.destination_id, DirectoryEntry.position, DirectoryEntry.id
                )
            ).all()
            stats = {
                "events": session.scalar(select(func.count(OutageEvent.id))) or 0,
                "sent": session.scalar(select(func.count(NotificationDelivery.id)).where(NotificationDelivery.status == "sent")) or 0,
                "failed": session.scalar(select(func.count(NotificationDelivery.id)).where(NotificationDelivery.status == "failed")) or 0,
                "pending": session.scalar(select(func.count(NotificationDelivery.id)).where(NotificationDelivery.status == "pending")) or 0,
            }
            deliveries = session.execute(
                select(NotificationDelivery, OutageEvent, NotificationDestination)
                .join(OutageEvent, OutageEvent.id == NotificationDelivery.event_id)
                .join(NotificationDestination, NotificationDestination.id == NotificationDelivery.destination_id)
                .order_by(NotificationDelivery.id.desc()).limit(30)
            ).all()

        source_by_type = {}
        for row in sources:
            source_by_type.setdefault(row.notification_type_id, []).append(row)
        destination_ids_by_type = {}
        for type_id, destination_id in subscriptions:
            destination_ids_by_type.setdefault(type_id, set()).add(destination_id)
        entries_by_destination = {}
        for entry in entries:
            entries_by_destination.setdefault(entry.destination_id, []).append(entry)
        return render_template(
            "community.html",
            settings=settings,
            notification_types=notification_types,
            sources_by_type=source_by_type,
            destinations=destinations,
            destination_ids_by_type=destination_ids_by_type,
            entries_by_destination=entries_by_destination,
            stats=stats,
            deliveries=deliveries,
            token_configured=bool(env("TELEGRAM_BOT_TOKEN", "")),
        )

    @app.post("/admin/community/settings")
    def community_settings():
        Session = make_session_factory()
        try:
            with Session() as session:
                settings = session.get(BotSettings, 1)
                settings.request_timeout_seconds = _positive(
                    request.form.get("request_timeout_seconds"), "Таймаут"
                )
                settings.send_existing_on_first_run = request.form.get("send_existing_on_first_run") == "1"
                settings.updated_at = now_utc_naive()
                session.commit()
            flash("Настройки сохранены", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("community_admin"))

    @app.post("/admin/community/types/<int:type_id>")
    def community_type_update(type_id):
        Session = make_session_factory()
        try:
            with Session() as session:
                row = session.get(NotificationType, type_id)
                if row is None:
                    abort(404)
                name = request.form.get("name", "").strip()
                match_text = request.form.get("match_text", "").strip()
                if not name or not match_text:
                    raise ValueError("Название и строка поиска обязательны")
                selected_ids = {int(value) for value in request.form.getlist("destination_ids")}
                session.execute(delete(NotificationSubscription).where(NotificationSubscription.notification_type_id == type_id))
                now = now_utc_naive()
                for destination_id in selected_ids:
                    if session.get(NotificationDestination, destination_id) is not None:
                        session.add(NotificationSubscription(
                            notification_type_id=type_id,
                            destination_id=destination_id,
                            created_at=now,
                        ))
                row.name = name
                row.match_text = match_text
                row.poll_interval_seconds = _positive(
                    request.form.get("poll_interval_seconds"), "Интервал", 10
                )
                row.enabled = request.form.get("enabled") == "1"
                row.next_check_at = now
                row.updated_at = now
                session.commit()
            flash("Тип оповещения сохранён", "success")
        except (TypeError, ValueError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("community_admin") + "#types")

    @app.post("/admin/community/destinations")
    def community_destination_add():
        Session = make_session_factory()
        try:
            with Session() as session:
                name, chat_id = request.form.get("name", "").strip(), request.form.get("chat_id", "").strip()
                if not name or not chat_id:
                    raise ValueError("Укажите название и Telegram chat ID")
                now = now_utc_naive()
                session.add(NotificationDestination(
                    name=name,
                    chat_id=chat_id,
                    message_thread_id=_optional_positive(request.form.get("message_thread_id"), "ID топика"),
                    reputation_enabled=False,
                    directory_enabled=False,
                    reputation_cooldown_hours=24,
                    thanks_phrases="спасибо\nблагодарю\nспасибо за помощь\nвыручил\nвыручила",
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                ))
                session.commit()
            flash("Канал добавлен", "success")
        except (IntegrityError, ValueError) as exc:
            flash("Этот чат и топик уже добавлены" if isinstance(exc, IntegrityError) else str(exc), "error")
        return redirect(url_for("community_admin") + "#destinations")

    @app.post("/admin/community/destinations/<int:destination_id>")
    def community_destination_update(destination_id):
        Session = make_session_factory()
        try:
            with Session() as session:
                row = session.get(NotificationDestination, destination_id)
                if row is None:
                    abort(404)
                name, chat_id = request.form.get("name", "").strip(), request.form.get("chat_id", "").strip()
                if not name or not chat_id:
                    raise ValueError("Укажите название и Telegram chat ID")
                row.name, row.chat_id = name, chat_id
                row.message_thread_id = _optional_positive(request.form.get("message_thread_id"), "ID топика")
                row.enabled = request.form.get("enabled") == "1"
                row.reputation_enabled = request.form.get("reputation_enabled") == "1"
                row.directory_enabled = request.form.get("directory_enabled") == "1"
                row.reputation_cooldown_hours = _positive(request.form.get("reputation_cooldown_hours"), "Пауза")
                row.thanks_phrases = request.form.get("thanks_phrases", "").strip()
                if not row.thanks_phrases:
                    raise ValueError("Укажите фразы благодарности")
                row.updated_at = now_utc_naive()
                session.commit()
            flash("Канал сохранён", "success")
        except (IntegrityError, ValueError) as exc:
            flash("Этот чат и топик уже добавлены" if isinstance(exc, IntegrityError) else str(exc), "error")
        return redirect(url_for("community_admin") + f"#destination-{destination_id}")

    @app.post("/admin/community/destinations/<int:destination_id>/delete")
    def community_destination_delete(destination_id):
        Session = make_session_factory()
        with Session() as session:
            row = session.get(NotificationDestination, destination_id)
            if row is None:
                abort(404)
            session.delete(row)
            session.commit()
        flash("Канал и связанная история удалены", "success")
        return redirect(url_for("community_admin") + "#destinations")

    @app.post("/admin/community/destinations/<int:destination_id>/test")
    def community_destination_test(destination_id):
        Session = make_session_factory()
        with Session() as session:
            row = session.get(NotificationDestination, destination_id)
            if row is None:
                abort(404)
            try:
                TelegramBotClient().send_message(
                    row.chat_id,
                    "✅ Тестовое сообщение Экопарка. Канал настроен правильно.",
                    message_thread_id=row.message_thread_id,
                )
                flash(f"Тестовое сообщение отправлено в «{row.name}»", "success")
            except Exception as exc:
                flash(f"Ошибка тестовой отправки: {exc}", "error")
        return redirect(url_for("community_admin") + f"#destination-{destination_id}")

    def save_directory_entry(session, row):
        name, phone = request.form.get("name", "").strip(), request.form.get("phone", "").strip()
        url = request.form.get("url", "").strip()
        if not name or not phone:
            raise ValueError("Укажите название и телефон")
        if url and not url.startswith(("http://", "https://", "tg://")):
            raise ValueError("Некорректная ссылка")
        row.name, row.phone = name, phone
        row.category = request.form.get("category", "").strip() or "Общее"
        row.description = request.form.get("description", "").strip() or None
        row.url = url or None
        row.keywords = request.form.get("keywords", "").strip()
        row.position = _positive(request.form.get("position"), "Порядок")
        row.enabled = request.form.get("enabled", "1") == "1"
        row.updated_at = now_utc_naive()

    @app.post("/admin/community/destinations/<int:destination_id>/directory")
    def community_directory_add(destination_id):
        Session = make_session_factory()
        try:
            with Session() as session:
                if session.get(NotificationDestination, destination_id) is None:
                    abort(404)
                now = now_utc_naive()
                row = DirectoryEntry(
                    destination_id=destination_id, name="", category="Общее", phone="",
                    description=None, url=None, keywords="", enabled=True, position=100,
                    created_at=now, updated_at=now,
                )
                save_directory_entry(session, row)
                session.add(row)
                session.commit()
            flash("Контакт добавлен", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("community_admin") + f"#destination-{destination_id}")

    @app.post("/admin/community/directory/<int:entry_id>")
    def community_directory_update(entry_id):
        Session = make_session_factory()
        try:
            with Session() as session:
                row = session.get(DirectoryEntry, entry_id)
                if row is None:
                    abort(404)
                destination_id = row.destination_id
                save_directory_entry(session, row)
                session.commit()
            flash("Контакт сохранён", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("community_admin") + f"#destination-{destination_id}")

    @app.post("/admin/community/directory/<int:entry_id>/delete")
    def community_directory_delete(entry_id):
        Session = make_session_factory()
        with Session() as session:
            row = session.get(DirectoryEntry, entry_id)
            if row is None:
                abort(404)
            destination_id = row.destination_id
            session.delete(row)
            session.commit()
        flash("Контакт удалён", "success")
        return redirect(url_for("community_admin") + f"#destination-{destination_id}")

    @app.post("/admin/community/check-now")
    def community_check_now():
        Session = make_session_factory()
        with Session() as session:
            now = now_utc_naive()
            rows = session.scalars(select(NotificationType).where(NotificationType.enabled.is_(True))).all()
            for row in rows:
                row.force_check_requested = True
                row.next_check_at = now
                row.updated_at = now
            session.commit()
        flash("Проверка начнётся в течение нескольких секунд", "success")
        return redirect(url_for("community_admin"))
