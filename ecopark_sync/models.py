from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("idx_sync_runs_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    source_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_text: Mapped[Optional[str]] = mapped_column(Text)


class Owner(Base):
    __tablename__ = "owners"
    __table_args__ = (
        Index("idx_owners_name", "name"),
        Index("idx_owners_sync_run_id", "sync_run_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Plot(Base):
    __tablename__ = "plots"
    __table_args__ = (
        Index("idx_plots_plot_number", "plot_number"),
        Index("idx_plots_account", "account"),
        Index("idx_plots_sync_run_id", "sync_run_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plot_number: Mapped[str] = mapped_column(String(64), nullable=False)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    cadastral_number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    organization: Mapped[str] = mapped_column(String(255), nullable=False)
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class OwnerPlot(Base):
    __tablename__ = "owner_plots"
    __table_args__ = (
        Index("idx_owner_plots_owner_id", "owner_id"),
        Index("idx_owner_plots_plot_id", "plot_id"),
        Index("idx_owner_plots_account", "account"),
        Index("idx_owner_plots_sync_run_id", "sync_run_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    plot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plot_number: Mapped[str] = mapped_column(String(64), nullable=False)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    presentation: Mapped[str] = mapped_column(String(255), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Balance(Base):
    __tablename__ = "balances"
    __table_args__ = (
        Index("idx_balances_owner_id", "owner_id"),
        Index("idx_balances_plot_id", "plot_id"),
        Index("idx_balances_account", "account"),
        Index("idx_balances_sync_run_id", "sync_run_id"),
    )

    owner_plot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plot_number: Mapped[str] = mapped_column(String(64), nullable=False)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    debt: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    penalty: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    overpayment: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MessengerBinding(Base):
    __tablename__ = "messenger_bindings"
    __table_args__ = (
        Index("idx_messenger_external_id", "messenger", "external_id"),
        Index("idx_messenger_sync_run_id", "sync_run_id"),
    )

    owner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    messenger: Mapped[str] = mapped_column(String(32), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TelegramProfile(Base):
    __tablename__ = "telegram_profiles"

    telegram_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    first_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    draft_full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    draft_plot_number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    draft_phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TelegramLinkRequest(Base):
    __tablename__ = "telegram_link_requests"
    __table_args__ = (
        Index("idx_telegram_link_requests_status_created", "status", "created_at"),
        Index("idx_telegram_link_requests_user", "telegram_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    plot_number: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    owner_id: Mapped[Optional[str]] = mapped_column(String(64))
    owner_plot_id: Mapped[Optional[str]] = mapped_column(String(64))
    review_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class TelegramBinding(Base):
    __tablename__ = "telegram_bindings"
    __table_args__ = (
        Index("idx_telegram_bindings_owner_id", "owner_id"),
        Index("idx_telegram_bindings_enabled", "enabled"),
    )

    telegram_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CallCampaign(Base):
    __tablename__ = "call_campaigns"
    __table_args__ = (
        Index("idx_call_campaigns_external_id", "external_id"),
        Index("idx_call_campaigns_called_at", "called_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    caller_phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    called_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    report_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    total_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    callbacks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CallAttempt(Base):
    __tablename__ = "call_attempts"
    __table_args__ = (
        Index("idx_call_attempts_campaign_id", "campaign_id"),
        Index("idx_call_attempts_phone", "phone_normalized"),
        Index("idx_call_attempts_called_at", "called_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(Integer, nullable=False)
    phone: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    call_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manager_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    called_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    cost: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False, default=0)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_file: Mapped[str] = mapped_column(String(255), nullable=False, default="")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index("idx_payments_owner_plot_id", "owner_plot_id"),
        Index("idx_payments_date", "date"),
        Index("idx_payments_document_id", "document_id"),
        Index("idx_payments_sync_run_id", "sync_run_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[str] = mapped_column(String(255), nullable=False)
    date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_plot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    payment_type: Mapped[str] = mapped_column(String(128), nullable=False)
    incoming_number: Mapped[str] = mapped_column(String(128), nullable=False)
    incoming_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    registry_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Accrual(Base):
    __tablename__ = "accruals"
    __table_args__ = (
        Index("idx_accruals_owner_plot_id", "owner_plot_id"),
        Index("idx_accruals_date", "date"),
        Index("idx_accruals_contribution_id", "contribution_id"),
        Index("idx_accruals_sync_run_id", "sync_run_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[str] = mapped_column(String(255), nullable=False)
    date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_plot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    contribution_id: Mapped[str] = mapped_column(String(64), nullable=False)
    contribution: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    quantity: Mapped[Decimal] = mapped_column(Numeric(15, 3), nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Income(Base):
    __tablename__ = "incomes"
    __table_args__ = (
        Index("idx_incomes_date", "date"),
        Index("idx_incomes_document_id", "document_id"),
        Index("idx_incomes_category", "income_category"),
        Index("idx_incomes_counterparty", "counterparty"),
        Index("idx_incomes_sync_run_id", "sync_run_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[str] = mapped_column(String(255), nullable=False)
    date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    income_category: Mapped[str] = mapped_column(String(255), nullable=False)
    counterparty_id: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    organization: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        Index("idx_expenses_date", "date"),
        Index("idx_expenses_document_id", "document_id"),
        Index("idx_expenses_category", "expense_category"),
        Index("idx_expenses_counterparty", "counterparty"),
        Index("idx_expenses_sync_run_id", "sync_run_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[str] = mapped_column(String(255), nullable=False)
    date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    number: Mapped[str] = mapped_column(String(64), nullable=False)
    expense_category_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expense_category: Mapped[str] = mapped_column(String(255), nullable=False)
    counterparty_id: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allocation_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    organization: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    sync_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExpenseBudgetCategory(Base):
    __tablename__ = "expense_budget_categories"
    __table_args__ = (
        Index("idx_expense_budget_categories_order", "sort_order", "name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ExpenseAllocationRule(Base):
    __tablename__ = "expense_allocation_rules"
    __table_args__ = (
        Index("idx_expense_allocation_rules_category_id", "category_id"),
        Index("idx_expense_allocation_rules_active", "active"),
        UniqueConstraint(
            "purpose_contains",
            name="uq_expense_allocation_rules_purpose_contains",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_id: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose_contains: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ExpenseAllocation(Base):
    __tablename__ = "expense_allocations"
    __table_args__ = (
        Index("idx_expense_allocations_expense_id", "expense_id"),
        Index("idx_expense_allocations_category_id", "category_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    expense_id: Mapped[str] = mapped_column(String(128), nullable=False)
    category_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PretrialClaim(Base):
    __tablename__ = "pretrial_claims"
    __table_args__ = (
        Index("idx_pretrial_claims_owner_plot_id", "owner_plot_id"),
        Index("idx_pretrial_claims_created_at", "created_at"),
    )

    number: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_plot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plot_number: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_name: Mapped[str] = mapped_column(String(255), nullable=False)
    claim_date: Mapped[date] = mapped_column(Date, nullable=False)
    calculation_date: Mapped[date] = mapped_column(Date, nullable=False)
    debt_period_from: Mapped[Optional[date]] = mapped_column(Date)
    debt_period_to: Mapped[Optional[date]] = mapped_column(Date)
    principal_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        Index("idx_whatsapp_messages_status_created", "status", "created_at"),
        Index("idx_whatsapp_messages_batch_id", "batch_id"),
        Index("idx_whatsapp_messages_owner_plot_id", "owner_plot_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_number: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_plot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_name: Mapped[str] = mapped_column(String(255), nullable=False)
    plot_number: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_message_id: Mapped[Optional[str]] = mapped_column(String(255))
    error_text: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# Ecoparkovich tables keep their original names so their data can be imported
# without remapping identifiers or losing delivery and reputation history.
class BotSettings(Base):
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    telegram_api_base_url: Mapped[str] = mapped_column(
        String(500), nullable=False, default="https://api.telegram.org/bot"
    )
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    send_existing_on_first_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    telegram_update_offset: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationType(Base):
    __tablename__ = "notification_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_url: Mapped[Optional[str]] = mapped_column(String(1000))
    match_text: Mapped[str] = mapped_column(String(255), nullable=False, default="экопарк")
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    initialized_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    force_check_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_check_status: Mapped[Optional[str]] = mapped_column(String(32))
    last_check_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationSource(Base):
    __tablename__ = "notification_sources"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_notification_source_key"),
        Index("idx_notification_sources_type", "notification_type_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_types.id", ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    initialized_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationDestination(Base):
    __tablename__ = "notification_destinations"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_thread_id", name="uq_destination_chat_thread"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message_thread_id: Mapped[Optional[int]] = mapped_column(Integer)
    reputation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    directory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reputation_cooldown_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    thanks_phrases: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="спасибо\nблагодарю\nспасибо за помощь\nвыручил\nвыручила",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DirectoryEntry(Base):
    __tablename__ = "directory_entries"
    __table_args__ = (Index("idx_directory_entries_destination", "destination_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_destinations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(255), nullable=False, default="Общее")
    phone: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(String(1000))
    keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CommunityUser(Base):
    __tablename__ = "community_users"
    __table_args__ = (
        UniqueConstraint(
            "destination_id", "telegram_user_id", name="uq_community_user_destination_telegram"
        ),
        Index("idx_community_users_reputation", "reputation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_destinations.id", ondelete="CASCADE"), nullable=False
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    reputation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CommunityMessage(Base):
    __tablename__ = "community_messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_community_chat_message"),
        Index("idx_community_messages_destination", "destination_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_destinations.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_thread_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ReputationEvent(Base):
    __tablename__ = "reputation_events"
    __table_args__ = (
        UniqueConstraint(
            "destination_id",
            "giver_user_id",
            "source_kind",
            "source_chat_id",
            "source_message_id",
            name="uq_reputation_source_giver",
        ),
        Index("idx_reputation_events_receiver", "receiver_user_id"),
        Index("idx_reputation_events_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_destinations.id", ondelete="CASCADE"), nullable=False
    )
    giver_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    receiver_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationSubscription(Base):
    __tablename__ = "notification_subscriptions"

    notification_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_types.id", ondelete="CASCADE"), primary_key=True
    )
    destination_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_destinations.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class OutageEvent(Base):
    __tablename__ = "outage_events"
    __table_args__ = (
        UniqueConstraint("notification_type_id", "fingerprint", name="uq_event_type_fingerprint"),
        Index("idx_outage_events_type", "notification_type_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_types.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "destination_id", name="uq_event_destination"),
        Index("idx_notification_deliveries_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("outage_events.id", ondelete="CASCADE"), nullable=False
    )
    destination_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_destinations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
