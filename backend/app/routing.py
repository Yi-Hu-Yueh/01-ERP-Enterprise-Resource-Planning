"""Ordered routing draft maintenance; no scheduling, BOM mutations or stock effects."""
import hashlib
import json
from uuid import UUID
from fastapi import Request, Response
from sqlalchemy import or_, select
from .db import DEMO_ACTOR_ID
from .models import Item, Unit, WorkCenter
from .routing_models import OperationDefinition, Routing, RoutingStep
from .routing_schemas import HeaderCreate, HeaderUpdate, HeaderOut, StepCreate, StepUpdate, StepOut
from .receiving_models import StockCreateReceipt


def step_output(session, row):
    operation = session.get(OperationDefinition, row.operation_id)
    center = session.get(WorkCenter, row.work_center_id)
    values = {c.name: getattr(row, c.name) for c in RoutingStep.__table__.columns}
    values["sequence_no"] = str(row.sequence_no)
    return StepOut(**values, operation_code=operation.code, operation_name=operation.name,
        operation_instruction=operation.instruction, operation_is_active=operation.is_active,
        work_center_code=center.code, work_center_name=center.name, work_center_is_active=center.is_active)


def header_output(session, row):
    product = session.get(Item, row.product_id)
    unit = session.get(Unit, product.base_unit_id)
    return HeaderOut(**{c.name: getattr(row, c.name) for c in Routing.__table__.columns},
        product_code=product.code, product_name=product.name, product_base_unit_id=unit.id,
        product_base_unit_code=unit.code, product_base_unit_name=unit.name,
        steps=[step_output(session, s) for s in session.scalars(select(RoutingStep).where(
            RoutingStep.routing_id == row.id).order_by(RoutingStep.sequence_no))])


def install_routing_routes(app, fail, references):
    prefix = "/api/routings"

    def header(session, record_id, version=None):
        row = session.get(Routing, str(record_id))
        if row is None:
            fail(404, "途程不存在，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "途程已被另一個視窗更新，請重新讀取後再操作。")
            if row.release_state != "DRAFT" or references(session, "routing", row.id, exclude_tables={"routing_step"}):
                fail(409, "途程已被引用或不是草稿，無法修改或刪除；請保留原資料。")
        return row

    def step(session, parent_id, record_id, version=None):
        row = session.get(RoutingStep, str(record_id))
        if row is None or row.routing_id != str(parent_id):
            fail(404, "此途程沒有這筆步驟，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "步驟已更新，請重新讀取後再操作。")
            if references(session, "routing_step", row.id):
                fail(409, "步驟已被引用，無法修改或刪除；請保留原資料。")
        return row

    def header_refs(session, data, row=None):
        item = session.get(Item, str(data.product_id))
        if item is None or item.item_kind != "FINISHED":
            fail(422, "途程產品必須是既有成品，不能使用原物料。")
        duplicate = select(Routing.id).where(Routing.product_id == item.id, Routing.revision == data.revision)
        if row:
            duplicate = duplicate.where(Routing.id != row.id)
            if row.product_id != item.id and session.scalar(select(RoutingStep.id).where(RoutingStep.routing_id == row.id).limit(1)):
                fail(409, "途程已有步驟，無法變更產品或重新解讀工時基準；請建立另一途程。")
        if session.scalar(duplicate):
            fail(409, "此成品的途程版號已存在，請使用不同版號。")

    def step_refs(session, parent, data, row=None):
        for model, field, label in [(OperationDefinition, "operation_id", "作業"), (WorkCenter, "work_center_id", "工作中心")]:
            reference = session.get(model, str(getattr(data, field)))
            if reference is None:
                fail(422, label+"不存在，請重新選擇。")
            if not reference.is_active and (row is None or getattr(row, field) != reference.id):
                fail(422, label+"已停用，不能新增引用；既有引用可保留。")
        duplicate = select(RoutingStep.id).where(RoutingStep.routing_id == parent.id, RoutingStep.sequence_no == int(data.sequence_no))
        if row:
            duplicate = duplicate.where(RoutingStep.id != row.id)
        if session.scalar(duplicate):
            fail(409, "此途程的步序已存在，請使用空閒步序；不會自動重排其他步驟。")

    def replay(session, kind, data, parent=None):
        payload = {"domain": "routing-create", "kind": kind, "parent": str(parent) if parent else None,
                   "data": data.model_dump(mode="json", exclude={"request_key", "routing_version"})}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt = session.get(StockCreateReceipt, str(data.request_key))
        if receipt:
            if receipt.resource_kind != kind or receipt.payload_hash != digest:
                fail(409, "新增請求識別已用於不同內容，請重新讀取。")
            row = session.get(Routing if kind == "header" else RoutingStep, receipt.record_id)
            if row is None:
                fail(410, "這次新增請求的資料已刪除，不能重複建立；請開始新的新增操作。")
            return digest, row
        return digest, None

    def remember(session, kind, data, row, digest):
        session.add(StockCreateReceipt(request_key=str(data.request_key), resource_kind=kind, record_id=row.id,
            payload_hash=digest, created_by=DEMO_ACTOR_ID))
        session.flush()

    @app.get(prefix, response_model=list[HeaderOut])
    def list_routings(request: Request, q: str = ""):
        statement = select(Routing).join(Item, Item.id == Routing.product_id)
        if q:
            matching_step = select(RoutingStep.id).join(OperationDefinition, OperationDefinition.id == RoutingStep.operation_id).join(
                WorkCenter, WorkCenter.id == RoutingStep.work_center_id).where(RoutingStep.routing_id == Routing.id,
                or_(*(column.contains(q, autoescape=True) for column in [
                    OperationDefinition.code, OperationDefinition.name, WorkCenter.code, WorkCenter.name]))).exists()
            statement = statement.where(or_(Item.code.contains(q, autoescape=True), Item.name.contains(q, autoescape=True),
                                             Routing.revision.contains(q, autoescape=True), matching_step))
        with request.app.state.db.read() as session:
            return [header_output(session, row) for row in session.scalars(statement.order_by(Item.code, Routing.revision))]

    @app.get(prefix+"/{record_id}", response_model=HeaderOut)
    def read_routing(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return header_output(session, header(session, record_id))

    @app.post(prefix, response_model=HeaderOut, status_code=201)
    def create_routing(data: HeaderCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest, row = replay(session, "header", data)
            if row:
                response.status_code = 200
                return header_output(session, row)
            header_refs(session, data)
            row = Routing(**data.model_dump(mode="json", exclude={"request_key"}), created_by=DEMO_ACTOR_ID)
            session.add(row);session.flush()
            remember(session, "header", data, row, digest)
            return header_output(session, row)

    @app.put(prefix+"/{record_id}", response_model=HeaderOut)
    def update_routing(record_id: UUID, data: HeaderUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = header(session, record_id, data.row_version)
            header_refs(session, data, row)
            for key, value in data.model_dump(mode="json", exclude={"row_version"}).items():
                setattr(row, key, value)
            row.row_version += 1;session.flush()
            return header_output(session, row)

    @app.delete(prefix+"/{record_id}", status_code=204)
    def delete_routing(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = header(session, record_id, row_version)
            if references(session, "routing", row.id):
                fail(409, "途程仍有步驟或其他引用，無法刪除；請逐筆處理未被引用的草稿步驟。")
            session.delete(row);session.flush()

    @app.get(prefix+"/{routing_id}/steps", response_model=list[StepOut])
    def list_steps(routing_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return header_output(session, header(session, routing_id)).steps

    @app.get(prefix+"/{routing_id}/steps/{record_id}", response_model=StepOut)
    def read_step(routing_id: UUID, record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            header(session, routing_id)
            return step_output(session, step(session, routing_id, record_id))

    @app.post(prefix+"/{routing_id}/steps", response_model=StepOut, status_code=201)
    def create_step(routing_id: UUID, data: StepCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest, row = replay(session, "line", data, routing_id)
            if row:
                response.status_code = 200
                return step_output(session, row)
            parent = header(session, routing_id, data.routing_version)
            step_refs(session, parent, data)
            values = data.model_dump(mode="json", exclude={"request_key", "routing_version"})
            values["sequence_no"] = int(data.sequence_no)
            row = RoutingStep(**values, routing_id=parent.id, created_by=DEMO_ACTOR_ID)
            session.add(row);parent.row_version += 1;session.flush()
            remember(session, "line", data, row, digest)
            return step_output(session, row)

    @app.put(prefix+"/{routing_id}/steps/{record_id}", response_model=StepOut)
    def update_step(routing_id: UUID, record_id: UUID, data: StepUpdate, request: Request):
        with request.app.state.db.write() as session:
            parent = header(session, routing_id, data.routing_version)
            row = step(session, parent.id, record_id, data.row_version)
            step_refs(session, parent, data, row)
            values = data.model_dump(mode="json", exclude={"row_version", "routing_version"})
            values["sequence_no"] = int(data.sequence_no)
            for key, value in values.items():
                setattr(row, key, value)
            row.row_version += 1;parent.row_version += 1;session.flush()
            return step_output(session, row)

    @app.delete(prefix+"/{routing_id}/steps/{record_id}", status_code=204)
    def delete_step(routing_id: UUID, record_id: UUID, row_version: int, routing_version: int, request: Request):
        with request.app.state.db.write() as session:
            parent = header(session, routing_id, routing_version)
            row = step(session, parent.id, record_id, row_version)
            session.delete(row);parent.row_version += 1;session.flush()
