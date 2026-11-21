"""One committed draft header/component per request; exact text, no stock writes."""
import hashlib
import json
from uuid import UUID
from fastapi import Request, Response
from sqlalchemy import func, or_, select
from .db import DEMO_ACTOR_ID
from .models import Item, Unit
from .bom_models import Bom, BomComponent
from .bom_schemas import HeaderCreate, HeaderUpdate, HeaderOut, ComponentCreate, ComponentUpdate, ComponentOut
from .receiving_models import StockCreateReceipt


def item_fields(session, item_id, prefix):
    item = session.get(Item, item_id)
    unit = session.get(Unit, item.base_unit_id)
    return {prefix+"_code": item.code, prefix+"_name": item.name,
            prefix+"_base_unit_id": unit.id, prefix+"_base_unit_code": unit.code, prefix+"_base_unit_name": unit.name}


def component_output(session, row):
    return ComponentOut(**{c.name: getattr(row, c.name) for c in BomComponent.__table__.columns},
                        **item_fields(session, row.material_id, "material"))


def header_output(session, row):
    return HeaderOut(**{c.name: getattr(row, c.name) for c in Bom.__table__.columns},
        **item_fields(session, row.product_id, "product"),
        components=[component_output(session, c) for c in session.scalars(
            select(BomComponent).where(BomComponent.bom_id == row.id).order_by(BomComponent.line_no))])


def install_bom_routes(app, fail, references):
    prefix = "/api/boms"

    def header(session, record_id, version=None):
        row = session.get(Bom, str(record_id))
        if not row:
            fail(404, "BOM 不存在，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "BOM 已被另一個視窗更新，請重新讀取後再操作。")
            if row.release_state != "DRAFT":
                fail(409, "此 BOM 版本已封存，無法修改或刪除；請保留歷史資料。")
            if references(session, "bom", row.id, exclude_tables={"bom_component"}):
                fail(409, "BOM 已被其他資料引用，無法修改或刪除；請保留原資料。")
        return row

    def component(session, parent_id, record_id, version=None):
        row = session.get(BomComponent, str(record_id))
        if not row or row.bom_id != str(parent_id):
            fail(404, "此 BOM 沒有這筆用料明細，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "用料明細已更新，請重新讀取後再操作。")
            if references(session, "bom_component", row.id):
                fail(409, "用料明細已被引用，無法修改或刪除；請保留原資料。")
        return row

    def typed_item(session, record_id, kind):
        row = session.get(Item, str(record_id))
        if not row or row.item_kind != kind:
            fail(422, "BOM 產品必須是既有成品，用料必須是既有原物料。")
        return row

    def header_refs(session, data, row=None):
        typed_item(session, data.product_id, "FINISHED")
        duplicate = select(Bom.id).where(Bom.product_id == str(data.product_id), Bom.revision == data.revision)
        if row:
            duplicate = duplicate.where(Bom.id != row.id)
            if row.product_id != str(data.product_id) and session.scalar(
                select(BomComponent.id).where(BomComponent.bom_id == row.id).limit(1)):
                fail(409, "BOM 已有用料明細，無法變更產品；請建立另一產品的 BOM。")
        if session.scalar(duplicate):
            fail(409, "此成品的 BOM 版號已存在，請使用不同版號。")

    def replay(session, kind, data, parent=None):
        value = {"domain": "bom-create", "kind": kind, "parent": str(parent) if parent else None,
                 "data": data.model_dump(mode="json", exclude={"request_key", "bom_version"})}
        digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt = session.get(StockCreateReceipt, str(data.request_key))
        if receipt:
            if receipt.resource_kind != kind or receipt.payload_hash != digest:
                fail(409, "新增請求識別已用於不同內容，請重新讀取。")
            existing = session.get(Bom if kind == "header" else BomComponent, receipt.record_id)
            if not existing:
                fail(410, "這次新增請求的資料已刪除，不能重複建立；請開始新的新增操作。")
            return digest, existing
        return digest, None

    def receipt(session, kind, data, row, digest):
        session.add(StockCreateReceipt(request_key=str(data.request_key), resource_kind=kind,
            record_id=row.id, payload_hash=digest, created_by=DEMO_ACTOR_ID))
        session.flush()

    @app.get(prefix, response_model=list[HeaderOut])
    def list_boms(request: Request, q: str = ""):
        statement = select(Bom).join(Item, Item.id == Bom.product_id)
        if q:
            matching_component = select(BomComponent.id).join(Item, Item.id == BomComponent.material_id).where(
                BomComponent.bom_id == Bom.id, or_(Item.code.contains(q, autoescape=True), Item.name.contains(q, autoescape=True))).exists()
            statement = statement.where(or_(Item.code.contains(q, autoescape=True), Item.name.contains(q, autoescape=True),
                                            Bom.revision.contains(q, autoescape=True), matching_component))
        with request.app.state.db.read() as session:
            return [header_output(session, row) for row in session.scalars(statement.order_by(Item.code, Bom.revision))]

    @app.get(prefix+"/{record_id}", response_model=HeaderOut)
    def read_bom(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return header_output(session, header(session, record_id))

    @app.post(prefix, response_model=HeaderOut, status_code=201)
    def create_bom(data: HeaderCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest, existing = replay(session, "header", data)
            if existing:
                response.status_code = 200
                return header_output(session, existing)
            header_refs(session, data)
            row = Bom(**data.model_dump(mode="json", exclude={"request_key"}), created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            receipt(session, "header", data, row, digest)
            return header_output(session, row)

    @app.put(prefix+"/{record_id}", response_model=HeaderOut)
    def update_bom(record_id: UUID, data: HeaderUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = header(session, record_id, data.row_version)
            header_refs(session, data, row)
            for key, value in data.model_dump(mode="json", exclude={"row_version"}).items():
                setattr(row, key, value)
            row.row_version += 1
            session.flush()
            return header_output(session, row)

    @app.delete(prefix+"/{record_id}", status_code=204)
    def delete_bom(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = header(session, record_id, row_version)
            if references(session, "bom", row.id):
                fail(409, "BOM 仍有用料明細或其他引用，無法刪除；請逐筆處理未被引用的明細。")
            session.delete(row)
            session.flush()

    @app.get(prefix+"/{bom_id}/components", response_model=list[ComponentOut])
    def list_components(bom_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return header_output(session, header(session, bom_id)).components

    @app.get(prefix+"/{bom_id}/components/{record_id}", response_model=ComponentOut)
    def read_component(bom_id: UUID, record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            header(session, bom_id)
            return component_output(session, component(session, bom_id, record_id))

    @app.post(prefix+"/{bom_id}/components", response_model=ComponentOut, status_code=201)
    def create_component(bom_id: UUID, data: ComponentCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest, existing = replay(session, "line", data, bom_id)
            if existing:
                response.status_code = 200
                return component_output(session, existing)
            parent = header(session, bom_id, data.bom_version)
            typed_item(session, data.material_id, "RAW")
            number = (session.scalar(select(func.max(BomComponent.line_no)).where(BomComponent.bom_id == parent.id)) or 0)+1
            row = BomComponent(**data.model_dump(mode="json", exclude={"request_key", "bom_version"}),
                bom_id=parent.id, line_no=number, created_by=DEMO_ACTOR_ID)
            session.add(row)
            parent.row_version += 1
            session.flush()
            receipt(session, "line", data, row, digest)
            return component_output(session, row)

    @app.put(prefix+"/{bom_id}/components/{record_id}", response_model=ComponentOut)
    def update_component(bom_id: UUID, record_id: UUID, data: ComponentUpdate, request: Request):
        with request.app.state.db.write() as session:
            parent = header(session, bom_id, data.bom_version)
            row = component(session, parent.id, record_id, data.row_version)
            typed_item(session, data.material_id, "RAW")
            for key, value in data.model_dump(mode="json", exclude={"row_version", "bom_version"}).items():
                setattr(row, key, value)
            row.row_version += 1
            parent.row_version += 1
            session.flush()
            return component_output(session, row)

    @app.delete(prefix+"/{bom_id}/components/{record_id}", status_code=204)
    def delete_component(bom_id: UUID, record_id: UUID, row_version: int, bom_version: int, request: Request):
        with request.app.state.db.write() as session:
            parent = header(session, bom_id, bom_version)
            row = component(session, parent.id, record_id, row_version)
            session.delete(row)
            parent.row_version += 1
            session.flush()
