from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from .schemas import Input
from .purchase_schemas import AuditOut, calendar_date
from .decimal_text import canonical_decimal
from .work_order_schemas import WorkOrderOut


class IssueInput(Input):
    document_no: str
    work_order_id: UUID
    work_order_version: StrictInt
    business_date: str
    note: str | None = None

    @field_validator("document_no")
    @classmethod
    def number(cls,value):
        if not value.strip():
            raise ValueError("領料單號不可空白")
        return value

    @field_validator("business_date",mode="before")
    @classmethod
    def date(cls,value):
        return calendar_date(value)


class IssueCreate(IssueInput):
    request_key: UUID


class IssueUpdate(IssueInput):
    row_version: StrictInt


class IssueLineInput(Input):
    material_id: UUID
    warehouse_id: UUID
    position_id: UUID
    bom_component_id: UUID | None = None
    bom_component_version: StrictInt | None = None
    quantity: str
    note: str | None = None
    document_version: StrictInt

    @field_validator("quantity",mode="before")
    @classmethod
    def decimal(cls,value):
        return canonical_decimal(value)

    @model_validator(mode="after")
    def paired(self):
        if (self.bom_component_id is None)!=(self.bom_component_version is None):
            raise ValueError("用料來源及資料版次須同時提供或留空")
        return self


class IssueLineCreate(IssueLineInput):
    request_key: UUID


class IssueLineUpdate(IssueLineInput):
    row_version: StrictInt


class PostIssue(Input):
    request_key: UUID
    row_version: StrictInt
    work_order_version: StrictInt
    confirmation_token: str | None = Field(default=None,min_length=1)


class IssuePolicy(BaseModel):
    posting_allowed: bool
    authorization_basis: str | None
    message: str


class ReceiptOrigin(BaseModel):
    receipt_id: str
    receipt_no: str
    receipt_line_id: str
    quantity_base: str


class IssuePosition(BaseModel):
    id: str
    material_id: str
    material_code: str
    material_name: str
    warehouse_id: str
    warehouse_code: str
    warehouse_name: str
    lot_id: str | None
    lot_code: str | None
    unit_id: str
    unit_code: str
    unit_name: str
    physical_qty_base: str
    receipt_origins: list[ReceiptOrigin]


class IssueLineOut(AuditOut):
    material_issue_id: str
    line_no: int
    material_id: str
    warehouse_id: str
    position_id: str
    unit_id: str
    bom_component_id: str | None
    bom_component_version: int | None
    quantity: str
    note: str | None
    position: IssuePosition
    component_purpose: str | None


class IssueMovement(BaseModel):
    id: str
    posting_id: str
    issue_line_id: str
    position_id: str
    qty_base_delta: str
    created_at: datetime
    created_by: str


class IssueOut(AuditOut):
    document_no: str
    work_order_id: str
    work_order_version: int
    bom_id: str | None
    bom_version: int | None
    routing_id: str | None
    routing_version: int | None
    business_date: str
    note: str | None
    work_order: WorkOrderOut
    posting_state: str
    posting_id: str | None
    posted_at: datetime | None
    posted_by: str | None
    posting_request_key: str | None
    posting_source_version: int | None
    authorization_basis: str | None
    confirmation_digest: str | None = None
    confirmation_received_at: datetime | None = None
    policy: IssuePolicy
    lines: list[IssueLineOut]
    movements: list[IssueMovement]


class ReviewMovement(BaseModel):
    issue_line_id: str
    position_id: str
    qty_base_delta: str
    unit_code: str


class IssueReview(BaseModel):
    issue: IssueOut
    confirmation_token: str
    expected_movements: list[ReviewMovement]
