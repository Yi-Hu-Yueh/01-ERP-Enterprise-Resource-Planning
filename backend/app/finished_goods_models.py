"""Source-linked finished-goods receipts, with one immutable inbound leg."""
from datetime import datetime
from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit
from .receiving_models import Created, reference


class FinishedGoodsReceipt(DocumentAudit, Base):
    __tablename__ = "finished_goods_receipt"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    completion_id: Mapped[str] = mapped_column(UUIDText(), reference("production_completion.id"))
    completion_version: Mapped[int] = mapped_column(Integer)
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    lot_id: Mapped[str | None] = mapped_column(UUIDText(), reference("inventory_lot.id"))
    lot_code: Mapped[str | None] = mapped_column(Text)
    business_date: Mapped[str] = mapped_column(Text)
    quantity: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class FinishedGoodsPosting(Created, Base):
    __tablename__ = "finished_goods_posting"
    receipt_id: Mapped[str] = mapped_column(UUIDText(), reference("finished_goods_receipt.id"), unique=True)
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


class FinishedGoodsEntry(Created, Base):
    __tablename__ = "finished_goods_entry"
    posting_id: Mapped[str] = mapped_column(UUIDText(), reference("finished_goods_posting.id"))
    receipt_id: Mapped[str] = mapped_column(UUIDText(), reference("finished_goods_receipt.id"), unique=True)
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)
