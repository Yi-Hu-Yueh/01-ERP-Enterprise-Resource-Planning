from uuid import UUID
from pydantic import StrictInt, field_validator
from .schemas import Input
from .purchase_schemas import calendar_date
from .decimal_text import canonical_decimal


class StocktakeInput(Input):
    document_no: str
    warehouse_id: UUID
    business_date: str
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def number(cls, value):
        if not value.strip(): raise ValueError("盤點單號不可空白")
        return value

    @field_validator("business_date", mode="before")
    @classmethod
    def date(cls, value): return calendar_date(value)


class StocktakeCreate(StocktakeInput):
    request_key: UUID


class StocktakeUpdate(StocktakeInput):
    row_version: StrictInt


class StocktakeAction(Input):
    request_key: UUID
    row_version: StrictInt


class CountUpdate(Input):
    row_version: StrictInt
    stocktake_version: StrictInt
    counted_qty: str | None
    note: str | None = None

    @field_validator("counted_qty", mode="before")
    @classmethod
    def count(cls, value):
        if value is None: return None
        value = canonical_decimal(value)
        if value.startswith("-"): raise ValueError("實際盤點數量不可為負")
        return value


class ExceptionCreate(Input):
    request_key: UUID
    stocktake_version: StrictInt
    description: str

    @field_validator("description")
    @classmethod
    def description_required(cls, value):
        if not value.strip(): raise ValueError("請描述未登錄實物")
        return value


class StocktakeClose(StocktakeAction):
    confirmation_token: str
