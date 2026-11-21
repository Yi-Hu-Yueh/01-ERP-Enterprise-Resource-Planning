from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, StrictInt, field_validator, model_validator
from .schemas import Input
from .purchase_schemas import AuditOut, calendar_date
from .decimal_text import canonical_decimal


class WorkOrderInput(Input):
    document_no: str
    product_id: UUID
    planned_qty_base: str
    planned_start: str
    planned_finish: str
    note: str | None = None
    bom_id: UUID | None = None
    routing_id: UUID | None = None
    # Request-time CAS guards, not persisted manufacturing snapshots.
    bom_version: StrictInt | None = None
    routing_version: StrictInt | None = None

    @field_validator("document_no")
    @classmethod
    def number(cls, value):
        if not value.strip():
            raise ValueError("工單號不可空白")
        return value

    @field_validator("planned_qty_base", mode="before")
    @classmethod
    def quantity(cls, value):
        result = canonical_decimal(value)
        if not Decimal(result).is_finite():
            raise ValueError("計畫量必須為有限十進位文字")
        return result

    @field_validator("planned_start", "planned_finish", mode="before")
    @classmethod
    def dates(cls, value):
        return calendar_date(value)

    @model_validator(mode="after")
    def selected_versions(self):
        for field in ["bom", "routing"]:
            selected = getattr(self, field+"_id")
            version = getattr(self, field+"_version")
            if (selected is None) != (version is None):
                raise ValueError("來源識別與資料版次必須一同提供或留空")
        return self


class WorkOrderCreate(WorkOrderInput):
    request_key: UUID


class WorkOrderUpdate(WorkOrderInput):
    row_version: StrictInt


class SourceOut(BaseModel):
    id: str
    product_id: str
    revision: str
    release_state: str
    is_active: bool
    row_version: int


class WorkOrderOut(AuditOut):
    document_no: str
    product_id: str
    product_code: str
    product_name: str
    product_specification: str | None
    product_base_unit_id: str
    product_base_unit_code: str
    product_base_unit_name: str
    planned_qty_base: str
    planned_start: str
    planned_finish: str
    note: str | None
    approval_state: str
    bom_id: str | None
    routing_id: str | None
    bom: SourceOut | None
    routing: SourceOut | None
    date_order_warning: str | None
