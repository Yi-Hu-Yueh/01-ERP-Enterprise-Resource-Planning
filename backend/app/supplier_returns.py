"""Receipt-origin supplier returns using the shared exact physical stock ledger."""
import hashlib
import json
from collections import defaultdict
from uuid import UUID

from fastapi import Request, Response
from sqlalchemy import func, or_, select

from .db import DEMO_ACTOR_ID
from .decimal_text import exact_difference, exact_product, exact_sum
from .models import Item, Unit, Warehouse, Supplier
from .purchase_models import PurchaseOrder
from .receiving import fields
from .receiving_models import InventoryLot, StockCreateReceipt, StockDocument, StockDocumentLine, StockEntry, StockPosition, StockPosting
from .return_models import SupplierReturn, SupplierReturnLine, SupplierReturnPosting, SupplierReturnEntry
from .return_schemas import (PostReturn, ReturnCreate, ReturnLineCreate, ReturnLineOut, ReturnLineUpdate,
                             ReturnMovementOut, ReturnOut, ReturnUpdate, SourceLineOut, SourceReceiptOut)
from .stock_ledger import compare, negate, on_hand, returned


def source_context(session, origin):
    receipt = session.get(StockDocument, origin.stock_document_id)
    order, item = session.get(PurchaseOrder, receipt.purchase_order_id), session.get(Item, origin.item_id)
    unit, warehouse = session.get(Unit, origin.unit_id), session.get(Warehouse, origin.warehouse_id)
    lot = session.get(InventoryLot, origin.lot_id) if origin.lot_id else None
    position = session.scalar(select(StockPosition).where(StockPosition.item_id == origin.item_id,
        StockPosition.warehouse_id == origin.warehouse_id, StockPosition.lot_id == origin.lot_id))
    accepted, prior = exact_product(origin.quantity, origin.base_factor_snapshot), returned(session, origin.id)
    return dict(source_receipt_id=receipt.id, source_receipt_no=receipt.document_no, source_line_no=origin.line_no,
        purchase_order_id=order.id, purchase_order_no=order.document_no, purchase_order_line_id=origin.purchase_order_line_id,
        item_id=item.id, item_code=item.code, item_name=item.name, description_snapshot=origin.description_snapshot,
        unit_id=unit.id, unit_code=unit.code, unit_name=unit.name,
        warehouse_id=warehouse.id, warehouse_code=warehouse.code, warehouse_name=warehouse.name,
        lot_id=origin.lot_id, lot_code=lot.lot_code if lot else None, supplier_lot_code=lot.supplier_lot_code if lot else None,
        lot_tracking=item.lot_tracking, source_quantity_base=accepted, returned_qty_base=prior,
        returnable_qty_base=exact_difference(accepted, prior), on_hand_qty_base=on_hand(session, position.id) if position else "0")


def line_output(session, row):
    return ReturnLineOut(**fields(row), **source_context(session, session.get(StockDocumentLine, row.origin_line_id)))


def return_output(session, row):
    posting = session.scalar(select(SupplierReturnPosting).where(SupplierReturnPosting.supplier_return_id == row.id))
    state = "DRAFT" if posting is None else ("POSTED" if posting.posting_state == "POSTED" else "INCOMPLETE")
    lines = list(session.scalars(select(SupplierReturnLine).where(SupplierReturnLine.supplier_return_id == row.id).order_by(SupplierReturnLine.line_no)))
    movements = []
    if state == "POSTED":
        for entry in session.scalars(select(SupplierReturnEntry).where(SupplierReturnEntry.posting_id == posting.id).order_by(SupplierReturnEntry.return_line_id)):
            detail = session.get(SupplierReturnLine, entry.return_line_id)
            context = source_context(session, session.get(StockDocumentLine, detail.origin_line_id))
            movements.append(ReturnMovementOut(**fields(entry), document_line_id=detail.id, origin_line_id=detail.origin_line_id, **context))
    return ReturnOut(**fields(row), posting_state=state, posting_id=posting.id if posting else None,
        posted_at=posting.posted_at if state == "POSTED" else None, line_count=len(lines),
        lines=[line_output(session, line) for line in lines], movements=movements)


def install_return_routes(app, fail, references):
    prefix = "/api/supplier-returns"

    def find(session, record_id):
        row = session.get(SupplierReturn, str(record_id))
        if row is None:
            fail(404, "供應商退貨單不存在，請重新讀取。")
        return row

    def draft(session, record_id, version):
        row = find(session, record_id)
        if session.scalar(select(SupplierReturnPosting.id).where(SupplierReturnPosting.supplier_return_id == row.id)):
            fail(409, "退貨單已有過帳紀錄，僅可檢視，不能修改或刪除。")
        if row.row_version != version:
            fail(409, "退貨單已被另一個視窗更新，請重新讀取。")
        return row

    def supplier(session, supplier_id):
        row = session.get(Supplier, str(supplier_id))
        if row is None:
            fail(422, "供應商不存在，請重新選擇。")
        return row

    def origin(session, origin_id, supplier_id=None):
        row = session.get(StockDocumentLine, str(origin_id))
        if row is None:
            fail(422, "原收貨明細不存在，請選擇已過帳的採購入庫明細。")
        receipt = session.get(StockDocument, row.stock_document_id)
        posting = session.scalar(select(StockPosting).where(StockPosting.stock_document_id == receipt.id, StockPosting.posting_state == "POSTED"))
        if receipt.document_kind != "PURCHASE_RECEIPT" or posting is None:
            fail(422, "僅能從成功過帳的原採購入庫明細退貨。")
        if supplier_id is not None and receipt.supplier_id != str(supplier_id):
            fail(422, "原收貨明細不屬於退貨供應商，不能跨供應商退貨。")
        item = session.get(Item, row.item_id)
        if item is None or item.item_kind != "RAW" or item.base_unit_id != row.unit_id or row.base_factor_snapshot != "1":
            fail(409, "原收貨料品或基本單位關聯不一致，請保留原資料並進行檢查。")
        entries = list(session.scalars(select(StockEntry).where(StockEntry.posting_id == posting.id, StockEntry.document_line_id == row.id)))
        if len(entries) != 1:
            fail(409, "原收貨庫存分錄不完整，不能退貨，請保留資料進行檢查。")
        entry = entries[0]
        position = session.get(StockPosition, entry.position_id)
        if (position is None or entry.leg_no != 1 or entry.qty_base_delta != exact_product(row.quantity, row.base_factor_snapshot)
            or (position.item_id, position.warehouse_id, position.lot_id) != (row.item_id, row.warehouse_id, row.lot_id)):
            fail(409, "原收貨庫存部位或數量不一致，不能退貨，請保留資料進行檢查。")
        return row, position

    def find_line(session, parent_id, record_id):
        row = session.get(SupplierReturnLine, str(record_id))
        if row is None or row.supplier_return_id != str(parent_id):
            fail(404, "這張退貨單沒有此筆明細，請重新讀取。")
        return row

    def fingerprint(kind, data, parent=None):
        value = {"domain": "supplier-return", "kind": kind, "parent": str(parent) if parent else None,
                 "data": data.model_dump(mode="json", exclude={"request_key", "document_version"})}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def replay(session, kind, data, parent=None):
        digest = fingerprint(kind, data, parent)
        receipt = session.get(StockCreateReceipt, str(data.request_key))
        if receipt:
            if receipt.resource_kind != kind or receipt.payload_hash != digest:
                fail(409, "新增請求識別已用於不同作業或內容，請重新讀取。")
            existing = session.get(SupplierReturn if kind == "header" else SupplierReturnLine, receipt.record_id)
            if existing is None:
                fail(410, "原新增資料已刪除，不能以原請求重建；請使用新的新增操作。")
            return digest, existing
        return digest, None

    def remember(session, kind, data, row, digest):
        session.add(StockCreateReceipt(request_key=str(data.request_key), resource_kind=kind,
            record_id=row.id, payload_hash=digest, created_by=DEMO_ACTOR_ID))
        session.flush()

    def receipt_output(session, row):
        lines = list(session.scalars(select(StockDocumentLine).where(StockDocumentLine.stock_document_id == row.id).order_by(StockDocumentLine.line_no)))
        return SourceReceiptOut(id=row.id, document_no=row.document_no, business_date=row.business_date,
            supplier_id=row.supplier_id, party_name_snapshot=row.party_name_snapshot, posting_state="POSTED",
            lines=[SourceLineOut(id=line.id, origin_line_id=line.id, line_no=line.line_no, **source_context(session, line)) for line in lines])

    @app.get(prefix + "/source-receipts", response_model=list[SourceReceiptOut])
    def source_receipts(request: Request, supplier_id: UUID | None = None):
        query = select(StockDocument).join(StockPosting, StockPosting.stock_document_id == StockDocument.id).where(
            StockPosting.posting_state == "POSTED", StockDocument.document_kind == "PURCHASE_RECEIPT")
        if supplier_id:
            query = query.where(StockDocument.supplier_id == str(supplier_id))
        with request.app.state.db.read() as session:
            if supplier_id:
                supplier(session, supplier_id)
            return [receipt_output(session, row) for row in session.scalars(query.order_by(StockDocument.document_no))]

    @app.get(prefix + "/source-receipts/{record_id}", response_model=SourceReceiptOut)
    def source_receipt(record_id: UUID, request: Request, supplier_id: UUID | None = None):
        with request.app.state.db.read() as session:
            row = session.get(StockDocument, str(record_id))
            if row is None:
                fail(404, "原採購入庫單不存在。")
            if supplier_id is not None and row.supplier_id != str(supplier_id):
                fail(422, "原採購入庫單不屬於所選供應商。")
            if row.document_kind != "PURCHASE_RECEIPT" or not session.scalar(select(StockPosting.id).where(
                StockPosting.stock_document_id == row.id, StockPosting.posting_state == "POSTED")):
                fail(422, "原採購入庫單尚未成功過帳，不能作為退貨來源。")
            return receipt_output(session, row)

    @app.get(prefix, response_model=list[ReturnOut])
    def list_returns(request: Request, q: str = ""):
        query = select(SupplierReturn)
        if q:
            query = query.where(or_(*(column.contains(q, autoescape=True) for column in [SupplierReturn.document_no,
                SupplierReturn.party_name_snapshot, SupplierReturn.external_reference, SupplierReturn.reason_code])))
        with request.app.state.db.read() as session:
            return [return_output(session, row) for row in session.scalars(query.order_by(SupplierReturn.document_no))]

    @app.get(prefix + "/{record_id}", response_model=ReturnOut)
    def read_return(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return return_output(session, find(session, record_id))

    @app.post(prefix, response_model=ReturnOut, status_code=201)
    def create_return(data: ReturnCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest, existing = replay(session, "header", data)
            if existing:
                response.status_code = 200
                return return_output(session, existing)
            party = supplier(session, data.supplier_id)
            if session.scalar(select(SupplierReturn.id).where(SupplierReturn.document_no == data.document_no)):
                fail(409, "退貨單號已存在，請使用不同單號。")
            row = SupplierReturn(**data.model_dump(mode="json", exclude={"request_key"}), party_name_snapshot=party.name, created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            remember(session, "header", data, row, digest)
            return return_output(session, row)

    @app.put(prefix + "/{record_id}", response_model=ReturnOut)
    def update_return(record_id: UUID, data: ReturnUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = draft(session, record_id, data.row_version)
            party = supplier(session, data.supplier_id)
            if row.supplier_id != party.id and session.scalar(select(SupplierReturnLine.id).where(SupplierReturnLine.supplier_return_id == row.id)):
                fail(409, "退貨單已有來源明細，不能改掛供應商。")
            if session.scalar(select(SupplierReturn.id).where(SupplierReturn.document_no == data.document_no, SupplierReturn.id != row.id)):
                fail(409, "退貨單號已存在，請使用不同單號。")
            for key, value in data.model_dump(mode="json", exclude={"row_version"}).items():
                setattr(row, key, value)
            row.party_name_snapshot = party.name
            row.row_version += 1
            session.flush()
            return return_output(session, row)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_return(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = draft(session, record_id, row_version)
            if references(session, "supplier_return", row.id):
                fail(409, "退貨草稿仍有明細或引用，請先逐筆處理，不能整單連帶刪除。")
            session.delete(row)
            session.flush()

    @app.get(prefix + "/{document_id}/lines", response_model=list[ReturnLineOut])
    def read_lines(document_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            find(session, document_id)
            return [line_output(session, row) for row in session.scalars(select(SupplierReturnLine).where(
                SupplierReturnLine.supplier_return_id == str(document_id)).order_by(SupplierReturnLine.line_no))]

    @app.get(prefix + "/{document_id}/lines/{record_id}", response_model=ReturnLineOut)
    def read_line(document_id: UUID, record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            find(session, document_id)
            return line_output(session, find_line(session, document_id, record_id))

    @app.post(prefix + "/{document_id}/lines", response_model=ReturnLineOut, status_code=201)
    def create_line(document_id: UUID, data: ReturnLineCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest, existing = replay(session, "line", data, document_id)
            if existing:
                response.status_code = 200
                return line_output(session, existing)
            parent = draft(session, document_id, data.document_version)
            source, _ = origin(session, data.origin_line_id, parent.supplier_id)
            number = (session.scalar(select(func.max(SupplierReturnLine.line_no)).where(SupplierReturnLine.supplier_return_id == parent.id)) or 0)+1
            row = SupplierReturnLine(**data.model_dump(mode="json", exclude={"request_key", "document_version"}),
                supplier_return_id=parent.id, line_no=number, origin_line_version=source.row_version, created_by=DEMO_ACTOR_ID)
            session.add(row)
            parent.row_version += 1
            session.flush()
            remember(session, "line", data, row, digest)
            return line_output(session, row)

    @app.put(prefix + "/{document_id}/lines/{record_id}", response_model=ReturnLineOut)
    def update_line(document_id: UUID, record_id: UUID, data: ReturnLineUpdate, request: Request):
        with request.app.state.db.write() as session:
            parent = draft(session, document_id, data.document_version)
            row = find_line(session, parent.id, record_id)
            if row.row_version != data.row_version:
                fail(409, "退貨明細已更新，請重新讀取。")
            source, _ = origin(session, data.origin_line_id, parent.supplier_id)
            for key, value in data.model_dump(mode="json", exclude={"document_version", "row_version"}).items():
                setattr(row, key, value)
            row.origin_line_version = source.row_version
            row.row_version += 1
            parent.row_version += 1
            session.flush()
            return line_output(session, row)

    @app.delete(prefix + "/{document_id}/lines/{record_id}", status_code=204)
    def delete_line(document_id: UUID, record_id: UUID, row_version: int, document_version: int, request: Request):
        with request.app.state.db.write() as session:
            parent = draft(session, document_id, document_version)
            row = find_line(session, parent.id, record_id)
            if row.row_version != row_version:
                fail(409, "退貨明細已更新，請重新讀取。")
            if references(session, "supplier_return_line", row.id):
                fail(409, "退貨明細已被引用，不能刪除。")
            session.delete(row)
            parent.row_version += 1
            session.flush()

    def posting_fingerprint(parent, lines, version):
        excluded = {"created_at", "created_by", "updated_at", "row_version"}
        body = {"domain": "supplier-return-posting", "version": version,
                "header": {key: value for key, value in fields(parent).items() if key not in excluded},
                "lines": [{key: value for key, value in fields(row).items() if key not in excluded} for row in lines]}
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    @app.post(prefix + "/{document_id}/post", response_model=ReturnOut, status_code=201)
    def post_return(document_id: UUID, data: PostReturn, request: Request, response: Response):
        with request.app.state.db.write() as session:
            parent = find(session, document_id)
            lines = list(session.scalars(select(SupplierReturnLine).where(SupplierReturnLine.supplier_return_id == parent.id).order_by(SupplierReturnLine.line_no)))
            digest = posting_fingerprint(parent, lines, data.row_version)
            if session.scalar(select(StockPosting.id).where(StockPosting.idempotency_key == str(data.request_key))):
                fail(409, "過帳請求識別已用於採購入庫，不能用於退貨。")
            by_key = session.scalar(select(SupplierReturnPosting).where(SupplierReturnPosting.idempotency_key == str(data.request_key)))
            if by_key:
                if by_key.supplier_return_id != parent.id or by_key.request_fingerprint != digest:
                    fail(409, "過帳請求識別已用於不同退貨內容，請勿重複送出。")
                if by_key.posting_state != "POSTED":
                    fail(409, "退貨過帳紀錄不完整，請保留資料並進行獨立檢查。")
                response.status_code = 200
                return return_output(session, parent)
            prior = session.scalar(select(SupplierReturnPosting).where(SupplierReturnPosting.supplier_return_id == parent.id))
            if prior:
                if prior.posting_state != "POSTED":
                    fail(409, "退貨過帳紀錄不完整，請保留資料並進行獨立檢查。")
                response.status_code = 200
                return return_output(session, parent)
            parent = draft(session, parent.id, data.row_version)
            if not lines:
                fail(422, "退貨單沒有明細，不能過帳。")
            proposed_by_origin, proposed_by_position = defaultdict(list), defaultdict(list)
            sources, positions = {}, {}
            for row in lines:
                if compare(row.quantity, "0") <= 0:
                    fail(422, "退貨為向外出庫，退貨量必須大於零；負量不會轉作入庫或沖回。")
                source, position = origin(session, row.origin_line_id, parent.supplier_id)
                if row.origin_line_version != source.row_version:
                    fail(409, "原收貨明細版次不一致，請保留資料並重新檢查。")
                sources[source.id], positions[row.id] = source, position
                proposed_by_origin[source.id].append(row.quantity)
                proposed_by_position[position.id].append(row.quantity)
            for origin_id, amounts in proposed_by_origin.items():
                eligible = exact_difference(sources[origin_id].quantity, returned(session, origin_id))
                if compare(exact_sum(amounts), eligible) > 0:
                    fail(422, "退貨量超過該原收貨明細的剩餘可退量，請重新確認。")
            for position_id, amounts in proposed_by_position.items():
                if compare(exact_sum(amounts), on_hand(session, position_id)) > 0:
                    fail(422, "原料原倉原批的實際庫存不足，不能執行未核准的負庫存退貨。")
            parent.row_version += 1
            session.flush()
            posting = SupplierReturnPosting(supplier_return_id=parent.id, idempotency_key=str(data.request_key),
                request_fingerprint=digest, source_version=data.row_version, posting_state="PREPARING",
                posted_by=DEMO_ACTOR_ID, created_by=DEMO_ACTOR_ID)
            session.add(posting)
            session.flush()
            for row in lines:
                session.add(SupplierReturnEntry(posting_id=posting.id, return_line_id=row.id,
                    position_id=positions[row.id].id, qty_base_delta=negate(row.quantity), created_by=DEMO_ACTOR_ID))
            session.flush()
            posting.posting_state = "POSTED"
            session.flush()
            return return_output(session, parent)
