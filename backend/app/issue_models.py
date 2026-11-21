"""Typed manufacturing issues extending the shared immutable physical ledger."""
from datetime import datetime
from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, UUIDText, UTCText, now_utc
from .purchase_models import DocumentAudit
from .receiving_models import Created, reference


class MaterialIssue(DocumentAudit, Base):
    __tablename__ = "material_issue"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    work_order_id: Mapped[str] = mapped_column(UUIDText(), reference("work_order.id"))
    work_order_version: Mapped[int] = mapped_column(Integer)
    bom_id: Mapped[str | None] = mapped_column(UUIDText(), reference("bom.id"))
    bom_version: Mapped[int | None] = mapped_column(Integer)
    routing_id: Mapped[str | None] = mapped_column(UUIDText(), reference("routing.id"))
    routing_version: Mapped[int | None] = mapped_column(Integer)
    business_date: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class MaterialIssueLine(DocumentAudit, Base):
    __tablename__ = "material_issue_line"
    material_issue_id: Mapped[str] = mapped_column(UUIDText(), reference("material_issue.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    material_id: Mapped[str] = mapped_column(UUIDText(), reference("item.id"))
    warehouse_id: Mapped[str] = mapped_column(UUIDText(), reference("warehouse.id"))
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    unit_id: Mapped[str] = mapped_column(UUIDText(), reference("unit.id"))
    bom_component_id: Mapped[str | None] = mapped_column(UUIDText(), reference("bom_component.id"))
    bom_component_version: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class MaterialIssuePosting(Created, Base):
    __tablename__ = "material_issue_posting"
    material_issue_id: Mapped[str] = mapped_column(UUIDText(), reference("material_issue.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(UUIDText(), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    source_version: Mapped[int] = mapped_column(Integer)
    work_order_version: Mapped[int] = mapped_column(Integer)
    posting_state: Mapped[str] = mapped_column(Text)
    authorization_basis: Mapped[str] = mapped_column(Text)
    confirmation_digest: Mapped[str | None] = mapped_column(Text)
    confirmation_snapshot: Mapped[str | None] = mapped_column(Text)
    confirmation_received_at: Mapped[datetime | None] = mapped_column(UTCText())
    posted_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    posted_by: Mapped[str] = mapped_column(UUIDText(), reference("actor.id"))


class MaterialIssueEntry(Created, Base):
    __tablename__ = "material_issue_entry"
    posting_id: Mapped[str] = mapped_column(UUIDText(), reference("material_issue_posting.id"))
    issue_line_id: Mapped[str] = mapped_column(UUIDText(), reference("material_issue_line.id"))
    position_id: Mapped[str] = mapped_column(UUIDText(), reference("stock_position.id"))
    leg_no: Mapped[int] = mapped_column(Integer, default=1)
    qty_base_delta: Mapped[str] = mapped_column(Text)
