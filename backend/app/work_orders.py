"""Non-posting work-order drafts with explicit same-product source associations."""
import hashlib
import json
from uuid import UUID
from fastapi import Request, Response
from sqlalchemy import or_, select
from .db import DEMO_ACTOR_ID
from .models import Item, Unit
from .bom_models import Bom
from .routing_models import Routing
from .receiving_models import StockCreateReceipt
from .work_order_models import WorkOrder
from .work_order_schemas import WorkOrderCreate, WorkOrderUpdate, WorkOrderOut, SourceOut

REQUEST_FIELDS = {"request_key", "row_version", "bom_version", "routing_version"}
DATE_ORDER_WARNING = "預計完工日早於開工日；日期順序政策待核定，此草稿未排程。"


def source_output(session, model, record_id):
    if record_id is None:
        return None
    source = session.get(model, record_id)
    return SourceOut(**{k: getattr(source, k) for k in SourceOut.model_fields})


def output(session, row):
    product = session.get(Item, row.product_id)
    unit = session.get(Unit, product.base_unit_id)
    return WorkOrderOut(**{c.name: getattr(row, c.name) for c in WorkOrder.__table__.columns},
        product_code=product.code, product_name=product.name, product_specification=product.specification,
        product_base_unit_id=unit.id, product_base_unit_code=unit.code, product_base_unit_name=unit.name,
        bom=source_output(session, Bom, row.bom_id), routing=source_output(session, Routing, row.routing_id),
        date_order_warning=DATE_ORDER_WARNING if row.planned_finish < row.planned_start else None)


def install_work_order_routes(app, fail, references):
    prefix = "/api/work-orders"

    def find(session, record_id, version=None, *, editing=False):
        row = session.get(WorkOrder, str(record_id))
        if row is None:
            fail(404, "製造工單不存在，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "工單已被另一個視窗更新，請重新讀取後再操作。")
            # This review layer detects source changes; it must not become a new
            # posting/edit gate. Keep every pre-existing reference restriction
            # and retain approval-history protection when deleting the source.
            excluded = ("approval_request",) if editing else ()
            if row.approval_state != "DRAFT" or references(session, "work_order", row.id, exclude_tables=excluded):
                fail(409, "工單已被引用或不是草稿，無法修改或刪除；請保留原資料。")
        return row

    def validate(session, data, record_id=None):
        product = session.get(Item, str(data.product_id))
        if product is None or product.item_kind != "FINISHED":
            fail(422, "工單產出必須引用既有成品，不能使用原物料。")
        duplicate = select(WorkOrder.id).where(WorkOrder.document_no == data.document_no)
        if record_id:
            duplicate = duplicate.where(WorkOrder.id != record_id)
        if session.scalar(duplicate):
            fail(409, "製造工單號已存在，請使用不同單號。")
        for model, field, label in [(Bom, "bom", "BOM"), (Routing, "routing", "途程")]:
            selected = getattr(data, field+"_id")
            if selected is None:
                continue
            source = session.get(model, str(selected))
            if source is None or source.product_id != product.id:
                fail(422, label+"不存在或不屬於此成品；請明確清除或重新選取，系統不會自動替換。")
            if source.row_version != getattr(data, field+"_version"):
                fail(409, label+"資料版次已變更，請重新讀取並確認選取；這不是核准或發放版本快照。")

    def fingerprint(data):
        payload = {"domain": "work-order-create", "data": data.model_dump(mode="json", exclude={"request_key"})}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @app.get(prefix, response_model=list[WorkOrderOut])
    def list_orders(request: Request, q: str = ""):
        statement = select(WorkOrder).join(Item, Item.id == WorkOrder.product_id).outerjoin(
            Bom, Bom.id == WorkOrder.bom_id).outerjoin(Routing, Routing.id == WorkOrder.routing_id)
        if q:
            statement = statement.where(or_(*(column.contains(q, autoescape=True) for column in [
                WorkOrder.document_no, WorkOrder.note, Item.code, Item.name, Bom.revision, Routing.revision])))
        with request.app.state.db.read() as session:
            return [output(session, row) for row in session.scalars(statement.order_by(WorkOrder.document_no))]

    @app.get(prefix+"/{record_id}", response_model=WorkOrderOut)
    def read_order(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return output(session, find(session, record_id))

    @app.post(prefix, response_model=WorkOrderOut, status_code=201)
    def create_order(data: WorkOrderCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest = fingerprint(data)
            receipt = session.get(StockCreateReceipt, str(data.request_key))
            if receipt is not None:
                if receipt.resource_kind != "header" or receipt.payload_hash != digest:
                    fail(409, "新增請求識別已用於不同內容，請重新讀取。")
                row = session.get(WorkOrder, receipt.record_id)
                if row is None:
                    fail(410, "這次新增請求的工單已刪除，不能重複建立；請開始新的新增操作。")
                response.status_code = 200
                return output(session, row)
            validate(session, data)
            row = WorkOrder(**data.model_dump(mode="json", exclude=REQUEST_FIELDS), created_by=DEMO_ACTOR_ID)
            session.add(row);session.flush()
            session.add(StockCreateReceipt(request_key=str(data.request_key), resource_kind="header",
                record_id=row.id, payload_hash=digest, created_by=DEMO_ACTOR_ID))
            session.flush()
            return output(session, row)

    @app.put(prefix+"/{record_id}", response_model=WorkOrderOut)
    def update_order(record_id: UUID, data: WorkOrderUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, data.row_version, editing=True)
            validate(session, data, row.id)
            for key, value in data.model_dump(mode="json", exclude=REQUEST_FIELDS).items():
                setattr(row, key, value)
            row.row_version += 1;session.flush()
            return output(session, row)

    @app.delete(prefix+"/{record_id}", status_code=204)
    def delete_order(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, row_version)
            session.delete(row);session.flush()
