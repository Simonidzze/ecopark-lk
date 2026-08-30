from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import re

try:
    from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, url_for
except ModuleNotFoundError:
    Flask = None

from sqlalchemy import delete, func, select

from .calls import import_call_report, normalize_phone
from .claims import (
    DEFAULT_DEBT_PERIOD_START,
    DOCX_CONTENT_TYPE,
    claim_values,
    extract_cadastral_number,
    parse_iso_date,
    render_pretrial_claim,
    safe_claim_filename,
)
from .config import env, require_dependency
from .db import make_session_factory
from .models import Accrual, Balance, CallAttempt, CallCampaign, MessengerBinding, Owner, OwnerPlot, Payment, Plot, PretrialClaim, SyncRun
from .utils import now_utc_naive


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
    ("pretrial_claims", "Досудебные претензии", PretrialClaim),
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


def date_value(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def month_end(value):
    value = date_value(value)
    next_month = (value.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)


def iter_month_ends(first_value, last_value):
    current = month_end(first_value)
    last = date_value(last_value)
    while current <= last:
        yield current
        current = month_end(current + timedelta(days=1))


def build_monthly_debt_report(owner_plot_rows, accrual_rows, payment_rows, as_of):
    """Reconstruct principal balances at completed month ends from the current snapshot."""
    as_of_date = date_value(as_of)
    report_end = as_of_date.replace(day=1) - timedelta(days=1)
    balances = {}
    owners = {}
    for row in owner_plot_rows:
        owner_plot_id = row["owner_plot_id"]
        balances[owner_plot_id] = (
            decimal_value(row.get("debt"))
            - decimal_value(row.get("overpayment"))
        )
        owners[owner_plot_id] = row.get("owner_id") or owner_plot_id

    operations = []
    accruals_by_month = defaultdict(Decimal)
    payments_by_month = defaultdict(Decimal)
    first_operation_date = None

    for owner_plot_id, operation_date, amount in accrual_rows:
        operation_date = date_value(operation_date)
        if operation_date is None or operation_date > as_of_date:
            continue
        amount = decimal_value(amount)
        operations.append((operation_date, owner_plot_id, amount, "accrual"))
        accruals_by_month[month_key(operation_date)] += amount
        if first_operation_date is None or operation_date < first_operation_date:
            first_operation_date = operation_date

    for owner_plot_id, operation_date, amount in payment_rows:
        operation_date = date_value(operation_date)
        if operation_date is None or operation_date > as_of_date:
            continue
        amount = decimal_value(amount)
        operations.append((operation_date, owner_plot_id, amount, "payment"))
        payments_by_month[month_key(operation_date)] += amount
        if first_operation_date is None or operation_date < first_operation_date:
            first_operation_date = operation_date

    report_start = min(first_operation_date or report_end, report_end)
    period_ends = list(iter_month_ends(report_start, report_end))
    operations.sort(key=lambda item: item[0], reverse=True)

    rows = []
    operation_index = 0
    for period_end in reversed(period_ends):
        while operation_index < len(operations) and operations[operation_index][0] > period_end:
            _date, owner_plot_id, amount, operation_type = operations[operation_index]
            balances.setdefault(owner_plot_id, Decimal("0"))
            owners.setdefault(owner_plot_id, owner_plot_id)
            if operation_type == "accrual":
                balances[owner_plot_id] -= amount
            else:
                balances[owner_plot_id] += amount
            operation_index += 1

        debt = sum((amount for amount in balances.values() if amount > 0), Decimal("0"))
        overpayment = -sum((amount for amount in balances.values() if amount < 0), Decimal("0"))
        debtor_ids = {
            owners[owner_plot_id]
            for owner_plot_id, amount in balances.items()
            if amount > 0
        }
        key = month_key(period_end)
        rows.append(
            {
                "month": key,
                "period_end": period_end,
                "accruals": accruals_by_month[key],
                "payments": payments_by_month[key],
                "debt": debt,
                "overpayment": overpayment,
                "net_balance": debt - overpayment,
                "debtors": len(debtor_ids),
                "debt_change": None,
            }
        )

    rows.reverse()
    previous_debt = None
    for row in rows:
        if previous_debt is not None:
            row["debt_change"] = row["debt"] - previous_debt
        previous_debt = row["debt"]
    rows.reverse()
    return rows


def load_monthly_debt_report():
    Session = make_session_factory()
    with Session() as session:
        owner_plot_rows = session.execute(
            select(
                OwnerPlot.id.label("owner_plot_id"),
                OwnerPlot.owner_id,
                Balance.debt,
                Balance.overpayment,
            ).outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
        ).mappings().all()
        accrual_rows = session.execute(
            select(Accrual.owner_plot_id, Accrual.date, Accrual.amount)
        ).all()
        payment_rows = session.execute(
            select(Payment.owner_plot_id, Payment.date, Payment.amount)
        ).all()
        as_of = session.scalar(
            select(func.max(SyncRun.source_generated_at)).where(SyncRun.status == "ok")
        )
        if as_of is None:
            as_of = session.scalar(select(func.max(Balance.synced_at))) or datetime.now()

    rows = build_monthly_debt_report(
        owner_plot_rows,
        accrual_rows,
        payment_rows,
        as_of,
    )
    latest = rows[0] if rows else None
    return rows, latest, as_of


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
        payment_rows = session.execute(
            select(Payment.owner_plot_id, Payment.date, Payment.amount)
            .where(Payment.date.is_not(None))
            .order_by(Payment.date)
        ).all()

    owner_plot_to_owner = {
        row["owner_plot_id"]: row["owner_id"]
        for row in owner_plot_rows
    }
    accruals_by_owner_month = {}
    last_payment_by_owner = {}
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

    for owner_plot_id, date, amount in payment_rows:
        owner_id = owner_plot_to_owner.get(owner_plot_id)
        if not owner_id or date is None:
            continue
        current = last_payment_by_owner.get(owner_id)
        if current is None or date > current["date"]:
            last_payment_by_owner[owner_id] = {"date": date, "amount": decimal_value(amount)}

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
                "last_payment_date": None,
                "last_payment_amount": Decimal("0"),
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
        last_payment = last_payment_by_owner.get(owner_id)
        if last_payment is not None:
            debtor["last_payment_date"] = last_payment["date"]
            debtor["last_payment_amount"] = last_payment["amount"]

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


def campaign_payment_period_end(session, campaign, as_of=None):
    as_of = as_of or datetime.now().replace(microsecond=0)
    if campaign.called_at is None:
        return as_of

    next_campaign_at = session.scalar(
        select(CallCampaign.called_at)
        .where(CallCampaign.id != campaign.id)
        .where(func.date(CallCampaign.called_at) > campaign.called_at.date().isoformat())
        .order_by(CallCampaign.called_at, CallCampaign.id)
        .limit(1)
    )
    if next_campaign_at is None:
        return as_of
    return min(next_campaign_at, as_of)


def delete_campaign_day(session, campaign):
    campaign_ids_statement = select(CallCampaign.id).where(CallCampaign.id == campaign.id)
    if campaign.called_at is not None:
        campaign_ids_statement = select(CallCampaign.id).where(
            func.date(CallCampaign.called_at) == campaign.called_at.date().isoformat()
        )

    campaign_ids = list(session.scalars(campaign_ids_statement).all())
    attempts_count = session.scalar(
        select(func.count())
        .select_from(CallAttempt)
        .where(CallAttempt.campaign_id.in_(campaign_ids))
    ) or 0
    session.execute(delete(CallAttempt).where(CallAttempt.campaign_id.in_(campaign_ids)))
    session.execute(delete(CallCampaign).where(CallCampaign.id.in_(campaign_ids)))
    return {"campaigns": len(campaign_ids), "attempts": attempts_count}


def campaign_analysis(session, campaign, as_of=None):
    payment_period_end = campaign_payment_period_end(session, campaign, as_of=as_of)
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
            .where(Payment.date < payment_period_end)
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

    @app.get("/admin/debts/monthly")
    def monthly_debts():
        db_error = None
        rows = []
        latest = None
        as_of = None

        try:
            rows, latest, as_of = load_monthly_debt_report()
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "monthly_debts.html",
            rows=rows,
            latest=latest,
            as_of=as_of,
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

    @app.post("/admin/calls/<int:campaign_id>/delete")
    def delete_call_campaign(campaign_id):
        Session = make_session_factory()
        with Session() as session:
            campaign = session.get(CallCampaign, campaign_id)
            if campaign is None:
                abort(404)
            campaign_day = campaign.called_at.strftime("%d.%m.%Y") if campaign.called_at else campaign.title
            result = delete_campaign_day(session, campaign)
            session.commit()

        flash(
            f"Обзвон за {campaign_day} удалён: "
            f"{result['campaigns']} отчётов, {result['attempts']} звонков",
            "success",
        )
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
        issued_claims = []
        claim_defaults = {}
        claim_missing = []

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
                        Plot.address,
                        Plot.cadastral_number,
                        Balance.debt,
                        Balance.penalty,
                        Balance.overpayment,
                        Balance.total,
                        Balance.synced_at.label("balance_synced_at"),
                    )
                    .outerjoin(Plot, Plot.id == OwnerPlot.plot_id)
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
                issued_claims = session.scalars(
                    select(PretrialClaim)
                    .where(PretrialClaim.owner_plot_id == owner_plot_id)
                    .order_by(PretrialClaim.number.desc())
                    .limit(20)
                ).all()

                calculation_date = date_value(details["balance_synced_at"]) or datetime.now().date()
                claim_defaults = {
                    "claim_date": datetime.now().date().isoformat(),
                    "cadastral_number": (
                        details["cadastral_number"]
                        or extract_cadastral_number(details["address"])
                    ),
                    "debt_period_from": DEFAULT_DEBT_PERIOD_START.isoformat(),
                    "debt_period_to": calculation_date.isoformat(),
                    "charge_basis": env("TSN_CLAIM_BASIS", ""),
                }

                organization_fields = (
                    ("TSN_LEGAL_ADDRESS", "юридический или почтовый адрес ТСН"),
                    ("TSN_PHONE", "телефон ТСН"),
                    ("TSN_EMAIL", "электронная почта ТСН"),
                )
                claim_missing.extend(
                    label
                    for setting, label in organization_fields
                    if not env(setting, "").strip()
                )
                if not details["address"]:
                    claim_missing.append("адрес участка")
                if not claim_defaults["cadastral_number"]:
                    claim_missing.append("кадастровый номер")
                if not claim_defaults["charge_basis"]:
                    claim_missing.append("дата и номер решения общего собрания об обязательных платежах")
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "plot_detail.html",
            details=details,
            payments=payments,
            accruals=accruals,
            issued_claims=issued_claims,
            claim_defaults=claim_defaults,
            claim_missing=claim_missing,
            db_error=db_error,
        )

    @app.post("/admin/plots/<owner_plot_id>/pretrial-claim.docx")
    def plot_pretrial_claim(owner_plot_id):
        Session = make_session_factory()
        with Session() as session:
            details = session.execute(
                select(
                    OwnerPlot.id,
                    OwnerPlot.plot_number,
                    OwnerPlot.account,
                    OwnerPlot.owner,
                    Plot.address,
                    Plot.cadastral_number,
                    Balance.debt,
                    Balance.penalty,
                    Balance.overpayment,
                    Balance.total,
                    Balance.synced_at.label("balance_synced_at"),
                )
                .outerjoin(Plot, Plot.id == OwnerPlot.plot_id)
                .outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
                .where(OwnerPlot.id == owner_plot_id)
            ).mappings().first()
            if details is None:
                abort(404)

            principal_amount = decimal_value(details["debt"])
            penalty_amount = decimal_value(details["penalty"])
            if details["total"] is None:
                total_amount = principal_amount + penalty_amount - decimal_value(details["overpayment"])
            else:
                total_amount = decimal_value(details["total"])
            if total_amount <= 0:
                abort(400, description="По участку нет суммы к взысканию")

            calculation_date = date_value(details["balance_synced_at"]) or datetime.now().date()
            try:
                claim_date = parse_iso_date(
                    request.form.get("claim_date"),
                    "Дата претензии",
                    default=datetime.now().date(),
                )
                debt_period_from = parse_iso_date(
                    request.form.get("debt_period_from"),
                    "Период задолженности с",
                    default=DEFAULT_DEBT_PERIOD_START,
                )
                debt_period_to = parse_iso_date(
                    request.form.get("debt_period_to"),
                    "Период задолженности по",
                    default=calculation_date,
                )
            except ValueError as exc:
                abort(400, description=str(exc))

            issued_claim = PretrialClaim(
                owner_plot_id=owner_plot_id,
                plot_number=details["plot_number"],
                owner_name=details["owner"],
                claim_date=claim_date,
                calculation_date=calculation_date,
                debt_period_from=debt_period_from,
                debt_period_to=debt_period_to,
                principal_amount=principal_amount,
                total_amount=total_amount,
                created_at=now_utc_naive(),
            )
            session.add(issued_claim)
            session.flush()

            values = claim_values(
                tsn_address=env("TSN_LEGAL_ADDRESS", ""),
                tsn_phone=env("TSN_PHONE", ""),
                tsn_email=env("TSN_EMAIL", ""),
                plot_number=details["plot_number"],
                owner_name=details["owner"],
                plot_address=details["address"],
                cadastral_number=(
                    request.form.get("cadastral_number", "").strip()
                    or details["cadastral_number"]
                    or extract_cadastral_number(details["address"])
                ),
                claim_number=str(issued_claim.number),
                claim_date=claim_date,
                charge_basis=(
                    request.form.get("charge_basis", "").strip()
                    or env("TSN_CLAIM_BASIS", "")
                ),
                account=details["account"],
                calculation_date=calculation_date,
                debt_period_from=debt_period_from,
                debt_period_to=debt_period_to,
                principal_amount=principal_amount,
                penalty_amount=penalty_amount,
                penalty_basis="",
                total_amount=total_amount,
            )
            document = render_pretrial_claim(values)
            session.commit()

        return send_file(
            document,
            mimetype=DOCX_CONTENT_TYPE,
            as_attachment=True,
            download_name=safe_claim_filename(
                details["plot_number"],
                claim_date,
                issued_claim.number,
            ),
            max_age=0,
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
