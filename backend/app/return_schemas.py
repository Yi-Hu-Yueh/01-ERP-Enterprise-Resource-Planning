from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, StrictInt, field_validator

from .decimal_text import canonical_decimal
from .purchase_schemas import AuditOut, calendar_date
from .schemas import Input


class ReturnInput(Input):
    document_no: str
    supplier_id: UUID
    business_date: str
    reason_code: str | None = None
    external_reference: str | None = None
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def required(cls, value):
        if not value.strip():
            raise ValueError("不可空白")
        return value

    @field_validator("business_date", mode="before")
    @classmethod
    def date(cls, value):
        return calendar_date(value)


class ReturnCreate(ReturnInput):
    request_key: UUID


class ReturnUpdate(ReturnInput):
    row_version: StrictInt


class ReturnLineInput(Input):
    origin_line_id: UUID
    quantity: str
    note: str | None = None
    document_version: StrictInt

    @field_validator("quantity", mode="before")
    @classmethod
    def decimal(cls, value):
        return canonical_decimal(value)


class ReturnLineCreate(ReturnLineInput):
    request_key: UUID


class ReturnLineUpdate(ReturnLineInput):
    row_version: StrictInt


class PostReturn(Input):
    request_key: UUID
    row_version: StrictInt


class SourceContext(BaseModel):
    source_receipt_id: str
    source_receipt_no: str
    source_line_no: int
    purchase_order_id: str
    purchase_order_no: str
    purchase_order_line_id: str
    item_id: str
    item_code: str
    item_name: str
    description_snapshot: str
    unit_id: str
    unit_code: str
    unit_name: str
    warehouse_id: str
    warehouse_code: str
    warehouse_name: str
    lot_id: str | None
    lot_code: str | None
    supplier_lot_code: str | None
    lot_tracking: bool
    source_quantity_base: str
    returned_qty_base: str
    returnable_qty_base: str
    on_hand_qty_base: str


class SourceLineOut(SourceContext):
    id: str
    origin_line_id: str
    line_no: int


class SourceReceiptOut(BaseModel):
    id: str
    document_no: str
    business_date: str
    supplier_id: str
    party_name_snapshot: str
    posting_state: str
    lines: list[SourceLineOut]


class ReturnLineOut(AuditOut, SourceContext):
    supplier_return_id: str
    line_no: int
    origin_line_id: str
    origin_line_version: int
    quantity: str
    note: str | None


class ReturnMovementOut(BaseModel):
    id: str
    created_at: datetime
    created_by: str
    posting_id: str
    return_line_id: str
    document_line_id: str
    position_id: str
    leg_no: int
    qty_base_delta: str
    origin_line_id: str
    source_receipt_id: str
    source_receipt_no: str
    purchase_order_id: str
    purchase_order_no: str
    purchase_order_line_id: str
    item_id: str
    item_code: str
    item_name: str
    warehouse_id: str
    warehouse_code: str
    warehouse_name: str
    lot_id: str | None
    lot_code: str | None
    supplier_lot_code: str | None


class ReturnOut(AuditOut):
    document_no: str
    supplier_id: str
    party_name_snapshot: str
    business_date: str
    reason_code: str | None
    external_reference: str | None
    note: str | None
    posting_state: str
    posting_id: str | None
    posted_at: datetime | None
    line_count: int
    lines: list[ReturnLineOut]
    movements: list[ReturnMovementOut]
