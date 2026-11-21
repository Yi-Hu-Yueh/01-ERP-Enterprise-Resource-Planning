from uuid import UUID
from pydantic import StrictInt, field_validator
from .schemas import Input
from .purchase_schemas import calendar_date
from .decimal_text import canonical_decimal


class CompletionInput(Input):
    document_no: str
    work_order_id: UUID
    work_order_version: StrictInt
    report_date: str
    good_qty_base: str
    defective_qty_base: str
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def number(cls, value):
        if not value.strip():
            raise ValueError("完工回報單號不可空白")
        return value

    @field_validator("report_date", mode="before")
    @classmethod
    def date(cls, value):
        return calendar_date(value)

    @field_validator("good_qty_base", "defective_qty_base", mode="before")
    @classmethod
    def quantity(cls, value):
        return canonical_decimal(value)


class CompletionCreate(CompletionInput):
    request_key: UUID


class CompletionUpdate(CompletionInput):
    row_version: StrictInt


class CompletionConfirm(Input):
    request_key: UUID
    row_version: StrictInt
    confirmation_token: str
