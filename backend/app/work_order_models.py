"""Draft work orders only; no execution, snapshots, reservations or stock facts."""
from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, UUIDText
from .purchase_models import DocumentAudit


class WorkOrder(DocumentAudit, Base):
    __tablename__ = "work_order"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    product_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    planned_qty_base: Mapped[str] = mapped_column(Text)
    planned_start: Mapped[str] = mapped_column(Text)
    planned_finish: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    approval_state: Mapped[str] = mapped_column(Text, default="DRAFT")
    bom_id: Mapped[str | None] = mapped_column(UUIDText(), ForeignKey("bom.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    routing_id: Mapped[str | None] = mapped_column(UUIDText(), ForeignKey("routing.id", ondelete="RESTRICT", onupdate="RESTRICT"))
