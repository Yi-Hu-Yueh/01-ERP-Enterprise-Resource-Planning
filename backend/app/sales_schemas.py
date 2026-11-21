from uuid import UUID
from pydantic import StrictInt, field_validator
from .schemas import Input
from .decimal_text import canonical_decimal
from .purchase_schemas import AuditOut, calendar_date


class HeaderInput(Input):
    document_no: str
    customer_id: UUID
    order_date: str
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def required(cls, value):
        if not value.strip():
            raise ValueError("不可空白")
        return value

    @field_validator("order_date", mode="before")
    @classmethod
    def date(cls, value):
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
    order_version: StrictInt

    @field_validator("ordered_qty", "unit_price", mode="before")
    @classmethod
    def decimal(cls, value):
        return canonical_decimal(value)

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


class LineOut(AuditOut):
    sales_order_id: str
    line_no: int
    item_id: str
    description_snapshot: str
    unit_id: str
    ordered_qty: str
    unit_price: str
    line_amount: str


class HeaderOut(AuditOut):
    document_no: str
    customer_id: str
    customer_code: str
    customer_name: str
    order_date: str
    note: str | None
    state: str
    total_amount: str
    line_count: int
    lines: list[LineOut]
