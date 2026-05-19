from decimal import Decimal
from pathlib import Path
import re

try:
    from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for
except ModuleNotFoundError:
    Flask = None

from sqlalchemy import func, select

from .calls import import_call_report, normalize_phone
from .config import env, require_dependency
from .db import make_session_factory
from .models import Accrual, Balance, CallAttempt, CallCampaign, MessengerBinding, Owner, OwnerPlot, Payment, Plot, SyncRun


COUNT_MODELS = (
    ("owners", "Владельцы", Owner),
    ("plots", "Участки", Plot),
    ("owner_plots", "Владельцы участков", OwnerPlot),
    ("balances", "Остатки", Balance),
    ("messenger_bindings", "Мессенджеры", MessengerBinding),
    ("call_campaigns", "Обзвоны", CallCampaign),
    ("call_attempts", "Звонки", CallAttempt),
    ("payments", "Платежи", Payment),
    ("accruals", "Начисления", Accrual),
)


def plot_sort_key(row):
    value = str(row.get("plot_number") or "").strip().lower().replace("ё", "е")
    match = re.match(r"^(\d+)\s*(.*)$", value)
    if not match:
        return (1, value)
    number = int(match.group(1))
    suffix = match.group(2).strip()
    return (0, number, suffix)


def decimal_value(value):
    return Decimal(value or 0)


def month_key(value):
    if value is None:
        return None
    return value.strftime("%Y-%m")


def parse_min_months(value):
    min_months = str(value or "").strip()
    try:
        return min_months, Decimal(min_months.replace(",", ".")) if min_months else None
    except Exception:
        return "", None


def split_phones(value):
    phones = []
    for part in re.split(r"[,;\n\r]+", value or ""):
        phone = part.strip()
        if phone:
            phones.append(phone)
    return phones


def unique_phone_list(rows):
    seen = set()
    phones = []
    for row in rows:
        for phone in row.get("phones", []):
            key = re.sub(r"\D+", "", phone)
            key = key or phone
            if key in seen:
                continue
            seen.add(key)
            phones.append(phone)
    return phones


def parse_max_months(value):
    max_months = str(value or "").strip()
    try:
        return max_months, Decimal(max_months.replace(",", ".")) if max_months else None
    except Exception:
        return "", None


def load_debtors_report(min_months_value, max_months_value=None):
    Session = make_session_factory()
    with Session() as session:
        owner_plot_rows = session.execute(
            select(
                OwnerPlot.id.label("owner_plot_id"),
                OwnerPlot.owner_id,
                OwnerPlot.owner,
                OwnerPlot.phone,
                OwnerPlot.plot_number,
                OwnerPlot.account,
                Balance.total,
            ).outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
        ).mappings().all()

        accrual_rows = session.execute(
            select(Accrual.owner_plot_id, Accrual.date, Accrual.amount)
        ).all()

    owner_plot_to_owner = {
        row["owner_plot_id"]: row["owner_id"]
        for row in owner_plot_rows
    }
    accruals_by_owner_month = {}
    for owner_plot_id, date, amount in accrual_rows:
        owner_id = owner_plot_to_owner.get(owner_plot_id)
        month = month_key(date)
        if not owner_id or not month:
            continue
        accruals_by_owner_month.setdefault(owner_id, {})
        accruals_by_owner_month[owner_id][month] = (
            accruals_by_owner_month[owner_id].get(month, Decimal("0"))
            + decimal_value(amount)
        )

    debtors_by_owner = {}
    for row in owner_plot_rows:
        debt_total = decimal_value(row["total"])
        if debt_total <= 0:
            continue

        owner_id = row["owner_id"]
        debtor = debtors_by_owner.setdefault(
            owner_id,
            {
                "owner": row["owner"],
                "plots": [],
                "phones": [],
                "total_debt": Decimal("0"),
                "monthly_accrual": Decimal("0"),
                "debt_months": None,
            },
        )
        debtor["plots"].append(
            {
                "plot_number": row["plot_number"],
                "account": row["account"],
                "owner_plot_id": row["owner_plot_id"],
            }
        )
        for phone in split_phones(row["phone"]):
            if phone not in debtor["phones"]:
                debtor["phones"].append(phone)
        debtor["total_debt"] += debt_total

    for owner_id, debtor in debtors_by_owner.items():
        monthly_values = [
            amount
            for amount in accruals_by_owner_month.get(owner_id, {}).values()
            if amount > 0
        ]
        if monthly_values:
            monthly_accrual = sum(monthly_values, Decimal("0")) / Decimal(len(monthly_values))
            debtor["monthly_accrual"] = monthly_accrual
            debtor["debt_months"] = debtor["total_debt"] / monthly_accrual
        debtor["plots"] = sorted(debtor["plots"], key=plot_sort_key)

    rows = [
        debtor
        for debtor in debtors_by_owner.values()
        if debtor["debt_months"] is not None
        and (min_months_value is None or debtor["debt_months"] > min_months_value)
        and (max_months_value is None or debtor["debt_months"] <= max_months_value)
    ]
    rows.sort(key=lambda row: (-row["total_debt"], row["owner"]))
    stats = {
        "count": len(rows),
        "total_debt": sum((row["total_debt"] for row in rows), Decimal("0")),
    }
    phones = unique_phone_list(rows)
    return rows, stats, phones


def campaign_analysis(session, campaign):
    attempts = session.scalars(
        select(CallAttempt)
        .where(CallAttempt.campaign_id == campaign.id)
        .order_by(CallAttempt.called_at, CallAttempt.phone)
    ).all()
    owner_plots = session.execute(
        select(
            OwnerPlot.id.label("owner_plot_id"),
            OwnerPlot.owner_id,
            OwnerPlot.owner,
            OwnerPlot.phone,
            OwnerPlot.plot_number,
            OwnerPlot.account,
        )
    ).mappings().all()

    calls_by_phone = {}
    for attempt in attempts:
        item = calls_by_phone.setdefault(
            attempt.phone_normalized,
            {
                "phone": attempt.phone,
                "phone_normalized": attempt.phone_normalized,
                "called_at": attempt.called_at,
                "attempts": 0,
                "duration": 0,
                "cost": Decimal("0"),
            },
        )
        item["attempts"] += 1
        item["duration"] += attempt.call_duration_seconds or 0
        item["cost"] += Decimal(attempt.cost or 0)
        if attempt.called_at and (item["called_at"] is None or attempt.called_at < item["called_at"]):
            item["called_at"] = attempt.called_at

    rows_by_owner_plot = {}
    matched_phones = set()
    for owner_plot in owner_plots:
        owner_phones = [normalize_phone(phone) for phone in split_phones(owner_plot["phone"])]
        matched_calls = [calls_by_phone[phone] for phone in owner_phones if phone in calls_by_phone]
        if not matched_calls:
            continue

        first_call = min((call["called_at"] for call in matched_calls if call["called_at"]), default=None)
        for phone in owner_phones:
            if phone in calls_by_phone:
                matched_phones.add(phone)
        rows_by_owner_plot[owner_plot["owner_plot_id"]] = {
            "owner_plot_id": owner_plot["owner_plot_id"],
            "owner": owner_plot["owner"],
            "plot_number": owner_plot["plot_number"],
            "account": owner_plot["account"],
            "phone": owner_plot["phone"],
            "called_at": first_call,
            "call_duration": sum(call["duration"] for call in matched_calls),
            "call_cost": sum((call["cost"] for call in matched_calls), Decimal("0")),
            "payment_count": 0,
            "payment_sum": Decimal("0"),
            "first_payment_at": None,
        }

    if rows_by_owner_plot:
        payments = session.scalars(
            select(Payment)
            .where(Payment.owner_plot_id.in_(rows_by_owner_plot))
            .order_by(Payment.date)
        ).all()
        for payment in payments:
            row = rows_by_owner_plot.get(payment.owner_plot_id)
            if row is None or row["called_at"] is None or payment.date is None or payment.date < row["called_at"]:
                continue
            row["payment_count"] += 1
            row["payment_sum"] += Decimal(payment.amount or 0)
            if row["first_payment_at"] is None or payment.date < row["first_payment_at"]:
                row["first_payment_at"] = payment.date

    rows = sorted(rows_by_owner_plot.values(), key=lambda row: (row["payment_sum"] <= 0, row["owner"], row["plot_number"]))
    unmatched = [
        call
        for phone, call in sorted(calls_by_phone.items(), key=lambda item: item[1]["phone"])
        if phone not in matched_phones
    ]
    stats = {
        "attempts": len(attempts),
        "phones": len(calls_by_phone),
        "matched_phones": len(matched_phones),
        "matched_owner_plots": len(rows),
        "paid_owner_plots": sum(1 for row in rows if row["payment_sum"] > 0),
        "payment_sum": sum((row["payment_sum"] for row in rows), Decimal("0")),
        "call_cost": sum((Decimal(attempt.cost or 0) for attempt in attempts), Decimal("0")),
    }
    return rows, unmatched, stats


def create_app():
    require_dependency(Flask, "Flask")
    template_folder = Path(__file__).resolve().parent.parent / "templates"
    app = Flask(__name__, template_folder=str(template_folder))
    app.secret_key = env("FLASK_SECRET_KEY", "dev-secret-change-me")

    @app.get("/")
    def index():
        return redirect(url_for("admin"))

    @app.get("/admin")
    def admin():
        db_error = None
        counts = []
        runs = []
        totals = {
            "debt": Decimal("0"),
            "overpayment": Decimal("0"),
            "payments": Decimal("0"),
            "accruals": Decimal("0"),
        }

        try:
            Session = make_session_factory()
            with Session() as session:
                counts = [
                    {
                        "name": name,
                        "label": label,
                        "count": session.scalar(select(func.count()).select_from(model)),
                    }
                    for name, label, model in COUNT_MODELS
                ]
                runs = session.scalars(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(20)).all()
                totals = {
                    "debt": session.scalar(select(func.coalesce(func.sum(Balance.debt), 0))) or Decimal("0"),
                    "overpayment": session.scalar(select(func.coalesce(func.sum(Balance.overpayment), 0))) or Decimal("0"),
                    "payments": session.scalar(select(func.coalesce(func.sum(Payment.amount), 0))) or Decimal("0"),
                    "accruals": session.scalar(select(func.coalesce(func.sum(Accrual.amount), 0))) or Decimal("0"),
                }
        except Exception as exc:
            db_error = str(exc)

        return render_template("admin.html", counts=counts, runs=runs, totals=totals, db_error=db_error)

    @app.get("/admin/debts")
    def debts():
        db_error = None
        rows = []
        search = request.args.get("q", "").strip()

        try:
            Session = make_session_factory()
            with Session() as session:
                statement = (
                    select(
                        OwnerPlot.plot_number,
                        OwnerPlot.account,
                        OwnerPlot.owner,
                        OwnerPlot.phone,
                        OwnerPlot.id.label("owner_plot_id"),
                        Balance.debt,
                        Balance.penalty,
                        Balance.overpayment,
                        Balance.total,
                    )
                    .outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
                    .order_by(OwnerPlot.plot_number, OwnerPlot.owner)
                )
                if search:
                    like = f"%{search}%"
                    statement = statement.where(
                        OwnerPlot.plot_number.like(like)
                        | OwnerPlot.account.like(like)
                        | OwnerPlot.owner.like(like)
                    )
                rows = sorted(session.execute(statement).mappings().all(), key=plot_sort_key)
        except Exception as exc:
            db_error = str(exc)

        return render_template("debts.html", rows=rows, search=search, db_error=db_error)

    @app.get("/admin/debtors")
    def debtors():
        db_error = None
        rows = []
        stats = {"count": 0, "total_debt": Decimal("0")}
        phones = []
        min_months, min_months_value = parse_min_months(request.args.get("months_from", request.args.get("months", "1")))
        max_months, max_months_value = parse_max_months(request.args.get("months_to", ""))

        try:
            rows, stats, phones = load_debtors_report(min_months_value, max_months_value)
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "debtors.html",
            rows=rows,
            stats=stats,
            phones=phones,
            phone_text="\n".join(phones),
            min_months=min_months,
            max_months=max_months,
            db_error=db_error,
        )

    @app.get("/admin/debtors/phones.txt")
    def debtor_phones():
        min_months, min_months_value = parse_min_months(request.args.get("months_from", request.args.get("months", "1")))
        max_months, max_months_value = parse_max_months(request.args.get("months_to", ""))
        _rows, _stats, phones = load_debtors_report(min_months_value, max_months_value)
        body = "\n".join(phones)
        if body:
            body += "\n"
        suffix = f"from-{min_months or '0'}"
        if max_months:
            suffix += f"-to-{max_months}"
        return Response(
            body,
            content_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=debtors-phones-{suffix}.txt"},
        )

    @app.get("/admin/calls")
    def calls():
        db_error = None
        campaigns = []
        summaries = {}

        try:
            Session = make_session_factory()
            with Session() as session:
                campaigns = session.scalars(
                    select(CallCampaign).order_by(CallCampaign.called_at.desc(), CallCampaign.id.desc())
                ).all()
                for campaign in campaigns:
                    _rows, _unmatched, stats = campaign_analysis(session, campaign)
                    summaries[campaign.id] = stats
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "calls.html",
            campaigns=campaigns,
            summaries=summaries,
            db_error=db_error,
        )

    @app.post("/admin/calls/import")
    def import_calls():
        uploaded = request.files.get("report")
        if uploaded is None or not uploaded.filename:
            flash("Выберите CSV-файл отчета обзвона", "error")
            return redirect(url_for("calls"))

        try:
            result = import_call_report(uploaded.stream, source_file=uploaded.filename)
            flash(f"Отчет обзвона загружен: {result['attempts']} звонков", "success")
            return redirect(url_for("call_detail", campaign_id=result["campaign_id"]))
        except Exception as exc:
            flash(f"Ошибка загрузки отчета обзвона: {exc}", "error")
            return redirect(url_for("calls"))

    @app.get("/admin/calls/<int:campaign_id>")
    def call_detail(campaign_id):
        db_error = None
        campaign = None
        rows = []
        unmatched = []
        stats = {}

        try:
            Session = make_session_factory()
            with Session() as session:
                campaign = session.get(CallCampaign, campaign_id)
                if campaign is None:
                    abort(404)
                rows, unmatched, stats = campaign_analysis(session, campaign)
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "call_detail.html",
            campaign=campaign,
            rows=rows,
            unmatched=unmatched,
            stats=stats,
            db_error=db_error,
        )

    @app.get("/admin/plots/<owner_plot_id>")
    def plot_detail(owner_plot_id):
        db_error = None
        details = None
        payments = []
        accruals = []

        try:
            Session = make_session_factory()
            with Session() as session:
                details = session.execute(
                    select(
                        OwnerPlot.id,
                        OwnerPlot.plot_number,
                        OwnerPlot.account,
                        OwnerPlot.owner,
                        OwnerPlot.phone,
                        Balance.debt,
                        Balance.penalty,
                        Balance.overpayment,
                        Balance.total,
                    )
                    .outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
                    .where(OwnerPlot.id == owner_plot_id)
                ).mappings().first()
                if details is None:
                    abort(404)

                payments = session.scalars(
                    select(Payment)
                    .where(Payment.owner_plot_id == owner_plot_id)
                    .order_by(Payment.date.desc(), Payment.number.desc())
                ).all()
                accruals = session.scalars(
                    select(Accrual)
                    .where(Accrual.owner_plot_id == owner_plot_id)
                    .order_by(Accrual.date.desc(), Accrual.number.desc())
                ).all()
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "plot_detail.html",
            details=details,
            payments=payments,
            accruals=accruals,
            db_error=db_error,
        )

    @app.post("/admin/sync")
    def sync_now():
        from .scheduler import run_once_recording_errors

        try:
            result = run_once_recording_errors()
            flash(f"Синхронизация завершена, запуск #{result['run_id']}", "success")
        except Exception as exc:
            flash(f"Ошибка синхронизации: {exc}", "error")
        return redirect(url_for("admin"))

    @app.get("/health")
    def health():
        return {"ok": True, "service": "ecopark-lk-admin"}

    if env("WEB_SYNC_ENABLED", "false").lower() in {"1", "true", "yes", "y"}:
        from .scheduler import make_scheduler

        scheduler = make_scheduler()
        scheduler.start_background()
        app.extensions["ecopark_sync_scheduler"] = scheduler

    return app


def run_server():
    app = create_app()
    app.run(
        host=env("FLASK_HOST", "127.0.0.1"),
        port=int(env("FLASK_PORT", "8080")),
        debug=env("FLASK_DEBUG", "false").lower() in {"1", "true", "yes", "y"},
    )
