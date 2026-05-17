from sqlalchemy import delete

from .db import make_session_factory, upsert_many
from .models import Accrual, Balance, MessengerBinding, Owner, OwnerPlot, Payment, Plot, SyncRun
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


def sync_plots(session, snapshot, run_id, synced_at):
    rows = [
        {
            "id": text(item.get("id")),
            "plot_number": text(item.get("plot_number")),
            "account": text(item.get("account")),
            "address": text(item.get("address")),
            "organization_id": text(item.get("organization_id")),
            "organization": text(item.get("organization")),
            "sync_run_id": run_id,
            "synced_at": synced_at,
        }
        for item in snapshot.get("plots", [])
        if item.get("id")
    ]
    upsert_many(session, Plot, rows)


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


def delete_stale_rows(session, run_id):
    for model in reversed(SYNC_MODELS):
        session.execute(delete(model).where(model.sync_run_id != run_id))


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
            delete_stale_rows(session, run_id)
            finish_sync_run(session, run_id, "ok", generated_at=generated_at)
            session.commit()
        except Exception as exc:
            finish_sync_run(session, run_id, "error", generated_at=generated_at, error_text=str(exc))
            session.commit()
            raise

    return {
        "run_id": run_id,
        "generated_at": snapshot.get("generated_at"),
        "counts": {model.__tablename__: len(snapshot.get(model.__tablename__, [])) for model in SYNC_MODELS},
    }
