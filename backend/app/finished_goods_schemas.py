from uuid import UUID
from pydantic import StrictInt, field_validator
from .schemas import Input
from .purchase_schemas import calendar_date
from .decimal_text import canonical_decimal


class ReceiptInput(Input):
    document_no: str
    completion_id: UUID
    completion_version: StrictInt
    warehouse_id: UUID
    lot_id: UUID | None = None
    lot_code: str | None = None
    business_date: str
    quantity: str
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def number(cls, value):
        if not value.strip():
            raise ValueError("成品入庫單號不可空白")
        return value

    @field_validator("lot_code")
    @classmethod
    def lot(cls, value):
        if value == "":
            return None
        if value is not None and not value.strip():
            raise ValueError("批號不可只有空白")
        return value

    @field_validator("business_date", mode="before")
    @classmethod
    def date(cls, value):
        return calendar_date(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def number_text(cls, value):
        return canonical_decimal(value)


class ReceiptCreate(ReceiptInput):
    request_key: UUID


class ReceiptUpdate(ReceiptInput):
    row_version: StrictInt


class PostReceipt(Input):
    request_key: UUID
    row_version: StrictInt
    confirmation_token: str
