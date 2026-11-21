"""Single-record purchasing transactions and exact committed-line totals."""
import hashlib
import json
from uuid import UUID

from fastapi import Request, Response
from sqlalchemy import func, or_, select

from .db import DEMO_ACTOR_ID
from .decimal_text import exact_product, exact_sum
from .models import Currency, Item, Supplier, SupplierOffer, Warehouse
from .purchase_models import PurchaseCreateReceipt, PurchaseLine, PurchaseOrder
from .purchase_schemas import HeaderCreate, HeaderOut, HeaderUpdate, LineCreate, LineOut, LineUpdate


def line_output(row):
    return LineOut(**{c.name: getattr(row, c.name) for c in PurchaseLine.__table__.columns},
                   line_amount=exact_product(row.ordered_qty, row.unit_price),
                   ordered_qty_base=exact_product(row.ordered_qty, row.base_factor_snapshot))


def header_output(session, row):
    lines = [line_output(line) for line in session.scalars(select(PurchaseLine).where(
        PurchaseLine.purchase_order_id == row.id).order_by(PurchaseLine.line_no))]
    return HeaderOut(**{c.name: getattr(row, c.name) for c in PurchaseOrder.__table__.columns},
                     lines=lines, line_count=len(lines), total_amount=exact_sum(line.line_amount for line in lines))


def install_purchase_routes(app, fail, references):
    prefix = "/api/purchase-orders"

    def order(session, record_id, version=None):
        row = session.get(PurchaseOrder, str(record_id))
        if row is None:
            fail(404, "採購單不存在，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "採購單已被另一個視窗更新，請重新讀取後再操作。")
            if row.approval_state is not None or row.approved_at is not None or row.approved_by is not None:
                fail(409, "此採購單的核准狀態尚未支援修改或刪除，請保留原資料。")
        return row

    def line(session, order_id, record_id):
        row = session.get(PurchaseLine, str(record_id))
        if row is None or row.purchase_order_id != str(order_id):
            fail(404, "此採購單沒有這筆明細，請重新讀取。")
        return row

    def header_refs(session, data, record_id=None):
        if session.get(Supplier, str(data.supplier_id)) is None:
            fail(422, "供應商不存在，請重新選擇。")
        if session.get(Currency, str(data.currency_id)) is None:
            fail(422, "幣別不存在，請重新選擇。")
        duplicate = select(PurchaseOrder.id).where(PurchaseOrder.document_no == data.document_no)
        if record_id:
            duplicate = duplicate.where(PurchaseOrder.id != record_id)
        if session.scalar(duplicate):
            fail(409, "採購單號已存在，請使用不同單號。")
        if record_id:
            mismatch = select(PurchaseLine.id).join(SupplierOffer, SupplierOffer.id == PurchaseLine.supplier_offer_id).where(
                PurchaseLine.purchase_order_id == record_id, SupplierOffer.supplier_id != str(data.supplier_id))
            if session.scalar(mismatch):
                fail(422, "明細的供料設定與新供應商不符，請先逐筆調整明細的報價依據。")

    def line_refs(session, parent, data):
        item = session.get(Item, str(data.item_id))
        if item is None or item.item_kind != "RAW":
            fail(422, "原料不存在或不是原物料，不能使用成品。")
        if item.base_unit_id != str(data.unit_id):
            fail(422, "目前只支援原料的基本單位；其他單位換算尚待核定。")
        if session.get(Warehouse, str(data.preferred_warehouse_id)) is None:
            fail(422, "預計收貨倉不存在，請重新選擇。")
        if data.supplier_offer_id:
            offer = session.get(SupplierOffer, str(data.supplier_offer_id))
            if offer is None or offer.supplier_id != parent.supplier_id or offer.item_id != str(data.item_id):
                fail(422, "供料設定不存在，或與採購單供應商、原料不符。")

    def fingerprint(kind, data, parent=None):
        value = {"kind": kind, "parent": str(parent) if parent else None,
                 "data": data.model_dump(mode="json", exclude={"request_key", "order_version"})}
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def replay(session, kind, data, parent=None):
        digest = fingerprint(kind, data, parent)
        receipt = session.get(PurchaseCreateReceipt, str(data.request_key))
        if receipt:
            if receipt.resource_kind != kind or receipt.payload_hash != digest:
                fail(409, "新增請求識別已用於不同內容，請重新讀取，勿重複送出。")
            existing = session.get(PurchaseOrder if kind == "header" else PurchaseLine, receipt.record_id)
            if existing is None:
                fail(410, "這次新增請求的資料已刪除，不能重複建立；請使用新的新增操作。")
            return digest, existing
        return digest, None

    def receipt(session, kind, data, row, digest):
        session.add(PurchaseCreateReceipt(request_key=str(data.request_key), resource_kind=kind,
            record_id=row.id, payload_hash=digest, created_by=DEMO_ACTOR_ID))
        session.flush()

    @app.get(prefix, response_model=list[HeaderOut])
    def list_orders(request: Request, q: str = ""):
        statement = select(PurchaseOrder).join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        if q:
            statement = statement.where(or_(*(column.contains(q, autoescape=True) for column in [
                PurchaseOrder.document_no, PurchaseOrder.supplier_name_snapshot, PurchaseOrder.note, Supplier.code, Supplier.name])))
        with request.app.state.db.read() as session:
            return [header_output(session, row) for row in session.scalars(statement.order_by(PurchaseOrder.document_no))]

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
            row = PurchaseOrder(**data.model_dump(mode="json", exclude={"request_key"}), created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            receipt(session, "header", data, row, digest)
            return header_output(session, row)

    @app.put(prefix + "/{record_id}", response_model=HeaderOut)
    def update_order(record_id: UUID, data: HeaderUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = order(session, record_id, data.row_version)
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
            if references(session, "purchase_order", row.id):
                fail(409, "採購單仍有明細或其他引用，無法整單刪除；整單取消規則尚待核定。")
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
            number = (session.scalar(select(func.max(PurchaseLine.line_no)).where(PurchaseLine.purchase_order_id == parent.id)) or 0) + 1
            row = PurchaseLine(**data.model_dump(mode="json", exclude={"request_key", "order_version"}),
                purchase_order_id=parent.id, line_no=number, base_factor_snapshot="1", created_by=DEMO_ACTOR_ID)
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
            if references(session, "purchase_order_line", row.id):
                fail(409, "明細已被引用，無法刪除，請保留原資料。")
            session.delete(row)
            parent.row_version += 1
            session.flush()
