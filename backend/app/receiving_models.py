"""Purchase receiving and immutable physical inventory, without valuation/returns."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit


def reference(target):
    return ForeignKey(target, ondelete="RESTRICT", onupdate="RESTRICT")


class Created:
    id: Mapped[str] = mapped_column(UUIDText(), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    created_by: Mapped[str] = mapped_column(UUIDText(), reference("actor.id"))


class InventoryLot(Created, Base):
    __tablename__ = "inventory_lot"
    item_id: Mapped[str] = mapped_column(UUIDText(), reference("item.id"))
    lot_code: Mapped[str] = mapped_column(Text)
    origin_kind: Mapped[str] = mapped_column(Text, default="PURCHASED")
    supplier_id: Mapped[str | None] = mapped_column(UUIDText(), reference("supplier.id"))
    supplier_lot_code: Mapped[str | None] = mapped_column(Text)
    manufactured_date: Mapped[str | None] = mapped_column(Text)
    expiry_date: Mapped[str | None] = mapped_column(Text)
    traceability_note: Mapped[str | None] = mapped_column(Text)


class StockDocument(DocumentAudit, Base):
    __tablename__ = "stock_document"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    document_kind: Mapped[str] = mapped_column(Text, default="PURCHASE_RECEIPT")
    purchase_order_id: Mapped[str] = mapped_column(UUIDText(), reference("purchase_order.id"))
    purchase_order_version: Mapped[int] = mapped_column(Integer)
    business_date: Mapped[str] = mapped_column(Text)
    supplier_id: Mapped[str] = mapped_column(UUIDText(), reference("supplier.id"))
    party_name_snapshot: Mapped[str] = mapped_column(Text)
    external_reference: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    approval_state: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(UTCText())
    approved_by: Mapped[str | None] = mapped_column(UUIDText(), reference("actor.id"))


class StockDocumentLine(DocumentAudit, Base):
    __tablename__ = "stock_document_line"
    stock_document_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_document.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    purchase_order_line_id: Mapped[str] = mapped_column(UUIDText(), reference("purchase_order_line.id"))
    purchase_order_line_version: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[str] = mapped_column(UUIDText(), reference("item.id"))
    description_snapshot: Mapped[str] = mapped_column(Text)
    quantity: Mapped[str] = mapped_column(Text)
    unit_id: Mapped[str] = mapped_column(UUIDText(), reference("unit.id"))
    base_factor_snapshot: Mapped[str] = mapped_column(Text)
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    lot_id: Mapped[str | None] = mapped_column(UUIDText(), reference("inventory_lot.id"))
    lot_code: Mapped[str | None] = mapped_column(Text)
    supplier_lot_code: Mapped[str | None] = mapped_column(Text)
    manufactured_date: Mapped[str | None] = mapped_column(Text)
    expiry_date: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class StockPosition(Created, Base):
    __tablename__ = "stock_position"
    item_id: Mapped[str] = mapped_column(UUIDText(), reference("item.id"))
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    lot_id: Mapped[str | None] = mapped_column(UUIDText(), reference("inventory_lot.id"))


class StockPosting(Created, Base):
    __tablename__ = "stock_posting"
    stock_document_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_document.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(UUIDText(), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    source_version: Mapped[int] = mapped_column(Integer)
    posting_state: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    posted_by: Mapped[str] = mapped_column(UUIDText(), reference("actor.id"))


class StockEntry(Created, Base):
    __tablename__ = "stock_entry"
    posting_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_posting.id"))
    document_line_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_document_line.id"))
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)


class StockCreateReceipt(Base):
    __tablename__ = "stock_create_receipt"
    request_key: Mapped[str] = mapped_column(UUIDText(), primary_key=True)
    resource_kind: Mapped[str] = mapped_column(Text)
    record_id: Mapped[str] = mapped_column(UUIDText())
    payload_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    created_by: Mapped[str] = mapped_column(UUIDText(), reference("actor.id"))
