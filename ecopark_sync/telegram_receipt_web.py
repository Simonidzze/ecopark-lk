from datetime import datetime
from io import BytesIO
import uuid

from flask import flash, redirect, render_template, request, send_file, url_for
from sqlalchemy import func, select

from .client import fetch_receipt_pdf
from .db import make_session_factory
from .models import Balance, OwnerPlot, TelegramBinding, TelegramReceiptDelivery
from .telegram_receipts import (
    DEFAULT_RECEIPT_MESSAGE,
    format_money,
    format_receipt_message,
    receipt_filename,
    telegram_receipt_queue_stats,
)
from .utils import now_utc_naive


def load_receipt_recipients(session, scope="debt"):
    rows = session.execute(
        select(TelegramBinding, OwnerPlot, Balance)
        .select_from(TelegramBinding)
        .join(OwnerPlot, OwnerPlot.owner_id == TelegramBinding.owner_id)
        .outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
        .where(TelegramBinding.enabled.is_(True))
        .order_by(OwnerPlot.owner, OwnerPlot.plot_number, TelegramBinding.telegram_user_id)
    ).all()
    recipients = []
    for binding, owner_plot, balance in rows:
        total = balance.total if balance is not None else 0
        if scope == "debt" and total <= 0:
            continue
        recipients.append(
            {
                "key": f"{binding.telegram_user_id}|{owner_plot.id}",
                "binding": binding,
                "owner_plot": owner_plot,
                "balance": balance,
                "total": total,
                "total_text": format_money(total),
            }
        )
    return recipients


def register_telegram_receipt_routes(app):
    @app.get("/admin/telegram/receipts")
    def telegram_receipts_admin():
        scope = request.args.get("scope", "debt")
        if scope not in {"debt", "all"}:
            scope = "debt"
        batch_id = request.args.get("batch_id", "").strip()
        Session = make_session_factory()
        with Session() as session:
            recipients = load_receipt_recipients(session, scope)
            statement = select(TelegramReceiptDelivery).order_by(
                TelegramReceiptDelivery.created_at.desc(),
                TelegramReceiptDelivery.id.desc(),
            )
            if batch_id:
                statement = statement.where(TelegramReceiptDelivery.batch_id == batch_id)
            deliveries = session.scalars(statement.limit(200)).all()
            stats = telegram_receipt_queue_stats(session)
            bound_owners = session.scalar(
                select(func.count(func.distinct(TelegramBinding.owner_id))).where(
                    TelegramBinding.enabled.is_(True)
                )
            ) or 0
        return render_template(
            "telegram_receipts.html",
            recipients=recipients,
            deliveries=deliveries,
            stats=stats,
            scope=scope,
            batch_id=batch_id,
            bound_owners=bound_owners,
            message_template=DEFAULT_RECEIPT_MESSAGE,
        )

    @app.post("/admin/telegram/receipts/queue")
    def telegram_receipts_queue():
        scope = request.form.get("scope", "debt")
        redirect_arguments = {"scope": scope}
        if request.form.get("confirm") != "yes":
            flash("Подтвердите постановку квитанций в очередь", "error")
            return redirect(url_for("telegram_receipts_admin", **redirect_arguments))
        selected = list(dict.fromkeys(request.form.getlist("recipient")))
        if not selected:
            flash("Выберите хотя бы одного получателя", "error")
            return redirect(url_for("telegram_receipts_admin", **redirect_arguments))

        template = request.form.get("message_template") or DEFAULT_RECEIPT_MESSAGE
        try:
            format_receipt_message(
                template,
                owner="Иванов Иван Иванович",
                plot_number="42",
                total="12 345,67 ₽",
                date="26.10.2026",
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("telegram_receipts_admin", **redirect_arguments))

        batch_id = f"receipt-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        queued = 0
        skipped = []
        Session = make_session_factory()
        with Session() as session:
            for key in selected:
                try:
                    telegram_user_id, owner_plot_id = key.split("|", 1)
                except ValueError:
                    skipped.append("некорректный получатель")
                    continue
                row = session.execute(
                    select(TelegramBinding, OwnerPlot, Balance)
                    .select_from(TelegramBinding)
                    .join(
                        OwnerPlot,
                        (OwnerPlot.owner_id == TelegramBinding.owner_id)
                        & (OwnerPlot.id == owner_plot_id),
                    )
                    .outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
                    .where(
                        TelegramBinding.telegram_user_id == telegram_user_id,
                        TelegramBinding.enabled.is_(True),
                    )
                ).first()
                if row is None:
                    skipped.append(f"{owner_plot_id}: привязка не найдена")
                    continue
                binding, owner_plot, balance = row
                total = balance.total if balance is not None else 0
                message_text = format_receipt_message(
                    template,
                    owner=owner_plot.owner,
                    plot_number=owner_plot.plot_number,
                    total=format_money(total),
                    date=(
                        balance.synced_at.strftime("%d.%m.%Y")
                        if balance is not None
                        else owner_plot.synced_at.strftime("%d.%m.%Y")
                    ),
                )
                session.add(
                    TelegramReceiptDelivery(
                        batch_id=batch_id,
                        telegram_user_id=binding.telegram_user_id,
                        chat_id=binding.chat_id,
                        owner_plot_id=owner_plot.id,
                        owner_name=owner_plot.owner,
                        plot_number=owner_plot.plot_number,
                        message_text=message_text,
                        filename=receipt_filename(owner_plot.plot_number),
                        status="queued",
                        attempts=0,
                        created_at=now_utc_naive(),
                    )
                )
                queued += 1
            session.commit()

        if queued:
            text = f"В очередь Telegram добавлено квитанций: {queued}"
            if skipped:
                text += f". Пропущено: {len(skipped)} ({'; '.join(skipped[:3])})"
            flash(text, "success")
            return redirect(url_for("telegram_receipts_admin", batch_id=batch_id, scope=scope))
        flash("В очередь ничего не добавлено", "error")
        return redirect(url_for("telegram_receipts_admin", **redirect_arguments))

    @app.get("/admin/telegram/receipts/<owner_plot_id>/preview.pdf")
    def telegram_receipt_preview(owner_plot_id):
        Session = make_session_factory()
        with Session() as session:
            owner_plot = session.get(OwnerPlot, owner_plot_id)
            if owner_plot is None:
                return "Участок не найден", 404
            filename = receipt_filename(owner_plot.plot_number)
        try:
            document = fetch_receipt_pdf(owner_plot_id)
        except Exception as exc:
            flash(f"Не удалось сформировать квитанцию: {exc}", "error")
            return redirect(url_for("telegram_receipts_admin"))
        return send_file(
            BytesIO(document),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=filename,
        )

    @app.post("/admin/telegram/receipts/<int:delivery_id>/retry")
    def telegram_receipt_retry(delivery_id):
        Session = make_session_factory()
        with Session() as session:
            delivery = session.get(TelegramReceiptDelivery, delivery_id)
            if delivery is None:
                flash("Отправка не найдена", "error")
            elif delivery.status != "failed":
                flash("Повторить можно только неуспешную отправку", "error")
            else:
                delivery.status = "queued"
                delivery.error_text = None
                delivery.started_at = None
                session.commit()
                flash(f"Квитанция #{delivery.id} возвращена в очередь", "success")
        return redirect(
            url_for(
                "telegram_receipts_admin",
                batch_id=request.form.get("batch_id", ""),
            )
        )
