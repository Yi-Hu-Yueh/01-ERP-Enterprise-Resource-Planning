from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator

from .decimal_text import canonical_decimal
from .purchase_schemas import AuditOut, calendar_date
from .schemas import Input


class ReceiptInput(Input):
    document_no: str
    purchase_order_id: UUID
    business_date: str
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
    def valid_date(cls, value):
        return calendar_date(value)


class ReceiptCreate(ReceiptInput):
    request_key: UUID


class ReceiptUpdate(ReceiptInput):
    row_version: StrictInt


class ReceiptLineInput(Input):
    purchase_order_line_id: UUID
    quantity: str
    warehouse_id: UUID
    lot_code: str | None = None
    supplier_lot_code: str | None = None
    manufactured_date: str | None = None
    expiry_date: str | None = None
    note: str | None = None
    document_version: StrictInt

    @field_validator("quantity", mode="before")
    @classmethod
    def exact_quantity(cls, value):
        return canonical_decimal(value)

    @field_validator("lot_code", "supplier_lot_code")
    @classmethod
    def optional_code(cls, value):
        return value if value and value.strip() else None

    @field_validator("manufactured_date", "expiry_date", mode="before")
    @classmethod
    def optional_date(cls, value):
        return None if value is None or value == "" else calendar_date(value)


class ReceiptLineCreate(ReceiptLineInput):
    request_key: UUID


class ReceiptLineUpdate(ReceiptLineInput):
    row_version: StrictInt


class PostReceipt(Input):
    request_key: UUID
    row_version: StrictInt


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    item_id: str
    warehouse_id: str
    lot_id: str | None
    created_at: datetime
    created_by: str
    item_code: str
    item_name: str
    warehouse_code: str
    warehouse_name: str
    lot_code: str | None
    on_hand_qty_base: str


class MovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    posting_id: str
    document_line_id: str
    position_id: str
    leg_no: int
    qty_base_delta: str
    created_at: datetime
    created_by: str
    item_id: str
    warehouse_id: str
    lot_id: str | None
    lot_code: str | None


class ReceiptLineOut(AuditOut):
    stock_document_id: str
    line_no: int
    purchase_order_line_id: str
    purchase_order_line_version: int
    item_id: str
    description_snapshot: str
    quantity: str
    unit_id: str
    base_factor_snapshot: str
    warehouse_id: str
    lot_id: str | None
    lot_code: str | None
    supplier_lot_code: str | None
    manufactured_date: str | None
    expiry_date: str | None
    note: str | None
    item_code: str
    lot_tracking: bool
    ordered_qty_base: str
    received_qty_base: str
    remaining_qty_base: str
    quantity_base: str
    on_hand_qty_base: str


class ReceiptOut(AuditOut):
    document_no: str
    document_kind: str
    purchase_order_id: str
    purchase_order_version: int
    business_date: str
    supplier_id: str
    party_name_snapshot: str
    external_reference: str | None
    note: str | None
    approval_state: str | None
    approved_at: datetime | None
    approved_by: str | None
    order_document_no: str
    order_total_amount: str
    posting_state: str
    posting_id: str | None
    posted_at: datetime | None
    line_count: int
    lines: list[ReceiptLineOut]
    movements: list[MovementOut]
