"""SQLAlchemy 2.x models. The source of truth for the schema is migrations/001_init.sql;
these classes mirror it for querying. If you change one, change the other."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Part(Base):
    __tablename__ = "parts"
    part_id: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    uom: Mapped[str] = mapped_column(String)


class POLine(Base):
    __tablename__ = "po_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_number: Mapped[str] = mapped_column(String)
    line_no: Mapped[int] = mapped_column(Integer)
    part_id: Mapped[str] = mapped_column(ForeignKey("parts.part_id"))
    supplier: Mapped[str] = mapped_column(String)
    ordered_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit_price_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    need_by_date: Mapped[date] = mapped_column(Date)
    promised_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String)


class ReceiptEvent(Base):
    __tablename__ = "receipt_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    po_line_id: Mapped[int] = mapped_column(ForeignKey("po_lines.id"))
    received_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    accepted_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    rejected_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    quarantined_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    lot_batch: Mapped[str] = mapped_column(String)
    coc_received: Mapped[bool] = mapped_column(Boolean)
    exception_flags: Mapped[str] = mapped_column(String, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerEvent(Base):
    __tablename__ = "ledger_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[str] = mapped_column(ForeignKey("parts.part_id"))
    po_line_id: Mapped[int | None] = mapped_column(ForeignKey("po_lines.id"), nullable=True)
    receipt_event_id: Mapped[int | None] = mapped_column(ForeignKey("receipt_events.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String)
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String)
    po_line_id: Mapped[int] = mapped_column(ForeignKey("po_lines.id"))
    invoice_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    invoice_unit_price_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    invoice_date: Mapped[date] = mapped_column(Date)
