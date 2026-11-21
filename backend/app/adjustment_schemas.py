from typing import Literal
from uuid import UUID
from pydantic import StrictInt, field_validator
from .schemas import Input
from .purchase_schemas import calendar_date
from .decimal_text import canonical_decimal


class AdjustmentInput(Input):
    document_no: str
    business_date: str
    position_id: UUID
    direction: Literal["INCREASE", "DECREASE"]
    quantity: str
    reason: str
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def number(cls, value):
        if not value.strip(): raise ValueError("庫存調整單號不可空白")
        return value

    @field_validator("business_date", mode="before")
    @classmethod
    def date(cls, value): return calendar_date(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def decimal(cls, value): return canonical_decimal(value)


class AdjustmentCreate(AdjustmentInput):
    request_key: UUID


class AdjustmentUpdate(AdjustmentInput):
    row_version: StrictInt


class AdjustmentAction(Input):
    request_key: UUID
    row_version: StrictInt


class AdjustmentPost(AdjustmentAction):
    confirmation_token: str
