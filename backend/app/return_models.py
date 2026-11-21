"""Typed supplier-return extension of the accepted receipt-only stock schema."""
from datetime import datetime

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit
from .receiving_models import Created, reference


class SupplierReturn(DocumentAudit, Base):
    __tablename__ = "supplier_return"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    supplier_id: Mapped[str] = mapped_column(UUIDText(), reference("supplier.id"))
    party_name_snapshot: Mapped[str] = mapped_column(Text)
    business_date: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    external_reference: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class SupplierReturnLine(DocumentAudit, Base):
    __tablename__ = "supplier_return_line"
    supplier_return_id: Mapped[str] = mapped_column(UUIDText(), reference("supplier_return.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    origin_line_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_document_line.id"))
    origin_line_version: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class SupplierReturnPosting(Created, Base):
    __tablename__ = "supplier_return_posting"
    supplier_return_id: Mapped[str] = mapped_column(UUIDText(), reference("supplier_return.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(UUIDText(), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    source_version: Mapped[int] = mapped_column(Integer)
    posting_state: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    posted_by: Mapped[str] = mapped_column(UUIDText(), reference("actor.id"))


class SupplierReturnEntry(Created, Base):
    __tablename__ = "supplier_return_entry"
    posting_id: Mapped[str] = mapped_column(UUIDText(), reference("supplier_return_posting.id"))
    return_line_id: Mapped[str] = mapped_column(UUIDText(), reference("supplier_return_line.id"))
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)
