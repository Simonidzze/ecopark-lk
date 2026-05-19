from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text
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
