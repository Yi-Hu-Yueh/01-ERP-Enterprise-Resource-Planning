"""Authorized pre-approval purchase documents; no stock or posting entities."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, UUIDText, UTCText, now_utc


class DocumentAudit:
    id: Mapped[str] = mapped_column(UUIDText(), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    created_by: Mapped[str] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    updated_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc, onupdate=now_utc)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class PurchaseOrder(DocumentAudit, Base):
    __tablename__ = "purchase_order"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    supplier_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("supplier.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    supplier_name_snapshot: Mapped[str] = mapped_column(Text)
    currency_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("currency.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    order_date: Mapped[str] = mapped_column(Text)
    delivery_address_snapshot: Mapped[str | None] = mapped_column(Text)
    payment_terms_snapshot: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    # D06 is pending: there is no approval/cancellation API or assumed state.
    approval_state: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(UTCText())
    approved_by: Mapped[str | None] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))


class PurchaseLine(DocumentAudit, Base):
    __tablename__ = "purchase_order_line"
    purchase_order_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("purchase_order.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    description_snapshot: Mapped[str] = mapped_column(Text)
    unit_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("unit.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    base_factor_snapshot: Mapped[str] = mapped_column(Text)
    ordered_qty: Mapped[str] = mapped_column(Text)
    unit_price: Mapped[str] = mapped_column(Text)
    due_date: Mapped[str] = mapped_column(Text)
    preferred_warehouse_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("warehouse.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    supplier_offer_id: Mapped[str | None] = mapped_column(UUIDText(), ForeignKey("supplier_offer.id", ondelete="RESTRICT", onupdate="RESTRICT"))


class PurchaseCreateReceipt(Base):
    """Durable create deduplication, retained even if its resource is deleted."""
    __tablename__ = "purchase_create_receipt"
    request_key: Mapped[str] = mapped_column(UUIDText(), primary_key=True)
    resource_kind: Mapped[str] = mapped_column(Text)
    record_id: Mapped[str] = mapped_column(UUIDText())
    payload_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    created_by: Mapped[str] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))
