"""Existing-position inventory adjustments with separate immutable posting identity."""
from datetime import datetime

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit
from .receiving_models import Created, reference


class InventoryAdjustment(DocumentAudit, Base):
    __tablename__ = "inventory_adjustment"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    business_date: Mapped[str] = mapped_column(Text)
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    item_id: Mapped[str] = mapped_column(UUIDText(), reference("item.id"))
    item_kind: Mapped[str] = mapped_column(Text)
    unit_id: Mapped[str] = mapped_column(UUIDText(), reference("unit.id"))
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    lot_id: Mapped[str | None] = mapped_column(UUIDText(), reference("inventory_lot.id"))
    direction: Mapped[str] = mapped_column(Text)
    quantity: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="DRAFT")
    posted_at: Mapped[datetime | None] = mapped_column(UTCText())
    posted_by: Mapped[str | None] = mapped_column(UUIDText(), reference("actor.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCText())
    cancelled_by: Mapped[str | None] = mapped_column(UUIDText(), reference("actor.id"))


class InventoryAdjustmentPosting(Created, Base):
    __tablename__ = "inventory_adjustment_posting"
    adjustment_id: Mapped[str] = mapped_column(UUIDText(), reference("inventory_adjustment.id"), unique=True)
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


class InventoryAdjustmentEntry(Created, Base):
    __tablename__ = "inventory_adjustment_entry"
    posting_id: Mapped[str] = mapped_column(UUIDText(), reference("inventory_adjustment_posting.id"), unique=True)
    adjustment_id: Mapped[str] = mapped_column(UUIDText(), reference("inventory_adjustment.id"), unique=True)
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)
