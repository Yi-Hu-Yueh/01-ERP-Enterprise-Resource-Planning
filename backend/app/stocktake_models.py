"""Durable whole-warehouse snapshots and source-linked inventory variances."""
from datetime import datetime

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit
from .receiving_models import Created, reference


class Stocktake(DocumentAudit, Base):
    __tablename__ = "stocktake"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    business_date: Mapped[str] = mapped_column(Text)
    scope_kind: Mapped[str] = mapped_column(Text, default="WHOLE_WAREHOUSE")
    state: Mapped[str] = mapped_column(Text, default="DRAFT")
    freeze_active: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)
    snapshot_at: Mapped[datetime | None] = mapped_column(UTCText())
    started_by: Mapped[str | None] = mapped_column(UUIDText(), reference("actor.id"))
    start_key: Mapped[str | None] = mapped_column(UUIDText(), unique=True)
    start_fingerprint: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(UTCText())
    closed_by: Mapped[str | None] = mapped_column(UUIDText(), reference("actor.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCText())
    cancelled_by: Mapped[str | None] = mapped_column(UUIDText(), reference("actor.id"))


class StocktakeLine(DocumentAudit, Base):
    __tablename__ = "stocktake_line"
    stocktake_id: Mapped[str] = mapped_column(UUIDText(), reference("stocktake.id"))
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    item_id: Mapped[str] = mapped_column(UUIDText(), reference("item.id"))
    item_kind: Mapped[str] = mapped_column(Text)
    unit_id: Mapped[str] = mapped_column(UUIDText(), reference("unit.id"))
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    lot_id: Mapped[str | None] = mapped_column(UUIDText(), reference("inventory_lot.id"))
    book_qty: Mapped[str] = mapped_column(Text)
    counted_qty: Mapped[str | None] = mapped_column(Text)
    counted_at: Mapped[datetime | None] = mapped_column(UTCText())
    counted_by: Mapped[str | None] = mapped_column(UUIDText(), reference("actor.id"))
    note: Mapped[str | None] = mapped_column(Text)


class StocktakeException(Created, Base):
    __tablename__ = "stocktake_exception"
    stocktake_id: Mapped[str] = mapped_column(UUIDText(), reference("stocktake.id"))
    description: Mapped[str] = mapped_column(Text)


class StocktakePosting(Created, Base):
    __tablename__ = "stocktake_posting"
    stocktake_id: Mapped[str] = mapped_column(UUIDText(), reference("stocktake.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(UUIDText(), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    source_version: Mapped[int] = mapped_column(Integer)
    posting_state: Mapped[str] = mapped_column(Text)
    authorization_basis: Mapped[str] = mapped_column(Text)
    confirmation_digest: Mapped[str] = mapped_column(Text)
    confirmation_snapshot: Mapped[str] = mapped_column(Text)
    confirmation_received_at: Mapped[datetime] = mapped_column(UTCText())
    posted_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    posted_by: Mapped[str] = mapped_column(UUIDText(), reference("actor.id"))


class StocktakeEntry(Created, Base):
    __tablename__ = "stocktake_entry"
    posting_id: Mapped[str] = mapped_column(UUIDText(), reference("stocktake_posting.id"))
    stocktake_id: Mapped[str] = mapped_column(UUIDText(), reference("stocktake.id"))
    stocktake_line_id: Mapped[str] = mapped_column(UUIDText(), reference("stocktake_line.id"), unique=True)
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)
