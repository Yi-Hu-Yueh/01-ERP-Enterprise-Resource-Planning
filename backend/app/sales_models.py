"""Customer demand documents; deliberately independent of physical stock."""
from datetime import datetime
from sqlalchemy import ForeignKey, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit


class SalesOrder(DocumentAudit, Base):
    __tablename__ = "sales_order"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    customer_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("customer.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    order_date: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="DRAFT")


class SalesLine(DocumentAudit, Base):
    __tablename__ = "sales_order_line"
    sales_order_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("sales_order.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    description_snapshot: Mapped[str] = mapped_column(Text)
    unit_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("unit.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    ordered_qty: Mapped[str] = mapped_column(Text)
    unit_price: Mapped[str] = mapped_column(Text)


class SalesCreateReceipt(Base):
    """Separate durable demand-create identities, including deleted resources."""
    __tablename__ = "sales_create_receipt"
    # Same established technical structure; no new authorization mechanism.
    request_key: Mapped[str] = mapped_column(UUIDText(), primary_key=True)
    resource_kind: Mapped[str] = mapped_column(Text)
    record_id: Mapped[str] = mapped_column(UUIDText())
    payload_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    created_by: Mapped[str] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))
