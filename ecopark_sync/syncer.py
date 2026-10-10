from collections import defaultdict

from sqlalchemy import delete

from .db import make_session_factory, upsert_many
from .expense_allocations import (
    money,
    normalize_receipt_items,
    refresh_auto_expense_allocations_for_ids,
    serialize_receipt_items,
)
from .models import Accrual, Balance, Expense, Income, MessengerBinding, Owner, OwnerPlot, Payment, Plot, SyncRun
from .utils import now_utc_naive, parse_datetime, parse_decimal, text


SYNC_MODELS = (
    Owner,
    Plot,
    OwnerPlot,
    Balance,
    MessengerBinding,
    Payment,
    Accrual,
)


def insert_sync_run(session):
    run = SyncRun(started_at=now_utc_naive(), status="running")
    session.add(run)
    session.flush()
    return run.id


def finish_sync_run(session, run_id, status, generated_at=None, error_text=None):
    run = session.get(SyncRun, run_id)
    run.finished_at = now_utc_naive()
    run.source_generated_at = generated_at
    run.status = status
    run.error_text = error_text


def record_failed_run(error_text):
    Session = make_session_factory()
    with Session() as session:
        run_id = insert_sync_run(session)
        finish_sync_run(session, run_id, "error", error_text=error_text)
        session.commit()
        return run_id


def sync_owners(session, snapshot, run_id, synced_at):
    rows = [
        {
            "id": text(item.get("id")),
            "name": text(item.get("name")),
            "phone": text(item.get("phone")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("owners", [])
        if item.get("id")
    ]
    upsert_many(session, Owner, rows)


def clean_plot_address(value):
    return text(value).replace("\\", "").replace('"', "").strip()


def sync_plots(session, snapshot, run_id, synced_at):
    rows = [
        {
            "id": text(item.get("id")),
            "plot_number": text(item.get("plot_number")),
            "account": text(item.get("account")),
            "address": clean_plot_address(item.get("address")),
            "organization_id": text(item.get("organization_id")),
            "organization": text(item.get("organization")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("plots", [])
        if item.get("id")
    ]
    upsert_many(session, Plot, rows, preserve_columns=("cadastral_number",))


def sync_owner_plots(session, snapshot, run_id, synced_at):
    rows = [
        {
            "id": text(item.get("id")),
            "owner_id": text(item.get("owner_id")),
            "owner": text(item.get("owner")),
            "phone": text(item.get("phone")),
            "plot_id": text(item.get("plot_id")),
            "plot_number": text(item.get("plot_number")),
            "account": text(item.get("account")),
            "presentation": text(item.get("presentation")),
            "organization_id": text(item.get("organization_id")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("owner_plots", [])
        if item.get("id")
    ]
    upsert_many(session, OwnerPlot, rows)


def sync_balances(session, snapshot, run_id, synced_at):
    rows = [
        {
            "owner_plot_id": text(item.get("owner_plot_id")),
            "owner_id": text(item.get("owner_id")),
            "plot_id": text(item.get("plot_id")),
            "plot_number": text(item.get("plot_number")),
            "account": text(item.get("account")),
            "debt": parse_decimal(item.get("debt")),
            "penalty": parse_decimal(item.get("penalty")),
            "overpayment": parse_decimal(item.get("overpayment")),
            "total": parse_decimal(item.get("total")),
            "currency": text(item.get("currency") or "RUB"),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("balances", [])
        if item.get("owner_plot_id")
    ]
    upsert_many(session, Balance, rows, key_columns=("owner_plot_id",))


def sync_messenger_bindings(session, snapshot, run_id, synced_at):
    rows = [
        {
            "owner_id": text(item.get("owner_id")),
            "messenger": text(item.get("messenger")),
            "external_id": text(item.get("external_id")),
            "owner": text(item.get("owner")),
            "enabled": bool(item.get("enabled")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("messenger_bindings", [])
        if item.get("owner_id") and item.get("messenger") and item.get("external_id")
    ]
    upsert_many(session, MessengerBinding, rows, key_columns=("owner_id", "messenger", "external_id"))


def sync_payments(session, snapshot, run_id, synced_at):
    rows = [
        {
            "id": text(item.get("id")),
            "document_id": text(item.get("document_id")),
            "document": text(item.get("document")),
            "date": parse_datetime(item.get("date")),
            "number": text(item.get("number")),
            "owner_plot_id": text(item.get("owner_plot_id")),
            "amount": parse_decimal(item.get("amount")),
            "payment_type": text(item.get("payment_type")),
            "incoming_number": text(item.get("incoming_number")),
            "incoming_date": parse_datetime(item.get("incoming_date")),
            "registry_file": text(item.get("registry_file")),
            "source": text(item.get("source")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("payments", [])
        if item.get("id")
    ]
    upsert_many(session, Payment, rows)


def sync_accruals(session, snapshot, run_id, synced_at):
    rows = [
        {
            "id": text(item.get("id")),
            "document_id": text(item.get("document_id")),
            "document": text(item.get("document")),
            "date": parse_datetime(item.get("date")),
            "number": text(item.get("number")),
            "owner_plot_id": text(item.get("owner_plot_id")),
            "contribution_id": text(item.get("contribution_id")),
            "contribution": text(item.get("contribution")),
            "amount": parse_decimal(item.get("amount")),
            "quantity": parse_decimal(item.get("quantity")),
            "source": text(item.get("source")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("accruals", [])
        if item.get("id")
    ]
    upsert_many(session, Accrual, rows)


def linked_advance_report_items(snapshot):
    expenses_by_document = defaultdict(list)
    for expense in snapshot.get("expenses", []):
        document = text(expense.get("document"))
        if document and expense.get("document_id"):
            expenses_by_document[document].append(expense)

    documents_by_key = defaultdict(set)
    for advance in snapshot.get("advance_report_advances", []):
        report_key = text(advance.get("report_key"))
        document = text(advance.get("document"))
        matching_expenses = expenses_by_document.get(document, [])
        if not report_key or len(matching_expenses) != 1:
            continue
        expense = matching_expenses[0]
        key = (report_key, parse_decimal(expense.get("amount")))
        documents_by_key[key].add(text(expense.get("document_id")))

    items_by_key = defaultdict(list)
    for item in snapshot.get("advance_report_items", []):
        report_key = text(item.get("report_key"))
        if not report_key:
            continue
        key = (report_key, parse_decimal(item.get("amount")))
        items_by_key[key].append(item)

    linked = defaultdict(list)
    for key, items in items_by_key.items():
        document_ids = documents_by_key.get(key, set())
        if len(items) != 1 or len(document_ids) != 1:
            continue
        item = dict(items[0])
        report_key = text(item.pop("report_key", ""))
        receipt_line = text(item.get("receipt_line"))
        item.setdefault(
            "receipt_id",
            f"{report_key}:{receipt_line}",
        )
        item.setdefault("advance_report", report_key)
        linked[next(iter(document_ids))].append(item)
    return linked


def normalized_person_name(value):
    return " ".join(text(value).lower().replace("ё", "е").split())


def proportional_amounts(total, weighted_items):
    total = max(money(total), money(0))
    weights = [
        (key, max(parse_decimal(weight), parse_decimal(0)))
        for key, weight in weighted_items
        if parse_decimal(weight) > 0
    ]
    weight_total = sum((weight for _key, weight in weights), parse_decimal(0))
    if total <= 0 or weight_total <= 0:
        return {}
    amounts = {
        key: money(total * weight / weight_total)
        for key, weight in weights
    }
    difference = total - sum(amounts.values(), money(0))
    if difference:
        largest_key = max(weights, key=lambda item: item[1])[0]
        amounts[largest_key] += difference
    return amounts


def payroll_item(employee, amount, kind, source, period=""):
    label = "Налог с зарплаты" if kind == "payroll_tax" else "Зарплата"
    return {
        "name": f"{label}: {employee}",
        "employee": employee,
        "amount": str(money(amount)),
        "kind": kind,
        "source": source,
        "payroll_period": period,
    }


def linked_payroll_statement_items(snapshot):
    expenses_by_document_id = {
        text(expense.get("document_id")): expense
        for expense in snapshot.get("expenses", [])
        if expense.get("document_id")
    }
    statement_items = defaultdict(lambda: defaultdict(lambda: {
        "employee": "",
        "amount": parse_decimal(0),
        "period": "",
    }))
    for item in snapshot.get("payroll_statement_items", []):
        statement_id = text(item.get("statement_id"))
        employee = text(item.get("employee")).strip()
        employee_key = normalized_person_name(employee)
        if not statement_id or not employee_key:
            continue
        summary = statement_items[statement_id][employee_key]
        summary["employee"] = employee
        summary["amount"] += parse_decimal(item.get("amount"))
        summary["period"] = text(item.get("period"))

    links = {}
    for link in snapshot.get("payroll_expense_links", []):
        document_id = text(link.get("document_id"))
        statement_id = text(link.get("statement_id"))
        if not document_id or not statement_id or document_id not in expenses_by_document_id:
            continue
        key = (document_id, statement_id)
        current = links.setdefault(
            key,
            {
                "document_id": document_id,
                "statement_id": statement_id,
                "employee": "",
            },
        )
        employee = text(link.get("employee")).strip()
        if employee:
            current["employee"] = employee

    for link in links.values():
        if link["employee"]:
            continue
        expense = expenses_by_document_id[link["document_id"]]
        expense_text = normalized_person_name(
            " ".join(
                text(expense.get(key))
                for key in ("counterparty", "purpose")
            )
        )
        matches = [
            employee_data["employee"]
            for employee_key, employee_data in statement_items.get(
                link["statement_id"], {}
            ).items()
            if employee_key and employee_key in expense_text
        ]
        if len(matches) == 1:
            link["employee"] = matches[0]

    links_by_statement = defaultdict(list)
    for link in links.values():
        links_by_statement[link["statement_id"]].append(link)

    linked = defaultdict(list)
    for statement_id, statement_links in links_by_statement.items():
        employees = statement_items.get(statement_id, {})
        if not employees:
            continue
        direct_employee_keys = {
            normalized_person_name(link["employee"])
            for link in statement_links
            if link["employee"]
        }
        for link in statement_links:
            if not link["employee"]:
                continue
            expense = expenses_by_document_id[link["document_id"]]
            employee_key = normalized_person_name(link["employee"])
            employee_data = employees.get(employee_key, {})
            employee = employee_data.get("employee") or link["employee"]
            linked[link["document_id"]].append(
                payroll_item(
                    employee,
                    expense.get("amount"),
                    "payroll",
                    "Зарплатная ведомость 1С",
                    employee_data.get("period", ""),
                )
            )

        collective_links = [link for link in statement_links if not link["employee"]]
        remaining = [
            (employee_key, employee_data)
            for employee_key, employee_data in employees.items()
            if employee_key not in direct_employee_keys
        ]
        if len(collective_links) != 1 or not remaining:
            continue
        link = collective_links[0]
        expense = expenses_by_document_id[link["document_id"]]
        amounts = proportional_amounts(
            expense.get("amount"),
            [
                (employee_key, employee_data["amount"])
                for employee_key, employee_data in remaining
            ],
        )
        for employee_key, employee_data in remaining:
            amount = amounts.get(employee_key, money(0))
            if amount <= 0:
                continue
            linked[link["document_id"]].append(
                payroll_item(
                    employee_data["employee"],
                    amount,
                    "payroll",
                    "Зарплатная ведомость 1С",
                    employee_data["period"],
                )
            )
    return linked


def linked_payroll_tax_items(snapshot):
    expenses_by_document_id = {
        text(expense.get("document_id")): expense
        for expense in snapshot.get("expenses", [])
        if expense.get("document_id")
    }
    weights = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: parse_decimal(0))))
    employee_names = {}
    for item in snapshot.get("payroll_accrual_taxes", []):
        period = text(item.get("period"))[:7]
        kind = text(item.get("kind"))
        employee = text(item.get("employee")).strip()
        employee_key = normalized_person_name(employee)
        amount = parse_decimal(item.get("amount"))
        if not period or not kind or not employee_key or amount <= 0:
            continue
        employee_names[employee_key] = employee
        weights[period][kind][employee_key] += amount

    linked = defaultdict(list)
    for tax_expense in snapshot.get("payroll_tax_expenses", []):
        document_id = text(tax_expense.get("document_id"))
        expense = expenses_by_document_id.get(document_id)
        if expense is None:
            continue
        period = text(tax_expense.get("tax_period"))[:7]
        period_weights = weights.get(period, {})
        tax_text = " ".join(
            text(tax_expense.get(key)).lower()
            for key in ("tax", "purpose", "expense_category")
        )
        kinds = (
            ("accident_insurance",)
            if "несчаст" in tax_text
            else ("income_tax", "insurance")
        )
        employee_weights = defaultdict(lambda: parse_decimal(0))
        for kind in kinds:
            for employee_key, amount in period_weights.get(kind, {}).items():
                employee_weights[employee_key] += amount
        amounts = proportional_amounts(
            expense.get("amount"),
            list(employee_weights.items()),
        )
        for employee_key, amount in amounts.items():
            if amount <= 0:
                continue
            linked[document_id].append(
                payroll_item(
                    employee_names[employee_key],
                    amount,
                    "payroll_tax",
                    text(tax_expense.get("tax")) or "Налоги по зарплате",
                    period,
                )
            )
    return linked


def linked_payroll_items(snapshot):
    linked = linked_payroll_statement_items(snapshot)
    for document_id, items in linked_payroll_tax_items(snapshot).items():
        linked[document_id].extend(items)
    return linked


def sync_expenses(session, snapshot, run_id, synced_at):
    advance_items = linked_advance_report_items(snapshot)
    payroll_items = linked_payroll_items(snapshot)
    rows = [
        {
            "id": text(item.get("id")),
            "document_id": text(item.get("document_id")),
            "document": text(item.get("document")),
            "date": parse_datetime(item.get("date")),
            "number": text(item.get("number")),
            "expense_category_id": text(item.get("expense_category_id")),
            "expense_category": text(item.get("expense_category")),
            "counterparty_id": text(item.get("counterparty_id")),
            "counterparty": text(item.get("counterparty")),
            "purpose": text(item.get("purpose")),
            "receipt_items_json": serialize_receipt_items(
                normalize_receipt_items(item.get("receipt_items"))
                + advance_items.get(text(item.get("document_id")), [])
                + payroll_items.get(text(item.get("document_id")), [])
            ),
            "amount": parse_decimal(item.get("amount")),
            "currency": text(item.get("currency") or "RUB"),
            "organization_id": text(item.get("organization_id")),
            "organization": text(item.get("organization")),
            "source": text(item.get("source")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("expenses", [])
        if item.get("id")
    ]
    upsert_many(session, Expense, rows, preserve_columns=("allocation_mode",))
    refresh_auto_expense_allocations_for_ids(
        session,
        [row["id"] for row in rows],
    )


def sync_incomes(session, snapshot, run_id, synced_at):
    rows = [
        {
            "id": text(item.get("id")),
            "document_id": text(item.get("document_id")),
            "document": text(item.get("document")),
            "date": parse_datetime(item.get("date")),
            "number": text(item.get("number")),
            "income_category": text(item.get("income_category")),
            "counterparty_id": text(item.get("counterparty_id")),
            "counterparty": text(item.get("counterparty")),
            "purpose": text(item.get("purpose")),
            "amount": parse_decimal(item.get("amount")),
            "currency": text(item.get("currency") or "RUB"),
            "organization_id": text(item.get("organization_id")),
            "organization": text(item.get("organization")),
            "source": text(item.get("source")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("incomes", [])
        if item.get("id")
    ]
    upsert_many(session, Income, rows)


def delete_stale_rows(session, run_id, models=SYNC_MODELS):
    for model in reversed(models):
        session.execute(delete(model).where(model.sync_run_id != run_id))


def synced_models_for_snapshot(snapshot):
    models = list(SYNC_MODELS)
    if "incomes" in snapshot:
        models.append(Income)
    if "expenses" in snapshot:
        models.append(Expense)
    return tuple(models)


def sync_snapshot(snapshot):
    generated_at = parse_datetime(snapshot.get("generated_at"))
    synced_at = now_utc_naive()

    Session = make_session_factory()
    with Session() as session:
        run_id = insert_sync_run(session)
        try:
            sync_owners(session, snapshot, run_id, synced_at)
            sync_plots(session, snapshot, run_id, synced_at)
            sync_owner_plots(session, snapshot, run_id, synced_at)
            sync_balances(session, snapshot, run_id, synced_at)
            sync_messenger_bindings(session, snapshot, run_id, synced_at)
            sync_payments(session, snapshot, run_id, synced_at)
            sync_accruals(session, snapshot, run_id, synced_at)
            if "incomes" in snapshot:
                sync_incomes(session, snapshot, run_id, synced_at)
            if "expenses" in snapshot:
                sync_expenses(session, snapshot, run_id, synced_at)
            delete_stale_rows(session, run_id, synced_models_for_snapshot(snapshot))
            finish_sync_run(session, run_id, "ok", generated_at=generated_at)
            session.commit()
        except Exception as exc:
            finish_sync_run(session, run_id, "error", generated_at=generated_at, error_text=str(exc))
            session.commit()
            raise

    counts = {model.__tablename__: len(snapshot.get(model.__tablename__, [])) for model in SYNC_MODELS}
    if "incomes" in snapshot:
        counts["incomes"] = len(snapshot["incomes"])
    if "expenses" in snapshot:
        counts["expenses"] = len(snapshot["expenses"])

    return {
        "run_id": run_id,
        "generated_at": snapshot.get("generated_at"),
        "counts": counts,
    }
