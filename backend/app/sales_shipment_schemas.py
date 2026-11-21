from uuid import UUID
from pydantic import StrictInt, field_validator
from .schemas import Input
from .purchase_schemas import calendar_date
from .decimal_text import canonical_decimal


class ShipmentInput(Input):
    document_no: str
    sales_order_line_id: UUID
    line_version: StrictInt
    order_version: StrictInt
    position_id: UUID
    business_date: str
    quantity: str
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def number(cls,value):
        if not value.strip():raise ValueError("銷售出貨單號不可空白")
        return value

    @field_validator("business_date",mode="before")
    @classmethod
    def date(cls,value):return calendar_date(value)

    @field_validator("quantity",mode="before")
    @classmethod
    def decimal(cls,value):return canonical_decimal(value)


class ShipmentCreate(ShipmentInput):
    request_key: UUID


class ShipmentUpdate(ShipmentInput):
    row_version: StrictInt


class PostShipment(Input):
    request_key: UUID
    row_version: StrictInt
    confirmation_token: str
