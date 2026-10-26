from sqlalchemy import inspect, select, text
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from .db import get_engine
from .expense_allocations import (
    BUDGET_CATEGORIES,
    DEFAULT_PURPOSE_RULES,
    refresh_auto_expense_allocations,
)
from .models import (
    Base,
    BotSettings,
    CommunityMessage,
    CommunityUser,
    DirectoryEntry,
    Expense,
    ExpenseAllocation,
    ExpenseAllocationRule,
    ExpenseBudgetCategory,
    NotificationDelivery,
    NotificationDestination,
    NotificationSource,
    NotificationSubscription,
    NotificationType,
    OutageEvent,
    ReputationEvent,
    TelegramBinding,
    TelegramLinkRequest,
    TelegramProfile,
    TelegramReceiptDelivery,
    WhatsAppMessage,
)
from .utils import now_utc_naive


def init_schema():
    engine = get_engine()
    Base.metadata.create_all(engine)
    apply_lightweight_upgrades(engine)
    seed_expense_budget_categories(engine)
    seed_expense_allocation_rules(engine)
    refresh_existing_auto_allocations(engine)


def ensure_whatsapp_schema():
    """Create only the WhatsApp queue table without changing existing data."""
    WhatsAppMessage.__table__.create(get_engine(), checkfirst=True)


def ensure_telegram_schema():
    """Create local Telegram profiles, review requests, bindings and delivery queue."""
    engine = get_engine()
    TelegramProfile.__table__.create(engine, checkfirst=True)
    TelegramLinkRequest.__table__.create(engine, checkfirst=True)
    TelegramBinding.__table__.create(engine, checkfirst=True)
    TelegramReceiptDelivery.__table__.create(engine, checkfirst=True)


def ensure_community_schema():
    """Create the tables transferred from Ecoparkovich in dependency order."""
    engine = get_engine()
    for table in (
        BotSettings.__table__,
        NotificationType.__table__,
        NotificationSource.__table__,
        NotificationDestination.__table__,
        DirectoryEntry.__table__,
        CommunityUser.__table__,
        CommunityMessage.__table__,
        ReputationEvent.__table__,
        NotificationSubscription.__table__,
        OutageEvent.__table__,
        NotificationDelivery.__table__,
    ):
        table.create(engine, checkfirst=True)


def ensure_expense_schema():
    """Create and upgrade expense allocation storage, then classify auto-mode rows."""
    engine = get_engine()
    ExpenseBudgetCategory.__table__.create(engine, checkfirst=True)
    ExpenseAllocationRule.__table__.create(engine, checkfirst=True)
    ExpenseAllocation.__table__.create(engine, checkfirst=True)
    apply_lightweight_upgrades(engine)
    seed_expense_budget_categories(engine)
    seed_expense_allocation_rules(engine)
    refresh_existing_auto_allocations(engine)


def seed_expense_budget_categories(engine):
    rows = [
        {
            "id": category_id,
            "name": name,
            "sort_order": index,
            "active": True,
        }
        for index, (category_id, name) in enumerate(BUDGET_CATEGORIES, start=1)
    ]
    statement = insert(ExpenseBudgetCategory).values(rows)
    with engine.begin() as connection:
        connection.execute(
            statement.on_duplicate_key_update(
                name=statement.inserted.name,
                sort_order=statement.inserted.sort_order,
                active=statement.inserted.active,
            )
        )


def seed_expense_allocation_rules(engine):
    timestamp = now_utc_naive()
    with Session(engine, future=True) as session:
        existing_fragments = {
            str(value).strip().lower()
            for value in session.scalars(
                select(ExpenseAllocationRule.purpose_contains)
            ).all()
        }
        for category_id, purpose_contains in DEFAULT_PURPOSE_RULES:
            if purpose_contains.strip().lower() in existing_fragments:
                continue
            session.add(
                ExpenseAllocationRule(
                    category_id=category_id,
                    purpose_contains=purpose_contains,
                    active=True,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        session.commit()


def refresh_existing_auto_allocations(engine):
    with Session(engine, future=True) as session:
        expenses = session.scalars(
            select(Expense).where(Expense.allocation_mode == "auto")
        ).all()
        refresh_auto_expense_allocations(session, expenses)
        session.commit()


def apply_lightweight_upgrades(engine):
    inspector = inspect(engine)
    columns_by_table = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
    }

    statements = []
    if "owners" in columns_by_table and "phone" not in columns_by_table["owners"]:
        statements.append("ALTER TABLE owners ADD COLUMN phone VARCHAR(100) NOT NULL DEFAULT '' AFTER name")
    if "owner_plots" in columns_by_table and "phone" not in columns_by_table["owner_plots"]:
        statements.append("ALTER TABLE owner_plots ADD COLUMN phone VARCHAR(100) NOT NULL DEFAULT '' AFTER owner")
    if "plots" in columns_by_table and "cadastral_number" not in columns_by_table["plots"]:
        statements.append(
            "ALTER TABLE plots ADD COLUMN cadastral_number VARCHAR(64) NOT NULL DEFAULT '' AFTER address"
        )
    if "call_attempts" in columns_by_table and "source_file" not in columns_by_table["call_attempts"]:
        statements.append("ALTER TABLE call_attempts ADD COLUMN source_file VARCHAR(255) NOT NULL DEFAULT '' AFTER comment")
    if "expenses" in columns_by_table:
        expense_columns = columns_by_table["expenses"]
        if "receipt_items_json" not in expense_columns:
            statements.append(
                "ALTER TABLE expenses ADD COLUMN receipt_items_json TEXT NULL AFTER purpose"
            )
        if "allocation_mode" not in expense_columns:
            after_column = (
                "receipt_items_json"
                if "receipt_items_json" in expense_columns
                else "purpose"
            )
            statements.append(
                "ALTER TABLE expenses ADD COLUMN allocation_mode "
                f"VARCHAR(16) NOT NULL DEFAULT 'auto' AFTER {after_column}"
            )

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        if (
            "expenses" in columns_by_table
            and "receipt_items_json" not in columns_by_table["expenses"]
        ):
            connection.execute(
                text(
                    "UPDATE expenses SET receipt_items_json = '[]' "
                    "WHERE receipt_items_json IS NULL OR receipt_items_json = ''"
                )
            )
