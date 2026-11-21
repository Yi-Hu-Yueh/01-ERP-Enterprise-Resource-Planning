"""Authorized item/reference and business masters. No stock or order tables."""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def now_utc():
    return datetime.now(timezone.utc)


class UUIDText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(UUID(str(value)))


class UTCText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamp requires a timezone")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def process_result_value(self, value, dialect):
        return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


class Base(DeclarativeBase):
    pass


class Master:
    id: Mapped[str] = mapped_column(UUIDText(), primary_key=True, default=lambda: str(uuid4()))
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean(create_constraint=True), default=True)
    created_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc, onupdate=now_utc)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Actor(Base):
    __tablename__ = "actor"
    id: Mapped[str] = mapped_column(UUIDText(), primary_key=True)
    login_key: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    external_subject: Mapped[str | None] = mapped_column(Text, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean(create_constraint=True), default=True)
    created_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Unit(Master, Base):
    __tablename__ = "unit"
    quantity_rule: Mapped[str] = mapped_column(Text, default="PENDING")
    created_by: Mapped[str] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))


class Category(Master, Base):
    __tablename__ = "item_category"
    parent_id: Mapped[str | None] = mapped_column(UUIDText(), ForeignKey("item_category.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    created_by: Mapped[str] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))


class Item(Master, Base):
    __tablename__ = "item"
    specification: Mapped[str | None] = mapped_column(Text)
    item_kind: Mapped[str] = mapped_column(Text, default="RAW")
    category_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item_category.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    base_unit_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("unit.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    lot_tracking: Mapped[bool] = mapped_column(Boolean(create_constraint=True))
    created_by: Mapped[str] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))



class AuditedMaster(Master):
    created_by: Mapped[str] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"), index=True)


class Currency(AuditedMaster, Base):
    __tablename__ = "currency"
    # Identity support only. D04 precision/rounding choices remain explicitly unset.
    rounding_scale: Mapped[int | None] = mapped_column(Integer)
    rounding_mode: Mapped[str | None] = mapped_column(Text)


class PartyFields:
    tax_identifier: Mapped[str | None] = mapped_column(Text)
    contact_name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    currency_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("currency.id", ondelete="RESTRICT", onupdate="RESTRICT"), index=True)


class Customer(PartyFields, AuditedMaster, Base):
    __tablename__ = "customer"
    billing_address: Mapped[str | None] = mapped_column(Text)
    default_ship_address: Mapped[str | None] = mapped_column(Text)


class Supplier(PartyFields, AuditedMaster, Base):
    __tablename__ = "supplier"
    address: Mapped[str | None] = mapped_column(Text)


class Warehouse(AuditedMaster, Base):
    __tablename__ = "warehouse"
    address: Mapped[str | None] = mapped_column(Text)
    mrp_included: Mapped[bool] = mapped_column(Boolean(create_constraint=True))


class WorkCenter(AuditedMaster, Base):
    __tablename__ = "work_center"
    description: Mapped[str | None] = mapped_column(Text)


class SupplierOffer(Base):
    """Current supplier/material association; no quote history or price tiers."""
    __tablename__ = "supplier_offer"
    id: Mapped[str] = mapped_column(UUIDText(), primary_key=True, default=lambda: str(uuid4()))
    supplier_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("supplier.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    item_id: Mapped[str] = mapped_column(UUIDText(), ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    unit_price: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean(create_constraint=True), default=True)
    created_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc)
    created_by: Mapped[str] = mapped_column(UUIDText(), ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"))
    updated_at: Mapped[datetime] = mapped_column(UTCText(), default=now_utc, onupdate=now_utc)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
