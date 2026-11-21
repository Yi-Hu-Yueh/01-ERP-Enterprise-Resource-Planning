from datetime import date, datetime
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator

from .decimal_text import canonical_decimal
from .schemas import Input


def calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("請使用有效日曆日期")
    return date.fromisoformat(value).isoformat()


class HeaderInput(Input):
    document_no: str
    supplier_id: UUID
    supplier_name_snapshot: str
    currency_id: UUID
    order_date: str
    delivery_address_snapshot: str | None = None
    payment_terms_snapshot: str | None = None
    note: str | None = None

    @field_validator("document_no", "supplier_name_snapshot")
    @classmethod
    def required(cls, value):
        if not value.strip():
            raise ValueError("不可空白")
        return value

    @field_validator("order_date", mode="before")
    @classmethod
    def valid_date(cls, value):
        return calendar_date(value)


class HeaderCreate(HeaderInput):
    request_key: UUID


class HeaderUpdate(HeaderInput):
    row_version: StrictInt


class LineInput(Input):
    item_id: UUID
    description_snapshot: str
    unit_id: UUID
    ordered_qty: str
    unit_price: str
    due_date: str
    preferred_warehouse_id: UUID
    supplier_offer_id: UUID | None = None
    order_version: StrictInt

    @field_validator("ordered_qty", "unit_price", mode="before")
    @classmethod
    def exact_value(cls, value):
        return canonical_decimal(value)

    @field_validator("due_date", mode="before")
    @classmethod
    def valid_date(cls, value):
        return calendar_date(value)

    @field_validator("description_snapshot")
    @classmethod
    def description(cls, value):
        if not value.strip():
            raise ValueError("不可空白")
        return value


class LineCreate(LineInput):
    request_key: UUID


class LineUpdate(LineInput):
    row_version: StrictInt


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    created_by: str
    updated_at: datetime
    row_version: int


class LineOut(AuditOut):
    purchase_order_id: str
    line_no: int
    item_id: str
    description_snapshot: str
    unit_id: str
    base_factor_snapshot: str
    ordered_qty: str
    unit_price: str
    due_date: str
    preferred_warehouse_id: str
    supplier_offer_id: str | None
    line_amount: str
    ordered_qty_base: str


class HeaderOut(AuditOut):
    document_no: str
    supplier_id: str
    supplier_name_snapshot: str
    currency_id: str
    order_date: str
    delivery_address_snapshot: str | None
    payment_terms_snapshot: str | None
    note: str | None
    approval_state: str | None
    approved_at: datetime | None
    approved_by: str | None
    total_amount: str
    line_count: int
    lines: list[LineOut]
