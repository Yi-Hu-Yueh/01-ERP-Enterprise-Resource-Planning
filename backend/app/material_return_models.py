"""Manufacturing returns trace their immutable original issue, not a new receipt."""
from datetime import datetime

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit
from .receiving_models import Created, reference


class MaterialReturn(DocumentAudit, Base):
    __tablename__ = "material_return"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    work_order_id: Mapped[str] = mapped_column(UUIDText(), reference("work_order.id"))
    work_order_version: Mapped[int] = mapped_column(Integer)
    business_date: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class MaterialReturnLine(DocumentAudit, Base):
    __tablename__ = "material_return_line"
    material_return_id: Mapped[str] = mapped_column(UUIDText(), reference("material_return.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    origin_line_id: Mapped[str] = mapped_column(UUIDText(), reference("material_issue_line.id"))
    origin_line_version: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class MaterialReturnPosting(Created, Base):
    __tablename__ = "material_return_posting"
    material_return_id: Mapped[str] = mapped_column(UUIDText(), reference("material_return.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(UUIDText(), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    source_version: Mapped[int] = mapped_column(Integer)
    work_order_version: Mapped[int] = mapped_column(Integer)
    posting_state: Mapped[str] = mapped_column(Text)
    authorization_basis: Mapped[str] = mapped_column(Text)
    confirmation_digest: Mapped[str] = mapped_column(Text)
    confirmation_snapshot: Mapped[str] = mapped_column(Text)
    confirmation_received_at: Mapped[datetime] = mapped_column(UTCText())
    posted_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    posted_by: Mapped[str] = mapped_column(UUIDText(), reference("actor.id"))


class MaterialReturnEntry(Created, Base):
    __tablename__ = "material_return_entry"
    posting_id: Mapped[str] = mapped_column(UUIDText(), reference("material_return_posting.id"))
    return_line_id: Mapped[str] = mapped_column(UUIDText(), reference("material_return_line.id"))
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)
