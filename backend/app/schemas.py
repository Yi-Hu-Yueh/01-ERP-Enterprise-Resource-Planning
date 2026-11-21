from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Named(Input):
    code: str
    name: str

    @field_validator("code", "name")
    @classmethod
    def required_text(cls, value):
        if not value.strip():
            raise ValueError("不可空白")
        return value


class CategoryCreate(Named):
    parent_id: UUID | None = None


class UnitCreate(Named):
    pass


class ItemCreate(Named):
    specification: str | None = None
    category_id: UUID
    base_unit_id: UUID
    lot_tracking: StrictBool
    is_active: StrictBool


class RawCreate(ItemCreate):
    pass


class RawUpdate(RawCreate):
    row_version: int


class FinishedCreate(ItemCreate):
    pass


class FinishedUpdate(FinishedCreate):
    row_version: int


class MasterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    is_active: bool
    created_at: datetime
    created_by: str
    updated_at: datetime
    row_version: int


class CategoryOut(MasterOut):
    parent_id: str | None


class UnitOut(MasterOut):
    quantity_rule: str


class ItemOut(MasterOut):
    specification: str | None
    item_kind: str
    category_id: str
    base_unit_id: str
    lot_tracking: bool



class RawOut(ItemOut):
    pass


class FinishedOut(ItemOut):
    pass


class CurrencyCreate(Named):
    pass


class CurrencyOut(MasterOut):
    rounding_scale: int | None
    rounding_mode: str | None


class PartyCreate(Named):
    tax_identifier: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    payment_terms: str | None = None
    currency_id: UUID
    is_active: StrictBool

    @field_validator("email")
    @classmethod
    def email_format(cls, value):
        if value is None or value == "":
            return None
        from email.headerregistry import Address
        from email.errors import HeaderParseError
        try:
            address = Address(addr_spec=value)
            if not address.username or not address.domain:
                raise ValueError("Email requires a local part and domain")
        except (ValueError, IndexError, HeaderParseError) as error:
            raise ValueError("電子郵件格式不正確") from error
        return value


class CustomerCreate(PartyCreate):
    billing_address: str | None = None
    default_ship_address: str | None = None


class CustomerUpdate(CustomerCreate):
    row_version: int


class SupplierCreate(PartyCreate):
    address: str | None = None


class SupplierUpdate(SupplierCreate):
    row_version: int


class WarehouseCreate(Named):
    address: str | None = None
    mrp_included: StrictBool
    is_active: StrictBool


class WarehouseUpdate(WarehouseCreate):
    row_version: int


class WorkCenterCreate(Named):
    description: str | None = None
    is_active: StrictBool


class WorkCenterUpdate(WorkCenterCreate):
    row_version: int


class PartyOut(MasterOut):
    tax_identifier: str | None
    contact_name: str | None
    phone: str | None
    email: str | None
    payment_terms: str | None
    currency_id: str


class CustomerOut(PartyOut):
    billing_address: str | None
    default_ship_address: str | None


class SupplierOut(PartyOut):
    address: str | None


class WarehouseOut(MasterOut):
    address: str | None
    mrp_included: bool


class WorkCenterOut(MasterOut):
    description: str | None


class OfferCreate(Input):
    supplier_id: UUID
    item_id: UUID
    unit_price: str
    is_active: StrictBool = True

    @field_validator("unit_price", mode="before")
    @classmethod
    def exact_price(cls, value):
        from .decimal_text import canonical_decimal
        return canonical_decimal(value)


class OfferUpdate(OfferCreate):
    row_version: int


class OfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    supplier_id: str
    item_id: str
    unit_price: str
    is_active: bool
    created_at: datetime
    created_by: str
    updated_at: datetime
    row_version: int
    supplier_code: str
    supplier_name: str
    item_code: str
    item_name: str
