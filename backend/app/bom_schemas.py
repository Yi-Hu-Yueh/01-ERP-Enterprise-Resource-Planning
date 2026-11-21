from decimal import Decimal
from uuid import UUID
from pydantic import StrictBool, StrictInt, field_validator, model_validator
from .schemas import Input
from .purchase_schemas import AuditOut, calendar_date
from .decimal_text import canonical_decimal


class HeaderInput(Input):
    product_id: UUID
    revision: str
    output_basis_qty: str
    valid_from: str
    valid_to: str | None = None
    is_active: StrictBool

    @field_validator("revision")
    @classmethod
    def required(cls, value):
        if not value.strip():
            raise ValueError("版號不可空白")
        return value

    @field_validator("output_basis_qty", mode="before")
    @classmethod
    def basis(cls, value):
        value = canonical_decimal(value)
        if Decimal(value) <= 0:
            raise ValueError("產出基準必須大於零")
        return value

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def dates(cls, value):
        return calendar_date(value) if value is not None else None

    @model_validator(mode="after")
    def interval(self):
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("有效迄日不可早於起日")
        return self


class HeaderCreate(HeaderInput):
    request_key: UUID


class HeaderUpdate(HeaderInput):
    row_version: StrictInt


class ComponentInput(Input):
    material_id: UUID
    usage_qty_base: str
    purpose_note: str | None = None
    bom_version: StrictInt

    @field_validator("usage_qty_base", mode="before")
    @classmethod
    def usage(cls, value):
        return canonical_decimal(value)


class ComponentCreate(ComponentInput):
    request_key: UUID


class ComponentUpdate(ComponentInput):
    row_version: StrictInt


class ComponentOut(AuditOut):
    bom_id: str
    line_no: int
    material_id: str
    usage_qty_base: str
    purpose_note: str | None
    material_code: str
    material_name: str
    material_base_unit_id: str
    material_base_unit_code: str
    material_base_unit_name: str


class HeaderOut(AuditOut):
    product_id: str
    revision: str
    output_basis_qty: str
    valid_from: str
    valid_to: str | None
    release_state: str
    is_active: bool
    product_code: str
    product_name: str
    product_base_unit_id: str
    product_base_unit_code: str
    product_base_unit_name: str
    components: list[ComponentOut]
