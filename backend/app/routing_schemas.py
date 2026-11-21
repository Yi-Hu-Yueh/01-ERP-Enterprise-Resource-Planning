import re
from decimal import Decimal
from uuid import UUID
from pydantic import StrictBool, StrictInt, ValidationInfo, field_validator
from .schemas import Input, Named, MasterOut
from .purchase_schemas import AuditOut, calendar_date
from .decimal_text import canonical_decimal


def sequence_text(value):
    """Transport SQLite INTEGER as text, preserving values beyond JS safe integers."""
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+", value):
        raise ValueError("步序須為整數文字")
    digits = value.lstrip("-").lstrip("0") or "0"
    negative = value.startswith("-") and digits != "0"
    limit = "9223372036854775808" if negative else "9223372036854775807"
    if len(digits) > len(limit) or (len(digits) == len(limit) and digits > limit):
        raise ValueError("超出 SQLite INTEGER 可表示範圍")
    return ("-" if negative else "") + digits


class OperationCreate(Named):
    instruction: str | None = None
    is_active: StrictBool


class OperationUpdate(OperationCreate):
    row_version: StrictInt


class OperationOut(MasterOut):
    instruction: str | None


class HeaderInput(Input):
    product_id: UUID
    revision: str
    valid_from: str
    valid_to: str | None = None
    is_active: StrictBool

    @field_validator("revision")
    @classmethod
    def required(cls, value):
        if not value.strip():
            raise ValueError("版號不可空白")
        return value

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def dates(cls, value):
        return calendar_date(value) if value is not None else None

    @field_validator("valid_to")
    @classmethod
    def interval(cls, value, info: ValidationInfo):
        if value and info.data.get("valid_from") and value < info.data["valid_from"]:
            raise ValueError("有效迄日不可早於起日")
        return value


class HeaderCreate(HeaderInput):
    request_key: UUID


class HeaderUpdate(HeaderInput):
    row_version: StrictInt


class StepInput(Input):
    sequence_no: str
    operation_id: UUID
    work_center_id: UUID
    setup_minutes: str | None = None
    run_minutes_per_base: str | None = None
    routing_version: StrictInt

    @field_validator("sequence_no", mode="before")
    @classmethod
    def sequence(cls, value):
        return sequence_text(value)

    @field_validator("setup_minutes", "run_minutes_per_base", mode="before")
    @classmethod
    def duration(cls, value):
        if value is None:
            return None
        result = canonical_decimal(value)
        if not Decimal(result).is_finite():
            raise ValueError("工時必須為有限十進位文字")
        return result


class StepCreate(StepInput):
    request_key: UUID


class StepUpdate(StepInput):
    row_version: StrictInt


class StepOut(AuditOut):
    routing_id: str
    sequence_no: str
    operation_id: str
    operation_code: str
    operation_name: str
    operation_instruction: str | None
    operation_is_active: bool
    work_center_id: str
    work_center_code: str
    work_center_name: str
    work_center_is_active: bool
    setup_minutes: str | None
    run_minutes_per_base: str | None


class HeaderOut(AuditOut):
    product_id: str
    product_code: str
    product_name: str
    product_base_unit_id: str
    product_base_unit_code: str
    product_base_unit_name: str
    revision: str
    valid_from: str
    valid_to: str | None
    release_state: str
    is_active: bool
    steps: list[StepOut]
