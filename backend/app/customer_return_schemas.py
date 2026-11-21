from uuid import UUID
from pydantic import StrictBool, StrictInt, field_validator
from .schemas import Input
from .purchase_schemas import calendar_date
from .decimal_text import canonical_decimal


class ReturnInput(Input):
    document_no: str
    shipment_id: UUID
    shipment_version: StrictInt
    business_date: str
    quantity: str
    restock_eligible: StrictBool
    reason: str | None = None
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def number(cls, value):
        if not value.strip(): raise ValueError("客戶退貨單號不可空白")
        return value

    @field_validator("business_date", mode="before")
    @classmethod
    def date(cls, value): return calendar_date(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def decimal(cls, value): return canonical_decimal(value)


class ReturnCreate(ReturnInput):
    request_key: UUID


class ReturnUpdate(ReturnInput):
    row_version: StrictInt


class FinalizeReturn(Input):
    request_key: UUID
    row_version: StrictInt
    confirmation_token: str
