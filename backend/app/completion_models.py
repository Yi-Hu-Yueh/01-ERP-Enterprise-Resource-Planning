"""One production event per report; deliberately no inventory relationships."""
from datetime import datetime
from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, UUIDText, UTCText
from .purchase_models import DocumentAudit


class ProductionCompletion(DocumentAudit, Base):
    __tablename__ = "production_completion"
    document_no: Mapped[str] = mapped_column(Text, unique=True)
    work_order_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("work_order.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    product_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    unit_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("unit.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    report_date: Mapped[str] = mapped_column(Text)
    good_qty_base: Mapped[str] = mapped_column(Text)
    defective_qty_base: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="DRAFT")
    confirmation_id: Mapped[str | None] = mapped_column(UUIDText(), unique=True)
    confirmation_fingerprint: Mapped[str | None] = mapped_column(Text)
    confirmation_snapshot: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCText())
    confirmed_by: Mapped[str | None] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    authorization_basis: Mapped[str | None] = mapped_column(Text)
