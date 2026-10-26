from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import uuid

try:
    from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, url_for
except ModuleNotFoundError:
    Flask = None

from sqlalchemy import delete, func, select

from .calls import import_call_report, normalize_phone
from .claims import (
    ClaimPdfConversionError,
    DEFAULT_CLAIM_BASIS,
    DEFAULT_DEBT_PERIOD_START,
    PDF_CONTENT_TYPE,
    claim_values,
    extract_cadastral_number,
    format_ru_money,
    parse_iso_date,
    render_pretrial_claim_pdf,
    safe_claim_filename,
)
from .config import env, require_dependency
from .db import make_session_factory
from .expense_allocations import (
    is_payroll_employee_fragment,
    money,
    normalize_text,
    refresh_auto_expense_allocations,
    replace_manual_allocations,
)
from .expense_report import (
    expense_filter_values,
    load_expense_detail,
    load_expense_report,
    load_monthly_expense_report,
    render_expenses_xlsx,
)
from .models import (
    Accrual,
    Balance,
    CallAttempt,
    CallCampaign,
    Expense,
    ExpenseAllocationRule,
    ExpenseBudgetCategory,
    Income,
    MessengerBinding,
    Owner,
    OwnerPlot,
    Payment,
    Plot,
    PretrialClaim,
    SyncRun,
    TelegramBinding,
    TelegramLinkRequest,
    TelegramProfile,
    WhatsAppMessage,
)
from .payment_report import XLSX_CONTENT_TYPE, load_payment_status_report, render_payment_status_xlsx
from .utils import now_utc_naive
from .whatsapp import (
    DEFAULT_DEBT_REMINDER_TEMPLATE,
    DEFAULT_MESSAGE_TEMPLATE,
    WhatsAppClient,
    WhatsAppServiceError,
    first_whatsapp_phone,
    format_whatsapp_message,
    normalize_whatsapp_phone,
    whatsapp_queue_stats,
)


COUNT_MODELS = (
    ("owners", "Владельцы", Owner),
    ("plots", "Участки", Plot),
    ("owner_plots", "Владельцы участков", OwnerPlot),
    ("balances", "Остатки", Balance),
    ("messenger_bindings", "Мессенджеры", MessengerBinding),
    ("telegram_bindings", "Telegram-привязки", TelegramBinding),
    ("telegram_link_requests", "Заявки Telegram", TelegramLinkRequest),
    ("call_campaigns", "Обзвоны", CallCampaign),
    ("call_attempts", "Звонки", CallAttempt),
    ("payments", "Платежи", Payment),
    ("accruals", "Начисления", Accrual),
    ("incomes", "Прочие доходы", Income),
    ("expenses", "Расходы", Expense),
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


def build_cash_balance_summary(
    payment_total,
    expense_total,
    payment_from=None,
    payment_to=None,
    expense_from=None,
    expense_to=None,
    income_total=0,
    income_from=None,
    income_to=None,
):
    period_from_values = [value for value in (payment_from, income_from, expense_from) if value]
    period_to_values = [value for value in (payment_to, income_to, expense_to) if value]
    payments = decimal_value(payment_total)
    incomes = decimal_value(income_total)
    expenses = decimal_value(expense_total)
    return {
        "cash_balance": payments + incomes - expenses,
        "total_incomes": payments + incomes,
        "cash_period_from": min(period_from_values) if period_from_values else None,
        "cash_period_to": max(period_to_values) if period_to_values else None,
    }


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


def build_monthly_debt_report(owner_plot_rows, accrual_rows, payment_rows, as_of, income_rows=()):
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
    incomes_by_month = defaultdict(Decimal)
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

    for operation_date, amount in income_rows:
        operation_date = date_value(operation_date)
        if operation_date is None or operation_date > as_of_date:
            continue
        incomes_by_month[month_key(operation_date)] += decimal_value(amount)
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
                "other_incomes": incomes_by_month[key],
                "total_incomes": payments_by_month[key] + incomes_by_month[key],
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
        income_rows = session.execute(select(Income.date, Income.amount)).all()
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
        income_rows,
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


def load_claim_details(session, owner_plot_id):
    return session.execute(
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


def prepare_pretrial_claim(session, owner_plot_id, fields):
    details = load_claim_details(session, owner_plot_id)
    if details is None:
        raise LookupError("Участок не найден")

    principal_amount = decimal_value(details["debt"])
    penalty_amount = decimal_value(details["penalty"])
    if details["total"] is None:
        total_amount = principal_amount + penalty_amount - decimal_value(details["overpayment"])
    else:
        total_amount = decimal_value(details["total"])
    if total_amount <= 0:
        raise ValueError("По участку нет суммы к взысканию")

    calculation_date = date_value(details["balance_synced_at"]) or datetime.now().date()
    claim_date = parse_iso_date(
        fields.get("claim_date"),
        "Дата претензии",
        default=datetime.now().date(),
    )
    debt_period_from = parse_iso_date(
        fields.get("debt_period_from"),
        "Период задолженности с",
        default=DEFAULT_DEBT_PERIOD_START,
    )
    debt_period_to = parse_iso_date(
        fields.get("debt_period_to"),
        "Период задолженности по",
        default=calculation_date,
    )
    cadastral_number = (
        str(fields.get("cadastral_number") or "").strip()
        or details["cadastral_number"]
        or extract_cadastral_number(details["address"])
    )

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
        cadastral_number=cadastral_number,
        claim_number=str(issued_claim.number),
        claim_date=claim_date,
        charge_basis=(
            str(fields.get("charge_basis") or "").strip()
            or env("TSN_CLAIM_BASIS", "")
            or DEFAULT_CLAIM_BASIS
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
    filename = safe_claim_filename(
        details["plot_number"], claim_date, issued_claim.number, extension="pdf"
    )
    return {
        "details": details,
        "claim": issued_claim,
        "values": values,
        "filename": filename,
        "claim_date": claim_date,
        "cadastral_number": cadastral_number,
    }


def enqueue_whatsapp_claim(session, owner_plot_id, fields, phone, message_template, batch_id):
    normalized_phone = normalize_whatsapp_phone(phone)
    package = prepare_pretrial_claim(session, owner_plot_id, fields)
    details = package["details"]
    if not details["address"]:
        raise ValueError("не указан адрес участка")
    if not package["cadastral_number"]:
        raise ValueError("не указан кадастровый номер")
    message_text = format_whatsapp_message(
        message_template,
        owner=details["owner"],
        plot_number=details["plot_number"],
        claim_number=package["claim"].number,
        debt=package["values"]["TOTAL_AMOUNT"],
    )
    queued = WhatsAppMessage(
        batch_id=batch_id,
        claim_number=package["claim"].number,
        owner_plot_id=owner_plot_id,
        owner_name=details["owner"],
        plot_number=details["plot_number"],
        phone=normalized_phone,
        message_text=message_text,
        claim_values_json=json.dumps(package["values"], ensure_ascii=False),
        filename=package["filename"],
        status="queued",
        attempts=0,
        created_at=now_utc_naive(),
    )
    session.add(queued)
    session.flush()
    return queued


def enqueue_whatsapp_debt_message(session, row, phone, message_template, batch_id):
    normalized_phone = normalize_whatsapp_phone(phone)
    plots = row.get("plots") or []
    if not plots:
        raise ValueError("не указан участок")
    plot_numbers = ", ".join(
        str(plot.get("plot_number") or "").strip()
        for plot in plots
        if str(plot.get("plot_number") or "").strip()
    )
    if not plot_numbers:
        raise ValueError("не указан номер участка")
    message_text = format_whatsapp_message(
        message_template,
        owner=row.get("owner") or "Собственник",
        plot_number=plot_numbers,
        claim_number="",
        debt=format_ru_money(row.get("total_debt")),
    )
    queued = WhatsAppMessage(
        batch_id=batch_id,
        claim_number=0,
        owner_plot_id=plots[0]["owner_plot_id"],
        owner_name=row.get("owner") or "Собственник",
        plot_number=plot_numbers[:64],
        phone=normalized_phone,
        message_text=message_text,
        claim_values_json="{}",
        filename="",
        status="queued",
        attempts=0,
        created_at=now_utc_naive(),
    )
    session.add(queued)
    session.flush()
    return queued


def refresh_all_auto_expenses(session):
    expenses = session.scalars(
        select(Expense).where(Expense.allocation_mode == "auto")
    ).all()
    return refresh_auto_expense_allocations(session, expenses)


def normalized_rule_fragment(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def validate_expense_rule(session, category_id, purpose_contains, exclude_id=None):
    category = session.get(ExpenseBudgetCategory, category_id)
    if category is None or not category.active:
        abort(400, description="Выберите действующую статью сметы")
    if not purpose_contains:
        abort(400, description="Укажите фрагмент назначения платежа")
    if len(purpose_contains) > 255:
        abort(400, description="Фрагмент назначения не должен превышать 255 символов")

    normalized_fragment = normalize_text(purpose_contains)
    rules = session.scalars(select(ExpenseAllocationRule)).all()
    for rule in rules:
        if rule.id != exclude_id and normalize_text(rule.purpose_contains) == normalized_fragment:
            abort(400, description="Такой фрагмент назначения уже есть в справочнике")
    return category


def create_app():
    require_dependency(Flask, "Flask")
    template_folder = Path(__file__).resolve().parent.parent / "templates"
    app = Flask(__name__, template_folder=str(template_folder))
    app.secret_key = env("FLASK_SECRET_KEY", "dev-secret-change-me")

    if env("WHATSAPP_AUTO_INIT_SCHEMA", "false").lower() in {"1", "true", "yes", "y"}:
        try:
            from .schema import ensure_whatsapp_schema

            ensure_whatsapp_schema()
        except Exception:
            app.logger.exception("Не удалось автоматически создать таблицу WhatsApp")

    if env("EXPENSE_AUTO_INIT_SCHEMA", "false").lower() in {"1", "true", "yes", "y"}:
        try:
            from .schema import ensure_expense_schema

            ensure_expense_schema()
        except Exception:
            app.logger.exception("Не удалось подготовить разнесение расходов")

    if env("TELEGRAM_AUTO_INIT_SCHEMA", "false").lower() in {"1", "true", "yes", "y"}:
        try:
            from .community import seed_community_defaults
            from .schema import ensure_community_schema, ensure_telegram_schema

            ensure_telegram_schema()
            ensure_community_schema()
            Session = make_session_factory()
            with Session() as session:
                seed_community_defaults(session)
        except Exception:
            app.logger.exception("Не удалось подготовить таблицы Telegram и сообщества")

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
            "incomes": Decimal("0"),
            "total_incomes": Decimal("0"),
            "accruals": Decimal("0"),
            "expenses": Decimal("0"),
            "cash_balance": Decimal("0"),
            "cash_period_from": None,
            "cash_period_to": None,
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
                payment_summary = session.execute(
                    select(
                        func.coalesce(func.sum(Payment.amount), 0),
                        func.min(Payment.date),
                        func.max(Payment.date),
                    )
                ).one()
                expense_summary = session.execute(
                    select(
                        func.coalesce(func.sum(Expense.amount), 0),
                        func.min(Expense.date),
                        func.max(Expense.date),
                    )
                ).one()
                income_summary = session.execute(
                    select(
                        func.coalesce(func.sum(Income.amount), 0),
                        func.min(Income.date),
                        func.max(Income.date),
                    )
                ).one()
                totals = {
                    "debt": session.scalar(select(func.coalesce(func.sum(Balance.debt), 0))) or Decimal("0"),
                    "overpayment": session.scalar(select(func.coalesce(func.sum(Balance.overpayment), 0))) or Decimal("0"),
                    "payments": payment_summary[0] or Decimal("0"),
                    "incomes": income_summary[0] or Decimal("0"),
                    "accruals": session.scalar(select(func.coalesce(func.sum(Accrual.amount), 0))) or Decimal("0"),
                    "expenses": expense_summary[0] or Decimal("0"),
                }
                totals.update(
                    build_cash_balance_summary(
                        payment_summary[0],
                        expense_summary[0],
                        payment_summary[1],
                        payment_summary[2],
                        expense_summary[1],
                        expense_summary[2],
                        income_total=income_summary[0],
                        income_from=income_summary[1],
                        income_to=income_summary[2],
                    )
                )
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

    @app.get("/admin/expenses")
    def expenses():
        db_error = None
        rows = []
        categories = []
        as_of = None
        stats = {
            "count": 0,
            "total": Decimal("0"),
            "allocated_total": Decimal("0"),
            "unallocated_total": Decimal("0"),
            "unallocated_count": 0,
            "categories": 0,
            "counterparties": 0,
            "category_totals": [],
        }
        filters = expense_filter_values(request.args)

        try:
            rows, stats, categories, as_of = load_expense_report(filters)
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "expenses.html",
            rows=rows,
            stats=stats,
            categories=categories,
            filters=filters,
            as_of=as_of,
            db_error=db_error,
        )

    @app.get("/admin/expense-rules")
    def expense_rules():
        Session = make_session_factory()
        with Session() as session:
            categories = session.scalars(
                select(ExpenseBudgetCategory)
                .where(ExpenseBudgetCategory.active.is_(True))
                .order_by(
                    ExpenseBudgetCategory.sort_order,
                    ExpenseBudgetCategory.name,
                )
            ).all()
            rows = session.execute(
                select(ExpenseAllocationRule, ExpenseBudgetCategory.name)
                .outerjoin(
                    ExpenseBudgetCategory,
                    ExpenseBudgetCategory.id == ExpenseAllocationRule.category_id,
                )
                .order_by(
                    ExpenseAllocationRule.active.desc(),
                    ExpenseAllocationRule.purpose_contains,
                    ExpenseAllocationRule.id,
                )
            ).all()
            rules = [
                {
                    "id": rule.id,
                    "category_id": rule.category_id,
                    "category_name": category_name or rule.category_id,
                    "purpose_contains": rule.purpose_contains,
                    "payroll_only": is_payroll_employee_fragment(
                        rule.purpose_contains
                    ),
                    "active": rule.active,
                }
                for rule, category_name in rows
            ]
        return render_template(
            "expense_rules.html",
            rules=rules,
            categories=categories,
        )

    @app.post("/admin/expense-rules")
    def create_expense_rule():
        category_id = str(request.form.get("category_id") or "").strip()
        purpose_contains = normalized_rule_fragment(
            request.form.get("purpose_contains")
        )
        Session = make_session_factory()
        with Session() as session:
            validate_expense_rule(session, category_id, purpose_contains)
            timestamp = now_utc_naive()
            session.add(
                ExpenseAllocationRule(
                    category_id=category_id,
                    purpose_contains=purpose_contains,
                    active=True,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            session.flush()
            refreshed = refresh_all_auto_expenses(session)
            session.commit()
        flash(
            f"Правило добавлено. Автоматически пересчитано расходов: {refreshed}",
            "success",
        )
        return redirect(url_for("expense_rules"))

    @app.post("/admin/expense-rules/<int:rule_id>")
    def update_expense_rule(rule_id):
        category_id = str(request.form.get("category_id") or "").strip()
        purpose_contains = normalized_rule_fragment(
            request.form.get("purpose_contains")
        )
        active = request.form.get("active") == "1"
        Session = make_session_factory()
        with Session() as session:
            rule = session.get(ExpenseAllocationRule, rule_id)
            if rule is None:
                abort(404)
            validate_expense_rule(
                session,
                category_id,
                purpose_contains,
                exclude_id=rule.id,
            )
            rule.category_id = category_id
            rule.purpose_contains = purpose_contains
            rule.active = active
            rule.updated_at = now_utc_naive()
            session.flush()
            refreshed = refresh_all_auto_expenses(session)
            session.commit()
        flash(
            f"Правило сохранено. Автоматически пересчитано расходов: {refreshed}",
            "success",
        )
        return redirect(url_for("expense_rules"))

    @app.post("/admin/expense-rules/<int:rule_id>/delete")
    def delete_expense_rule(rule_id):
        Session = make_session_factory()
        with Session() as session:
            rule = session.get(ExpenseAllocationRule, rule_id)
            if rule is None:
                abort(404)
            session.delete(rule)
            session.flush()
            refreshed = refresh_all_auto_expenses(session)
            session.commit()
        flash(
            f"Правило удалено. Автоматически пересчитано расходов: {refreshed}",
            "success",
        )
        return redirect(url_for("expense_rules"))

    @app.get("/admin/expenses/<expense_id>")
    def expense_detail(expense_id):
        expense, categories = load_expense_detail(expense_id)
        if expense is None:
            abort(404)
        return render_template(
            "expense_detail.html",
            expense=expense,
            categories=categories,
        )

    @app.post("/admin/expenses/<expense_id>/allocations")
    def save_expense_allocations(expense_id):
        Session = make_session_factory()
        with Session() as session:
            expense = session.get(Expense, expense_id)
            if expense is None:
                abort(404)
            categories = session.scalars(
                select(ExpenseBudgetCategory)
                .where(ExpenseBudgetCategory.active.is_(True))
                .order_by(
                    ExpenseBudgetCategory.sort_order,
                    ExpenseBudgetCategory.name,
                )
            ).all()
            amounts = {}
            try:
                for category in categories:
                    raw_value = request.form.get(f"allocation_{category.id}", "").strip()
                    if not raw_value:
                        continue
                    amount = money(Decimal(raw_value.replace(" ", "").replace(",", ".")))
                    if amount < 0:
                        raise ValueError("Сумма по статье не может быть отрицательной")
                    if amount > 0:
                        amounts[category.id] = amount
            except Exception as exc:
                abort(400, description=f"Некорректная сумма разнесения: {exc}")

            allocated_total = sum(amounts.values(), Decimal("0.00"))
            expense_total = money(expense.amount)
            if allocated_total > expense_total:
                abort(
                    400,
                    description=(
                        f"Разнесено {allocated_total:.2f}, но сумма расхода "
                        f"{expense_total:.2f}"
                    ),
                )

            replace_manual_allocations(session, expense, amounts)
            session.commit()

        remainder = expense_total - allocated_total
        flash(
            f"Ручное разнесение сохранено. Не распределено: {remainder:.2f} руб.",
            "success",
        )
        return redirect(url_for("expense_detail", expense_id=expense_id))

    @app.post("/admin/expenses/<expense_id>/allocations/auto")
    def auto_expense_allocations(expense_id):
        Session = make_session_factory()
        with Session() as session:
            expense = session.get(Expense, expense_id)
            if expense is None:
                abort(404)
            refresh_auto_expense_allocations(session, [expense], force=True)
            session.commit()

        flash("Автоматическое разнесение пересчитано", "success")
        return redirect(url_for("expense_detail", expense_id=expense_id))

    @app.get("/admin/incomes")
    def incomes():
        db_error = None
        rows = []
        categories = []
        total = Decimal("0")
        as_of = None

        try:
            Session = make_session_factory()
            with Session() as session:
                categories = session.execute(
                    select(
                        Income.income_category,
                        func.count(Income.id),
                        func.coalesce(func.sum(Income.amount), 0),
                    )
                    .group_by(Income.income_category)
                    .order_by(Income.income_category)
                ).all()
                rows = session.scalars(
                    select(Income).order_by(Income.date.desc(), Income.number.desc(), Income.id)
                ).all()
                total = sum((Decimal(row.amount or 0) for row in rows), Decimal("0"))
                as_of = session.scalar(select(func.max(Income.synced_at)))
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "incomes.html",
            rows=rows,
            categories=categories,
            total=total,
            as_of=as_of,
            db_error=db_error,
        )

    @app.get("/admin/expenses/report.xlsx")
    def expense_report_xlsx():
        filters = expense_filter_values(request.args)
        rows, stats, _categories, as_of = load_expense_report(filters)
        report = render_expenses_xlsx(rows, stats, filters, as_of)
        report_date = (as_of or datetime.now()).strftime("%Y-%m-%d")
        return send_file(
            report,
            mimetype=XLSX_CONTENT_TYPE,
            as_attachment=True,
            download_name=f"expenses-{report_date}.xlsx",
        )

    @app.get("/admin/expenses/monthly")
    def monthly_expenses():
        db_error = None
        rows = []
        latest = None
        as_of = None
        stats = {
            "months": 0,
            "total": Decimal("0"),
            "average": Decimal("0"),
            "operations": 0,
        }

        try:
            rows, latest, stats, as_of = load_monthly_expense_report()
        except Exception as exc:
            db_error = str(exc)

        return render_template(
            "monthly_expenses.html",
            rows=rows,
            latest=latest,
            stats=stats,
            as_of=as_of,
            db_error=db_error,
        )

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
            claim_count=sum(len(row["plots"]) for row in rows),
            whatsapp_message=DEFAULT_MESSAGE_TEMPLATE,
            whatsapp_debt_message=DEFAULT_DEBT_REMINDER_TEMPLATE,
            db_error=db_error,
        )

    @app.get("/admin/payments/report.xlsx")
    def payment_report_xlsx():
        scope = request.args.get("scope", "all").strip().lower()
        if scope not in {"all", "stopped"}:
            abort(400)

        rows, stats, as_of, cutoff = load_payment_status_report(scope=scope)
        report = render_payment_status_xlsx(
            rows,
            stats,
            as_of,
            cutoff,
            scope=scope,
        )
        report_date = as_of.strftime("%Y-%m-%d")
        report_scope = "all" if scope == "all" else "stopped"
        return send_file(
            report,
            mimetype=XLSX_CONTENT_TYPE,
            as_attachment=True,
            download_name=f"payment-report-{report_scope}-{report_date}.xlsx",
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
                    "charge_basis": (
                        env("TSN_CLAIM_BASIS", "").strip()
                        or DEFAULT_CLAIM_BASIS
                    ),
                }
                claim_defaults["whatsapp_phone"] = first_whatsapp_phone(
                    split_phones(details["phone"])
                )
                claim_defaults["whatsapp_message"] = DEFAULT_MESSAGE_TEMPLATE
                claim_defaults["whatsapp_debt_message"] = DEFAULT_DEBT_REMINDER_TEMPLATE

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

    @app.post("/admin/plots/<owner_plot_id>/pretrial-claim.pdf")
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
                    or DEFAULT_CLAIM_BASIS
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
            try:
                document = render_pretrial_claim_pdf(values)
            except ClaimPdfConversionError:
                app.logger.exception("Не удалось сформировать PDF досудебной претензии")
                abort(503, description="Не удалось сформировать PDF претензии")
            session.commit()

        return send_file(
            document,
            mimetype=PDF_CONTENT_TYPE,
            as_attachment=True,
            download_name=safe_claim_filename(
                details["plot_number"],
                claim_date,
                issued_claim.number,
                extension="pdf",
            ),
            max_age=0,
        )


    @app.post("/admin/plots/<owner_plot_id>/whatsapp")
    def plot_whatsapp_claim(owner_plot_id):
        batch_id = f"single-{uuid.uuid4().hex}"
        phone = request.form.get("whatsapp_phone", "")
        message_template = request.form.get("whatsapp_message", DEFAULT_MESSAGE_TEMPLATE)
        Session = make_session_factory()
        try:
            with Session() as session:
                queued = enqueue_whatsapp_claim(
                    session,
                    owner_plot_id,
                    request.form,
                    phone,
                    message_template,
                    batch_id,
                )
                session.commit()
        except LookupError:
            abort(404)
        except ValueError as exc:
            flash(f"Не удалось поставить претензию в очередь WhatsApp: {exc}", "error")
            return redirect(url_for("plot_detail", owner_plot_id=owner_plot_id))
        except Exception as exc:
            app.logger.exception("Не удалось создать отправку WhatsApp")
            flash(f"Не удалось поставить претензию в очередь WhatsApp: {exc}", "error")
            return redirect(url_for("plot_detail", owner_plot_id=owner_plot_id))

        flash(
            f"Претензия исх. № {queued.claim_number} поставлена в очередь WhatsApp на +{queued.phone}",
            "success",
        )
        return redirect(url_for("whatsapp_admin", batch_id=batch_id))

    @app.post("/admin/plots/<owner_plot_id>/whatsapp-message")
    def plot_whatsapp_message(owner_plot_id):
        batch_id = f"message-single-{uuid.uuid4().hex}"
        phone = request.form.get("whatsapp_phone", "")
        message_template = (
            request.form.get("whatsapp_debt_message")
            or DEFAULT_DEBT_REMINDER_TEMPLATE
        )
        Session = make_session_factory()
        try:
            with Session() as session:
                details = session.execute(
                    select(
                        OwnerPlot.id,
                        OwnerPlot.owner,
                        OwnerPlot.plot_number,
                        Balance.total,
                    )
                    .outerjoin(Balance, Balance.owner_plot_id == OwnerPlot.id)
                    .where(OwnerPlot.id == owner_plot_id)
                ).mappings().first()
                if details is None:
                    raise LookupError(owner_plot_id)
                queued = enqueue_whatsapp_debt_message(
                    session,
                    {
                        "owner": details["owner"],
                        "plots": [
                            {
                                "owner_plot_id": details["id"],
                                "plot_number": details["plot_number"],
                            }
                        ],
                        "total_debt": details["total"],
                    },
                    phone,
                    message_template,
                    batch_id,
                )
                session.commit()
        except LookupError:
            abort(404)
        except ValueError as exc:
            flash(f"Не удалось поставить сообщение в очередь WhatsApp: {exc}", "error")
            return redirect(url_for("plot_detail", owner_plot_id=owner_plot_id))
        except Exception as exc:
            app.logger.exception("Не удалось создать отправку сообщения WhatsApp")
            flash(f"Не удалось поставить сообщение в очередь WhatsApp: {exc}", "error")
            return redirect(url_for("plot_detail", owner_plot_id=owner_plot_id))

        flash(
            f"Сообщение по участку № {queued.plot_number} поставлено в очередь WhatsApp на +{queued.phone}",
            "success",
        )
        return redirect(url_for("whatsapp_admin", batch_id=batch_id))

    @app.post("/admin/debtors/whatsapp")
    def debtors_whatsapp():
        min_months, min_months_value = parse_min_months(request.form.get("months_from", "1"))
        max_months, max_months_value = parse_max_months(request.form.get("months_to", ""))
        redirect_arguments = {"months_from": min_months, "months_to": max_months}
        if request.form.get("confirm") != "yes":
            flash("Для массовой отправки подтвердите постановку документов в очередь", "error")
            return redirect(url_for("debtors", **redirect_arguments))

        try:
            rows, _stats, _phones = load_debtors_report(min_months_value, max_months_value)
            message_template = request.form.get("whatsapp_message", DEFAULT_MESSAGE_TEMPLATE)
            format_whatsapp_message(
                message_template,
                owner="Собственник",
                plot_number="0",
                claim_number=0,
                debt="12 345 руб. 67 коп.",
            )
            batch_id = f"bulk-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            queued_count = 0
            skipped = []
            Session = make_session_factory()
            with Session() as session:
                for row in rows:
                    phone = first_whatsapp_phone(row["phones"])
                    for plot in row["plots"]:
                        if not phone:
                            skipped.append(f"участок {plot['plot_number']}: нет корректного телефона")
                            continue
                        try:
                            with session.begin_nested():
                                enqueue_whatsapp_claim(
                                    session,
                                    plot["owner_plot_id"],
                                    {},
                                    phone,
                                    message_template,
                                    batch_id,
                                )
                            queued_count += 1
                        except (LookupError, ValueError) as exc:
                            skipped.append(f"участок {plot['plot_number']}: {exc}")
                session.commit()
        except Exception as exc:
            app.logger.exception("Не удалось создать массовую отправку WhatsApp")
            flash(f"Не удалось создать массовую отправку WhatsApp: {exc}", "error")
            return redirect(url_for("debtors", **redirect_arguments))

        if queued_count:
            text = f"В очередь WhatsApp добавлено документов: {queued_count}"
            if skipped:
                examples = "; ".join(skipped[:3])
                suffix = f"; и ещё {len(skipped) - 3}" if len(skipped) > 3 else ""
                text += f". Пропущено: {len(skipped)} ({examples}{suffix})"
            flash(text, "success")
            return redirect(url_for("whatsapp_admin", batch_id=batch_id))

        details = "; ".join(skipped[:5])
        if len(skipped) > 5:
            details += f"; и ещё {len(skipped) - 5}"
        flash(f"В очередь ничего не добавлено. {details}", "error")
        return redirect(url_for("debtors", **redirect_arguments))

    @app.post("/admin/debtors/whatsapp-message")
    def debtors_whatsapp_message():
        min_months, min_months_value = parse_min_months(request.form.get("months_from", "1"))
        max_months, max_months_value = parse_max_months(request.form.get("months_to", ""))
        redirect_arguments = {"months_from": min_months, "months_to": max_months}
        if request.form.get("confirm") != "yes":
            flash("Для рассылки подтвердите постановку сообщений в очередь", "error")
            return redirect(url_for("debtors", **redirect_arguments))

        try:
            rows, _stats, _phones = load_debtors_report(min_months_value, max_months_value)
            message_template = (
                request.form.get("whatsapp_debt_message")
                or DEFAULT_DEBT_REMINDER_TEMPLATE
            )
            format_whatsapp_message(
                message_template,
                owner="Собственник",
                plot_number="10",
                claim_number="",
                debt="12 345 руб. 67 коп.",
            )
            batch_id = f"message-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            queued_count = 0
            skipped = []
            Session = make_session_factory()
            with Session() as session:
                for row in rows:
                    phone = first_whatsapp_phone(row["phones"])
                    if not phone:
                        skipped.append(f"{row['owner']}: нет корректного телефона")
                        continue
                    try:
                        with session.begin_nested():
                            enqueue_whatsapp_debt_message(
                                session,
                                row,
                                phone,
                                message_template,
                                batch_id,
                            )
                        queued_count += 1
                    except ValueError as exc:
                        skipped.append(f"{row['owner']}: {exc}")
                session.commit()
        except Exception as exc:
            app.logger.exception("Не удалось создать рассылку WhatsApp")
            flash(f"Не удалось создать рассылку WhatsApp: {exc}", "error")
            return redirect(url_for("debtors", **redirect_arguments))

        if queued_count:
            text = f"В очередь WhatsApp добавлено сообщений: {queued_count}"
            if skipped:
                examples = "; ".join(skipped[:3])
                suffix = f"; и ещё {len(skipped) - 3}" if len(skipped) > 3 else ""
                text += f". Пропущено: {len(skipped)} ({examples}{suffix})"
            flash(text, "success")
            return redirect(url_for("whatsapp_admin", batch_id=batch_id))

        details = "; ".join(skipped[:5])
        if len(skipped) > 5:
            details += f"; и ещё {len(skipped) - 5}"
        flash(f"В очередь ничего не добавлено. {details}", "error")
        return redirect(url_for("debtors", **redirect_arguments))

    @app.get("/admin/whatsapp")
    def whatsapp_admin():
        db_error = None
        messages = []
        stats = {}
        batch_id = request.args.get("batch_id", "").strip()
        connection_status = {
            "state": "unavailable",
            "error": "Сервис WhatsApp не отвечает",
            "qr_data_url": None,
            "account": None,
        }
        try:
            connection_status = WhatsAppClient(timeout=5).status()
        except WhatsAppServiceError as exc:
            connection_status["error"] = str(exc)
        try:
            Session = make_session_factory()
            with Session() as session:
                statement = select(WhatsAppMessage).order_by(
                    WhatsAppMessage.created_at.desc(), WhatsAppMessage.id.desc()
                )
                if batch_id:
                    statement = statement.where(WhatsAppMessage.batch_id == batch_id)
                messages = session.scalars(statement.limit(100)).all()
                stats = whatsapp_queue_stats(session)
        except Exception as exc:
            db_error = str(exc)
        return render_template(
            "whatsapp.html",
            messages=messages,
            stats=stats,
            batch_id=batch_id,
            db_error=db_error,
            connection_status=connection_status,
        )

    @app.get("/admin/telegram")
    def telegram_admin():
        Session = make_session_factory()
        with Session() as session:
            requests_rows = session.scalars(
                select(TelegramLinkRequest)
                .order_by(
                    (TelegramLinkRequest.status == "pending").desc(),
                    TelegramLinkRequest.created_at.desc(),
                    TelegramLinkRequest.id.desc(),
                )
                .limit(200)
            ).all()
            owner_plots = session.scalars(
                select(OwnerPlot).order_by(OwnerPlot.owner, OwnerPlot.plot_number)
            ).all()
            bindings = session.scalars(
                select(TelegramBinding)
                .where(TelegramBinding.enabled.is_(True))
                .order_by(TelegramBinding.owner, TelegramBinding.telegram_user_id)
            ).all()

        owner_plot_options = []
        for owner_plot in owner_plots:
            phone = normalize_phone(owner_plot.phone)
            label = f"{owner_plot.owner} — участок {owner_plot.plot_number}"
            if phone:
                label += f" — +{phone}"
            owner_plot_options.append({"row": owner_plot, "label": label})

        suggestions = {}
        for link_request in requests_rows:
            best_id = None
            best_score = 0
            request_name = normalize_text(link_request.full_name)
            request_plot = normalize_text(link_request.plot_number)
            request_phone = normalize_phone(link_request.phone)
            for option in owner_plot_options:
                owner_plot = option["row"]
                score = 0
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
                if score > best_score:
                    best_score = score
                    best_id = owner_plot.id
            suggestions[link_request.id] = best_id if best_score >= 5 else None

        return render_template(
            "telegram.html",
            requests_rows=requests_rows,
            owner_plot_options=owner_plot_options,
            suggestions=suggestions,
            bindings=bindings,
        )

    @app.post("/admin/telegram/requests/<int:request_id>/approve")
    def telegram_approve(request_id):
        owner_plot_id = request.form.get("owner_plot_id", "").strip()
        Session = make_session_factory()
        with Session() as session:
            link_request = session.get(TelegramLinkRequest, request_id)
            if link_request is None:
                abort(404)
            if link_request.status != "pending":
                flash("Эта заявка уже обработана", "error")
                return redirect(url_for("telegram_admin"))
            owner_plot = session.get(OwnerPlot, owner_plot_id)
            if owner_plot is None:
                flash("Выберите владельца и участок для привязки", "error")
                return redirect(url_for("telegram_admin"))

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
            link_request.review_comment = request.form.get("review_comment", "").strip()[:4000]
            link_request.reviewed_at = now
            profile = session.get(TelegramProfile, link_request.telegram_user_id)
            if profile is not None:
                profile.state = "idle"
                profile.updated_at = now
            session.commit()

        notification_error = None
        telegram_token = env("TELEGRAM_BOT_TOKEN", "")
        if telegram_token:
            try:
                from .telegram_bot import MENU_MARKUP, TelegramBotClient

                TelegramBotClient(token=telegram_token).send_message(
                    link_request.chat_id,
                    "Заявка одобрена. Теперь задолженность доступна в меню.",
                    MENU_MARKUP,
                )
            except Exception as exc:
                app.logger.exception("Не удалось уведомить об одобрении Telegram-заявки")
                notification_error = str(exc)

        flash(
            f"Telegram @{link_request.username or link_request.telegram_user_id} привязан к {owner_plot.owner}",
            "success",
        )
        if notification_error:
            flash(f"Привязка сохранена, но уведомить пользователя не удалось: {notification_error}", "error")
        return redirect(url_for("telegram_admin"))

    @app.post("/admin/telegram/requests/<int:request_id>/reject")
    def telegram_reject(request_id):
        Session = make_session_factory()
        with Session() as session:
            link_request = session.get(TelegramLinkRequest, request_id)
            if link_request is None:
                abort(404)
            if link_request.status != "pending":
                flash("Эта заявка уже обработана", "error")
                return redirect(url_for("telegram_admin"))
            now = now_utc_naive()
            link_request.status = "rejected"
            link_request.review_comment = request.form.get("review_comment", "").strip()[:4000]
            link_request.reviewed_at = now
            profile = session.get(TelegramProfile, link_request.telegram_user_id)
            if profile is not None:
                profile.state = "idle"
                profile.updated_at = now
            session.commit()

        notification_error = None
        telegram_token = env("TELEGRAM_BOT_TOKEN", "")
        if telegram_token:
            try:
                from .telegram_bot import MENU_MARKUP, TelegramBotClient

                reason = f"\nПричина: {link_request.review_comment}" if link_request.review_comment else ""
                TelegramBotClient(token=telegram_token).send_message(
                    link_request.chat_id,
                    "Заявка на привязку отклонена. Отправьте /start, чтобы заполнить её заново."
                    + reason,
                    MENU_MARKUP,
                )
            except Exception as exc:
                app.logger.exception("Не удалось уведомить об отклонении Telegram-заявки")
                notification_error = str(exc)

        flash("Заявка Telegram отклонена", "success")
        if notification_error:
            flash(f"Решение сохранено, но уведомить пользователя не удалось: {notification_error}", "error")
        return redirect(url_for("telegram_admin"))

    @app.get("/admin/whatsapp/status")
    def whatsapp_status():
        try:
            return WhatsAppClient().status()
        except WhatsAppServiceError as exc:
            return {"state": "unavailable", "error": str(exc), "qr_data_url": None, "account": None}

    @app.post("/admin/whatsapp/logout")
    def whatsapp_logout():
        try:
            WhatsAppClient().logout()
            flash("Сессия WhatsApp завершена. Появится новый QR-код.", "success")
        except WhatsAppServiceError as exc:
            flash(f"Не удалось завершить сессию WhatsApp: {exc}", "error")
        return redirect(url_for("whatsapp_admin"))

    @app.post("/admin/whatsapp/messages/<int:message_id>/retry")
    def whatsapp_retry(message_id):
        Session = make_session_factory()
        with Session() as session:
            message = session.get(WhatsAppMessage, message_id)
            if message is None:
                abort(404)
            if message.status != "failed":
                flash("Повторить можно только неуспешную отправку", "error")
            else:
                message.status = "queued"
                message.error_text = None
                message.started_at = None
                session.commit()
                flash(f"Отправка #{message.id} возвращена в очередь", "success")
        return redirect(url_for("whatsapp_admin", batch_id=request.form.get("batch_id", "")))

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

    from .community_web import register_community_routes
    from .telegram_receipt_web import register_telegram_receipt_routes

    register_community_routes(app)
    register_telegram_receipt_routes(app)

    return app


def run_server():
    app = create_app()
    app.run(
        host=env("FLASK_HOST", "127.0.0.1"),
        port=int(env("FLASK_PORT", "8080")),
        debug=env("FLASK_DEBUG", "false").lower() in {"1", "true", "yes", "y"},
    )
