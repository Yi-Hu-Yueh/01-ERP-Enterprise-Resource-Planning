"""Routing maintenance identities; operations and centers are reusable, BOM is independent."""
from sqlalchemy import Boolean, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import AuditedMaster, Base, UUIDText
from .purchase_models import DocumentAudit


class OperationDefinition(AuditedMaster, Base):
    __tablename__ = "operation_definition"
    instruction: Mapped[str | None] = mapped_column(Text)


class Routing(DocumentAudit, Base):
    __tablename__ = "routing"
    product_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    revision: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[str] = mapped_column(Text)
    valid_to: Mapped[str | None] = mapped_column(Text)
    release_state: Mapped[str] = mapped_column(Text, default="DRAFT")
    is_active: Mapped[bool] = mapped_column(Boolean)


class RoutingStep(DocumentAudit, Base):
    __tablename__ = "routing_step"
    routing_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("routing.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    sequence_no: Mapped[int] = mapped_column(Integer)
    operation_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("operation_definition.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    work_center_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("work_center.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    setup_minutes: Mapped[str | None] = mapped_column(Text)
    run_minutes_per_base: Mapped[str | None] = mapped_column(Text)
