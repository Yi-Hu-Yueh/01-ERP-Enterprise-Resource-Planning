"""Source-linked customer returns; business finalization is distinct from stock restoration."""
from datetime import datetime
from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit
from .receiving_models import Created, reference


class CustomerReturn(DocumentAudit, Base):
    __tablename__ = "customer_return"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    shipment_id: Mapped[str] = mapped_column(UUIDText(), reference("sales_shipment.id"))
    shipment_version: Mapped[int] = mapped_column(Integer)
    sales_order_line_id: Mapped[str] = mapped_column(UUIDText(), reference("sales_order_line.id"))
    sales_order_id: Mapped[str] = mapped_column(UUIDText(), reference("sales_order.id"))
    customer_id: Mapped[str] = mapped_column(UUIDText(), reference("customer.id"))
    product_id: Mapped[str] = mapped_column(UUIDText(), reference("item.id"))
    unit_id: Mapped[str] = mapped_column(UUIDText(), reference("unit.id"))
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    lot_id: Mapped[str | None] = mapped_column(UUIDText(), reference("inventory_lot.id"))
    business_date: Mapped[str] = mapped_column(Text)
    quantity: Mapped[str] = mapped_column(Text)
    restock_eligible: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class CustomerReturnFinalization(Created, Base):
    __tablename__ = "customer_return_finalization"
    return_id: Mapped[str] = mapped_column(UUIDText(), reference("customer_return.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(UUIDText(), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    source_version: Mapped[int] = mapped_column(Integer)
    finalization_state: Mapped[str] = mapped_column(Text)
    authorization_basis: Mapped[str] = mapped_column(Text)
    confirmation_digest: Mapped[str] = mapped_column(Text)
    confirmation_snapshot: Mapped[str] = mapped_column(Text)
    confirmation_received_at: Mapped[datetime] = mapped_column(UTCText())
    finalized_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    finalized_by: Mapped[str] = mapped_column(UUIDText(), reference("actor.id"))


class CustomerReturnEntry(Created, Base):
    __tablename__ = "customer_return_entry"
    finalization_id: Mapped[str] = mapped_column(UUIDText(), reference("customer_return_finalization.id"))
    return_id: Mapped[str] = mapped_column(UUIDText(), reference("customer_return.id"), unique=True)
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)
