from datetime import datetime
from uuid import UUID
from pydantic import BaseModel,StrictInt,field_validator
from .schemas import Input
from .purchase_schemas import AuditOut,calendar_date
from .decimal_text import canonical_decimal
from .work_order_schemas import WorkOrderOut
from .issue_schemas import IssuePosition,IssuePolicy,PostIssue


class MaterialReturnInput(Input):
    document_no:str
    work_order_id:UUID
    work_order_version:StrictInt
    business_date:str
    note:str|None=None
    @field_validator('document_no')
    @classmethod
    def number(cls,value):
        if not value.strip():raise ValueError('退料單號不可空白')
        return value
    @field_validator('business_date',mode='before')
    @classmethod
    def date(cls,value):return calendar_date(value)


class MaterialReturnCreate(MaterialReturnInput):
    request_key:UUID


class MaterialReturnUpdate(MaterialReturnInput):
    row_version:StrictInt


class MaterialReturnLineInput(Input):
    origin_line_id:UUID
    origin_line_version:StrictInt
    quantity:str
    note:str|None=None
    document_version:StrictInt
    @field_validator('quantity',mode='before')
    @classmethod
    def decimal(cls,value):return canonical_decimal(value)


class MaterialReturnLineCreate(MaterialReturnLineInput):
    request_key:UUID


class MaterialReturnLineUpdate(MaterialReturnLineInput):
    row_version:StrictInt


class PostMaterialReturn(PostIssue):
    pass


class ReturnSource(BaseModel):
    origin_line_id:str
    origin_line_version:int
    issue_id:str
    issue_no:str
    issue_line_no:int
    issue_posting_id:str
    issue_posted_at:datetime
    work_order_id:str
    work_order_no:str
    work_order_version:int
    issued_qty_base:str
    returned_qty_base:str
    returnable_qty_base:str
    position:IssuePosition


class MaterialReturnLineOut(AuditOut):
    material_return_id:str
    line_no:int
    origin_line_id:str
    origin_line_version:int
    quantity:str
    note:str|None
    source:ReturnSource
    proposed_qty_base:str
    remaining_after_proposal_qty_base:str


class MaterialReturnMovement(BaseModel):
    id:str
    posting_id:str
    return_line_id:str
    position_id:str
    leg_no:int
    qty_base_delta:str
    created_at:datetime
    created_by:str


class MaterialReturnOut(AuditOut):
    document_no:str
    work_order_id:str
    work_order_version:int
    business_date:str
    note:str|None
    work_order:WorkOrderOut
    posting_state:str
    posting_id:str|None
    posted_at:datetime|None
    posted_by:str|None
    posting_request_key:str|None
    posting_source_version:int|None
    authorization_basis:str|None
    confirmation_digest:str|None
    confirmation_received_at:datetime|None
    policy:IssuePolicy
    lines:list[MaterialReturnLineOut]
    movements:list[MaterialReturnMovement]


class ReturnReviewMovement(BaseModel):
    return_line_id:str
    origin_line_id:str
    position_id:str
    qty_base_delta:str
    unit_code:str
    current_stock_qty_base:str
    expected_stock_qty_base:str


class MaterialReturnReview(BaseModel):
    material_return:MaterialReturnOut
    confirmation_token:str|None
    expected_movements:list[ReturnReviewMovement]
