from uuid import UUID
from pydantic import StrictInt, field_validator
from .schemas import Input
from .purchase_schemas import calendar_date
from .decimal_text import canonical_decimal
from .stock_ledger import compare


class TransferInput(Input):
    document_no: str
    business_date: str
    source_position_id: UUID
    destination_warehouse_id: UUID
    quantity: str
    reason: str
    note: str | None = None

    @field_validator("document_no", "reason")
    @classmethod
    def meaningful(cls, value):
        if not value.strip(): raise ValueError("調撥單號與原因不可空白")
        return value

    @field_validator("business_date", mode="before")
    @classmethod
    def date(cls, value): return calendar_date(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def decimal(cls, value):
        value = canonical_decimal(value)
        if compare(value, "0") <= 0: raise ValueError("調撥數量必須大於零")
        return value


class TransferCreate(TransferInput):
    request_key: UUID


class TransferUpdate(TransferInput):
    row_version: StrictInt


class TransferAction(Input):
    request_key: UUID
    row_version: StrictInt


class TransferPost(TransferAction):
    confirmation_token: str
