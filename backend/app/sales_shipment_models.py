"""One genuine sales-line/position shipment and its immutable negative ledger leg."""
from datetime import datetime
from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit
from .receiving_models import Created, reference


class SalesShipment(DocumentAudit, Base):
    __tablename__ = "sales_shipment"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    sales_order_line_id: Mapped[str] = mapped_column(UUIDText(), reference("sales_order_line.id"))
    line_version: Mapped[int] = mapped_column(Integer)
    order_version: Mapped[int] = mapped_column(Integer)
    sales_order_id: Mapped[str] = mapped_column(UUIDText(), reference("sales_order.id"))
    customer_id: Mapped[str] = mapped_column(UUIDText(), reference("customer.id"))
    product_id: Mapped[str] = mapped_column(UUIDText(), reference("item.id"))
    unit_id: Mapped[str] = mapped_column(UUIDText(), reference("unit.id"))
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    lot_id: Mapped[str | None] = mapped_column(UUIDText(), reference("inventory_lot.id"))
    business_date: Mapped[str] = mapped_column(Text)
    quantity: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class SalesShipmentPosting(Created, Base):
    __tablename__ = "sales_shipment_posting"
    shipment_id: Mapped[str] = mapped_column(UUIDText(), reference("sales_shipment.id"), unique=True)
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


class SalesShipmentEntry(Created, Base):
    __tablename__ = "sales_shipment_entry"
    posting_id: Mapped[str] = mapped_column(UUIDText(), reference("sales_shipment_posting.id"))
    shipment_id: Mapped[str] = mapped_column(UUIDText(), reference("sales_shipment.id"), unique=True)
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)
