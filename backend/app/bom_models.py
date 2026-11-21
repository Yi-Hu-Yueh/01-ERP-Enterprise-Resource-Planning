"""Draft BOM maintenance; shared FINISHED/RAW identities, no inventory effects."""
from sqlalchemy import Boolean, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, UUIDText
from .purchase_models import DocumentAudit


class Bom(DocumentAudit, Base):
    __tablename__ = "bom"
    product_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    revision: Mapped[str] = mapped_column(Text)
    output_basis_qty: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[str] = mapped_column(Text)
    valid_to: Mapped[str | None] = mapped_column(Text)
    release_state: Mapped[str] = mapped_column(Text, default="DRAFT")
    is_active: Mapped[bool] = mapped_column(Boolean)


class BomComponent(DocumentAudit, Base):
    __tablename__ = "bom_component"
    bom_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("bom.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    line_no: Mapped[int] = mapped_column(Integer)
    material_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    usage_qty_base: Mapped[str] = mapped_column(Text)
    purpose_note: Mapped[str | None] = mapped_column(Text)
