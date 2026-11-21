"""Read-only selections and exact, non-binding planning results."""
from uuid import UUID
from pydantic import BaseModel, Field, StrictInt
from .schemas import Input


class PreviewInput(Input):
    work_order_ids: list[UUID] = Field(min_length=1)
    warehouse_ids: list[UUID] = Field(min_length=1)
    expected_work_order_versions: dict[UUID, StrictInt] = Field(default_factory=dict)
    expected_bom_versions: dict[UUID, StrictInt] = Field(default_factory=dict)


class Identity(BaseModel):
    id: str
    code: str
    name: str
    row_version: int


class MaterialIdentity(Identity):
    unit_id: str
    unit_code: str
    unit_name: str
    unit_version: int


class Source(BaseModel):
    id: str
    revision: str
    row_version: int
    release_state: str
    output_basis_qty: str


class Contribution(BaseModel):
    component_id: str
    component_version: int
    line_no: int
    purpose_note: str | None
    material: MaterialIdentity
    usage_qty_base: str
    required_qty_base: str | None
    warnings: list[str]


class OrderRequirement(BaseModel):
    id: str
    document_no: str
    row_version: int
    approval_state: str
    product: MaterialIdentity
    planned_qty_base: str
    planned_start: str
    planned_finish: str
    bom: Source | None
    routing_id: str | None
    routing_version: int | None
    complete: bool
    warnings: list[str]
    components: list[Contribution]


class WarehouseScope(Identity):
    is_active: bool
    mrp_included: bool


class Position(BaseModel):
    position_id: str
    warehouse_id: str
    warehouse_code: str
    warehouse_name: str
    lot_id: str | None
    lot_code: str | None
    on_hand_qty_base: str


class WarehouseBalance(BaseModel):
    warehouse_id: str
    on_hand_qty_base: str


class MaterialRequirement(BaseModel):
    material: MaterialIdentity
    calculated_demand_qty_base: str
    gross_demand_qty_base: str | None
    demand_complete: bool
    physical_stock_qty_base: str
    planning_supply_qty_base: str | None
    shortage_qty_base: str | None
    suggested_replenishment_qty_base: str | None
    positions: list[Position]
    warehouses: list[WarehouseBalance]
    warnings: list[str]


class PreviewOut(BaseModel):
    calculated_at: str
    snapshot_started_at: str
    preliminary: bool
    complete: bool
    label: str
    formula: str
    normalized_work_order_ids: list[str]
    normalized_warehouse_ids: list[str]
    duplicate_work_order_ids_removed: int
    duplicate_warehouse_ids_removed: int
    warehouses: list[WarehouseScope]
    orders: list[OrderRequirement]
    materials: list[MaterialRequirement]
    warnings: list[str]
