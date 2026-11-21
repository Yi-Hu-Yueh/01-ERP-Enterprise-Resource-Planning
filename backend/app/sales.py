"""Single-record customer demand and exact committed-line totals; no stock writes."""
import hashlib
import json
from uuid import UUID

from fastapi import Request, Response
from sqlalchemy import func, or_, select

from .db import DEMO_ACTOR_ID
from .decimal_text import exact_product, exact_sum
from .models import Customer, Item
from .sales_models import SalesCreateReceipt, SalesLine, SalesOrder
from .sales_schemas import HeaderCreate, HeaderOut, HeaderUpdate, LineCreate, LineOut, LineUpdate
from .sales_shipments import protect_source_header_edit, protect_source_line_edit


def line_output(row):
    return LineOut(**{c.name: getattr(row, c.name) for c in SalesLine.__table__.columns},
                   line_amount=exact_product(row.ordered_qty, row.unit_price))


def header_output(session, row):
    lines = [line_output(line) for line in session.scalars(select(SalesLine).where(
        SalesLine.sales_order_id == row.id).order_by(SalesLine.line_no))]
    return HeaderOut(**{c.name: getattr(row, c.name) for c in SalesOrder.__table__.columns},
                     customer_code=session.get(Customer,row.customer_id).code, customer_name=session.get(Customer,row.customer_id).name,
                     lines=lines, line_count=len(lines), total_amount=exact_sum(line.line_amount for line in lines))


def install_sales_routes(app, fail, references):
    prefix = "/api/sales-orders"

    def order(session, record_id, version=None):
        row = session.get(SalesOrder, str(record_id))
        if row is None:
            fail(404, "銷售單不存在，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "銷售單已被另一個視窗更新，請重新讀取後再操作。")
            if row.state != 'DRAFT':
                fail(409, "此銷售單的核准狀態尚未支援修改或刪除，請保留原資料。")
        return row

    def line(session, order_id, record_id):
        row = session.get(SalesLine, str(record_id))
        if row is None or row.sales_order_id != str(order_id):
            fail(404, "此銷售單沒有這筆明細，請重新讀取。")
        return row

    def header_refs(session, data, record_id=None):
        if session.get(Customer, str(data.customer_id)) is None:
            fail(422, "客戶不存在，請重新選擇。")
        duplicate = select(SalesOrder.id).where(SalesOrder.document_no == data.document_no)
        if record_id:
            duplicate = duplicate.where(SalesOrder.id != record_id)
        if session.scalar(duplicate):
            fail(409, "銷售單號已存在，請使用不同單號。")
    def line_refs(session, parent, data):
        item = session.get(Item, str(data.item_id))
        if item is None or item.item_kind != "FINISHED":
            fail(422, "成品不存在或類型不符，只能選擇成品。")
        if item.base_unit_id != str(data.unit_id):
            fail(422, "目前只支援成品的基本單位；其他單位換算尚待核定。")
    def fingerprint(kind, data, parent=None):
        value = {"kind": kind, "parent": str(parent) if parent else None,
                 "data": data.model_dump(mode="json", exclude={"request_key", "order_version"})}
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def replay(session, kind, data, parent=None):
        digest = fingerprint(kind, data, parent)
        receipt = session.get(SalesCreateReceipt, str(data.request_key))
        if receipt:
            if receipt.resource_kind != kind or receipt.payload_hash != digest:
                fail(409, "新增請求識別已用於不同內容，請重新讀取，勿重複送出。")
            existing = session.get(SalesOrder if kind == "header" else SalesLine, receipt.record_id)
            if existing is None:
                fail(410, "這次新增請求的資料已刪除，不能重複建立；請使用新的新增操作。")
            return digest, existing
        return digest, None

    def receipt(session, kind, data, row, digest):
        session.add(SalesCreateReceipt(request_key=str(data.request_key), resource_kind=kind,
            record_id=row.id, payload_hash=digest, created_by=DEMO_ACTOR_ID))
        session.flush()

    @app.get(prefix, response_model=list[HeaderOut])
    def list_orders(request: Request, q: str = ""):
        statement = select(SalesOrder).join(Customer, Customer.id == SalesOrder.customer_id)
        if q:
            statement = statement.where(or_(*(column.contains(q, autoescape=True) for column in [
                SalesOrder.document_no, SalesOrder.note, Customer.code, Customer.name])))
        with request.app.state.db.read() as session:
            return [header_output(session, row) for row in session.scalars(statement.order_by(SalesOrder.document_no))]

    @app.get(prefix + "/{record_id}", response_model=HeaderOut)
    def read_order(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return header_output(session, order(session, record_id))

    @app.post(prefix, response_model=HeaderOut, status_code=201)
    def create_order(data: HeaderCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest, existing = replay(session, "header", data)
            if existing:
                response.status_code = 200
                return header_output(session, existing)
            header_refs(session, data)
            row = SalesOrder(**data.model_dump(mode="json", exclude={"request_key"}), created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            receipt(session, "header", data, row, digest)
            return header_output(session, row)

    @app.put(prefix + "/{record_id}", response_model=HeaderOut)
    def update_order(record_id: UUID, data: HeaderUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = order(session, record_id, data.row_version)
            # Approval snapshots report a changed source instead of imposing a
            # new edit gate. Existing shipment/return protection below remains.
            if references(session, "sales_order", row.id, exclude_tables=("sales_order_line","sales_shipment","customer_return","approval_request")):
                fail(409, "銷售單已被下游引用，請保留原資料。")
            protect_source_header_edit(session,row,data,fail)
            header_refs(session, data, row.id)
            for key, value in data.model_dump(mode="json", exclude={"row_version"}).items():
                setattr(row, key, value)
            row.row_version += 1
            session.flush()
            return header_output(session, row)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_order(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = order(session, record_id, row_version)
            if references(session, "sales_order", row.id):
                fail(409, "銷售單仍有明細或其他引用，無法整單刪除；整單取消規則尚待核定。")
            session.delete(row)
            session.flush()

    @app.get(prefix + "/{order_id}/lines", response_model=list[LineOut])
    def list_lines(order_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return header_output(session, order(session, order_id)).lines

    @app.get(prefix + "/{order_id}/lines/{record_id}", response_model=LineOut)
    def read_line(order_id: UUID, record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            order(session, order_id)
            return line_output(line(session, order_id, record_id))

    @app.post(prefix + "/{order_id}/lines", response_model=LineOut, status_code=201)
    def create_line(order_id: UUID, data: LineCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest, existing = replay(session, "line", data, order_id)
            if existing:
                response.status_code = 200
                return line_output(existing)
            parent = order(session, order_id, data.order_version)
            line_refs(session, parent, data)
            number = (session.scalar(select(func.max(SalesLine.line_no)).where(SalesLine.sales_order_id == parent.id)) or 0) + 1
            row = SalesLine(**data.model_dump(mode="json", exclude={"request_key", "order_version"}),
                sales_order_id=parent.id, line_no=number, created_by=DEMO_ACTOR_ID)
            session.add(row)
            parent.row_version += 1
            session.flush()
            receipt(session, "line", data, row, digest)
            return line_output(row)

    @app.put(prefix + "/{order_id}/lines/{record_id}", response_model=LineOut)
    def update_line(order_id: UUID, record_id: UUID, data: LineUpdate, request: Request):
        with request.app.state.db.write() as session:
            parent = order(session, order_id, data.order_version)
            row = line(session, parent.id, record_id)
            if row.row_version != data.row_version:
                fail(409, "明細已被另一個視窗更新，請重新讀取後再操作。")
            if references(session, "sales_order_line", row.id, exclude_tables=("sales_shipment","customer_return")):
                fail(409, "明細已被下游引用，請保留原資料。")
            protect_source_line_edit(session,row,data,fail)
            line_refs(session, parent, data)
            for key, value in data.model_dump(mode="json", exclude={"row_version", "order_version"}).items():
                setattr(row, key, value)
            row.row_version += 1
            parent.row_version += 1
            session.flush()
            return line_output(row)

    @app.delete(prefix + "/{order_id}/lines/{record_id}", status_code=204)
    def delete_line(order_id: UUID, record_id: UUID, row_version: int, order_version: int, request: Request):
        with request.app.state.db.write() as session:
            parent = order(session, order_id, order_version)
            row = line(session, parent.id, record_id)
            if row.row_version != row_version:
                fail(409, "明細已更新，請重新讀取後再刪除。")
            if references(session, "sales_order_line", row.id):
                fail(409, "明細已被引用，無法刪除，請保留原資料。")
            session.delete(row)
            parent.row_version += 1
            session.flush()
