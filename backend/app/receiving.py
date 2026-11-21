"""Draft purchase receiving and one atomic, idempotent, immutable inbound posting."""
import hashlib
import json
from uuid import UUID

from fastapi import Request, Response
from sqlalchemy import func, or_, select

from .db import DEMO_ACTOR_ID
from .decimal_text import exact_difference, exact_product, exact_sum
from .models import Item, Warehouse
from .purchase_models import PurchaseLine, PurchaseOrder
from .purchasing import header_output, line_output
from .receiving_models import (InventoryLot, StockCreateReceipt, StockDocument, StockDocumentLine,
                               StockEntry, StockPosition, StockPosting)
from .receiving_schemas import (MovementOut, PositionOut, PostReceipt, ReceiptCreate, ReceiptLineCreate,
                                ReceiptLineOut, ReceiptLineUpdate, ReceiptOut, ReceiptUpdate)
from .stock_ledger import on_hand


def fields(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def posted_received(session, purchase_line_id):
    rows = session.scalars(select(StockDocumentLine).join(StockPosting,
        StockPosting.stock_document_id == StockDocumentLine.stock_document_id).where(
        StockDocumentLine.purchase_order_line_id == str(purchase_line_id), StockPosting.posting_state == "POSTED"))
    return exact_sum(exact_product(row.quantity, row.base_factor_snapshot) for row in rows)


def position_output(session, row):
    item, warehouse = session.get(Item, row.item_id), session.get(Warehouse, row.warehouse_id)
    lot = session.get(InventoryLot, row.lot_id) if row.lot_id else None
    return PositionOut(**fields(row), item_code=item.code, item_name=item.name,
        warehouse_code=warehouse.code, warehouse_name=warehouse.name,
        lot_code=lot.lot_code if lot else None, on_hand_qty_base=on_hand(session, row.id))


def line_inventory(session, row):
    lot_id = row.lot_id
    if lot_id is None and row.lot_code:
        lot_id = session.scalar(select(InventoryLot.id).where(InventoryLot.item_id == row.item_id,
                                                             InventoryLot.lot_code == row.lot_code))
    query = select(StockPosition.id).where(StockPosition.item_id == row.item_id, StockPosition.warehouse_id == row.warehouse_id)
    query = query.where(StockPosition.lot_id == lot_id) if lot_id else query.where(StockPosition.lot_id.is_(None))
    position_id = session.scalar(query)
    return on_hand(session, position_id) if position_id else "0"


def receipt_line_output(session, row):
    source, item = session.get(PurchaseLine, row.purchase_order_line_id), session.get(Item, row.item_id)
    ordered = exact_product(source.ordered_qty, source.base_factor_snapshot)
    received = posted_received(session, source.id)
    return ReceiptLineOut(**fields(row), item_code=item.code, lot_tracking=item.lot_tracking,
        ordered_qty_base=ordered, received_qty_base=received, remaining_qty_base=exact_difference(ordered, received),
        quantity_base=exact_product(row.quantity, row.base_factor_snapshot), on_hand_qty_base=line_inventory(session, row))


def receipt_output(session, row):
    source = session.get(PurchaseOrder, row.purchase_order_id)
    posting = session.scalar(select(StockPosting).where(StockPosting.stock_document_id == row.id))
    state = "DRAFT" if posting is None else ("POSTED" if posting.posting_state == "POSTED" else "INCOMPLETE")
    lines = [receipt_line_output(session, line) for line in session.scalars(select(StockDocumentLine).where(
        StockDocumentLine.stock_document_id == row.id).order_by(StockDocumentLine.line_no))]
    movements = []
    if state == "POSTED":
        for entry in session.scalars(select(StockEntry).where(StockEntry.posting_id == posting.id).order_by(StockEntry.document_line_id)):
            position = session.get(StockPosition, entry.position_id)
            lot = session.get(InventoryLot, position.lot_id) if position.lot_id else None
            movements.append(MovementOut(**fields(entry), item_id=position.item_id,
                warehouse_id=position.warehouse_id, lot_id=position.lot_id, lot_code=lot.lot_code if lot else None))
    return ReceiptOut(**fields(row), order_document_no=source.document_no,
        order_total_amount=header_output(session, source).total_amount, posting_state=state,
        posting_id=posting.id if posting else None, posted_at=posting.posted_at if state == "POSTED" else None,
        line_count=len(lines), lines=lines, movements=movements)


def install_receiving_routes(app, fail, references):
    prefix = "/api/purchase-receipts"

    def find(session, record_id):
        row = session.get(StockDocument, str(record_id))
        if row is None:
            fail(404, "採購入庫單不存在，請重新讀取。")
        return row

    def draft(session, record_id, version):
        row = find(session, record_id)
        if session.scalar(select(StockPosting.id).where(StockPosting.stock_document_id == row.id)):
            fail(409, "入庫單已過帳或有過帳紀錄，僅可檢視，不能修改或刪除。")
        if row.row_version != version:
            fail(409, "入庫單已被另一個視窗更新，請重新讀取後再操作。")
        return row

    def source_order(session, order_id):
        row = session.get(PurchaseOrder, str(order_id))
        if row is None:
            fail(422, "來源採購單不存在，請重新選擇。")
        if row.approval_state is not None or row.approved_at is not None or row.approved_by is not None:
            fail(409, "來源採購單的正式核准狀態尚未支援此作業，請保留原資料。")
        return row

    def source_line(session, parent, line_id):
        row = session.get(PurchaseLine, str(line_id))
        if row is None or row.purchase_order_id != parent.purchase_order_id:
            fail(422, "採購明細不存在或不屬於這張採購單，請重新選擇。")
        item = session.get(Item, row.item_id)
        if item is None or item.item_kind != "RAW" or row.unit_id != item.base_unit_id or row.base_factor_snapshot != "1":
            fail(422, "來源明細必須為原物料基本單位，其他換算尚待核定。")
        return row, item

    def find_line(session, parent_id, record_id):
        row = session.get(StockDocumentLine, str(record_id))
        if row is None or row.stock_document_id != str(parent_id):
            fail(404, "這張入庫單沒有此筆明細，請重新讀取。")
        return row

    def validate_lot(session, parent, item, data, required=False):
        if not item.lot_tracking:
            if data.lot_code or data.supplier_lot_code or data.manufactured_date or data.expiry_date:
                fail(422, "此原料不追批，請勿填入虛構批號或批次日期。")
            return None
        if not data.lot_code:
            if required:
                fail(422, "追批原料必須填寫實際庫存批號後才能過帳。")
            return None
        lot = session.scalar(select(InventoryLot).where(InventoryLot.item_id == item.id, InventoryLot.lot_code == data.lot_code))
        if lot:
            if lot.supplier_id != parent.supplier_id or any(getattr(lot, key) != getattr(data, key)
                for key in ["supplier_lot_code", "manufactured_date", "expiry_date"]):
                fail(422, "此批號已有不同供應商或批次資料，請確認實際批次，不能改寫原批號。")
        return lot

    def digest(kind, data, parent=None):
        body = {"kind": kind, "parent": str(parent) if parent else None,
                "data": data.model_dump(mode="json", exclude={"request_key", "document_version"})}
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def replay(session, kind, data, parent=None):
        fingerprint = digest(kind, data, parent)
        receipt = session.get(StockCreateReceipt, str(data.request_key))
        if receipt:
            if receipt.resource_kind != kind or receipt.payload_hash != fingerprint:
                fail(409, "新增請求識別已用於不同內容，請重新讀取。")
            row = session.get(StockDocument if kind == "header" else StockDocumentLine, receipt.record_id)
            if row is None:
                fail(410, "原新增資料已刪除，不能重複建立；請使用新的新增操作。")
            return fingerprint, row
        return fingerprint, None

    def remember(session, kind, data, row, fingerprint):
        session.add(StockCreateReceipt(request_key=str(data.request_key), resource_kind=kind, record_id=row.id,
            payload_hash=fingerprint, created_by=DEMO_ACTOR_ID))
        session.flush()

    @app.get(prefix + "/source-orders/{order_id}")
    def order_context(order_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            parent = source_order(session, order_id)
            result = header_output(session, parent).model_dump(mode="json")
            for row in result["lines"]:
                item = session.get(Item, row["item_id"])
                received = posted_received(session, row["id"])
                row.update(item_code=item.code, lot_tracking=item.lot_tracking, received_qty_base=received,
                           remaining_qty_base=exact_difference(row["ordered_qty_base"], received))
            return result

    @app.get("/api/stock-positions", response_model=list[PositionOut])
    def positions(request: Request, item_id: UUID | None = None, warehouse_id: UUID | None = None):
        query = select(StockPosition)
        if item_id:
            query = query.where(StockPosition.item_id == str(item_id))
        if warehouse_id:
            query = query.where(StockPosition.warehouse_id == str(warehouse_id))
        with request.app.state.db.read() as session:
            return [position_output(session, row) for row in session.scalars(query.order_by(StockPosition.item_id, StockPosition.warehouse_id, StockPosition.id))]

    @app.get(prefix, response_model=list[ReceiptOut])
    def list_receipts(request: Request, q: str = ""):
        query = select(StockDocument).join(PurchaseOrder, PurchaseOrder.id == StockDocument.purchase_order_id)
        if q:
            query = query.where(or_(*(column.contains(q, autoescape=True) for column in [StockDocument.document_no,
                StockDocument.party_name_snapshot, StockDocument.external_reference, PurchaseOrder.document_no])))
        with request.app.state.db.read() as session:
            return [receipt_output(session, row) for row in session.scalars(query.order_by(StockDocument.document_no))]

    @app.get(prefix + "/{record_id}", response_model=ReceiptOut)
    def read_receipt(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return receipt_output(session, find(session, record_id))

    @app.post(prefix, response_model=ReceiptOut, status_code=201)
    def create_receipt(data: ReceiptCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            fingerprint, existing = replay(session, "header", data)
            if existing:
                response.status_code = 200
                return receipt_output(session, existing)
            parent = source_order(session, data.purchase_order_id)
            if session.scalar(select(StockDocument.id).where(StockDocument.document_no == data.document_no)):
                fail(409, "入庫單號已存在，請使用不同單號。")
            row = StockDocument(**data.model_dump(mode="json", exclude={"request_key"}), purchase_order_version=parent.row_version,
                supplier_id=parent.supplier_id, party_name_snapshot=parent.supplier_name_snapshot, created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            remember(session, "header", data, row, fingerprint)
            return receipt_output(session, row)

    @app.put(prefix + "/{record_id}", response_model=ReceiptOut)
    def update_receipt(record_id: UUID, data: ReceiptUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = draft(session, record_id, data.row_version)
            parent = source_order(session, data.purchase_order_id)
            if row.purchase_order_id != parent.id and session.scalar(select(StockDocumentLine.id).where(StockDocumentLine.stock_document_id == row.id)):
                fail(409, "入庫單已有明細，不能改掛來源採購單。")
            if session.scalar(select(StockDocument.id).where(StockDocument.document_no == data.document_no, StockDocument.id != row.id)):
                fail(409, "入庫單號已存在，請使用不同單號。")
            for key, value in data.model_dump(mode="json", exclude={"row_version"}).items():
                setattr(row, key, value)
            row.purchase_order_version = parent.row_version
            row.supplier_id, row.party_name_snapshot = parent.supplier_id, parent.supplier_name_snapshot
            row.row_version += 1
            session.flush()
            return receipt_output(session, row)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_receipt(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = draft(session, record_id, row_version)
            if references(session, "stock_document", row.id):
                fail(409, "草稿仍有明細或其他引用，請先逐筆處理，不能整單連帶刪除。")
            session.delete(row)
            session.flush()

    @app.post(prefix + "/{document_id}/lines", response_model=ReceiptLineOut, status_code=201)
    def create_line(document_id: UUID, data: ReceiptLineCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            fingerprint, existing = replay(session, "line", data, document_id)
            if existing:
                response.status_code = 200
                return receipt_line_output(session, existing)
            parent = draft(session, document_id, data.document_version)
            po = source_order(session, parent.purchase_order_id)
            source, item = source_line(session, parent, data.purchase_order_line_id)
            if session.get(Warehouse, str(data.warehouse_id)) is None:
                fail(422, "收貨倉庫不存在，請重新選擇。")
            validate_lot(session, parent, item, data)
            number = (session.scalar(select(func.max(StockDocumentLine.line_no)).where(StockDocumentLine.stock_document_id == parent.id)) or 0)+1
            row = StockDocumentLine(**data.model_dump(mode="json", exclude={"request_key", "document_version"}),
                stock_document_id=parent.id, line_no=number, purchase_order_line_version=source.row_version,
                item_id=source.item_id, unit_id=source.unit_id, base_factor_snapshot=source.base_factor_snapshot,
                description_snapshot=source.description_snapshot, created_by=DEMO_ACTOR_ID)
            session.add(row)
            parent.purchase_order_version, parent.row_version = po.row_version, parent.row_version+1
            session.flush()
            remember(session, "line", data, row, fingerprint)
            return receipt_line_output(session, row)

    @app.get(prefix + "/{document_id}/lines", response_model=list[ReceiptLineOut])
    def read_lines(document_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            find(session, document_id)
            return [receipt_line_output(session, row) for row in session.scalars(select(StockDocumentLine).where(
                StockDocumentLine.stock_document_id == str(document_id)).order_by(StockDocumentLine.line_no))]

    @app.get(prefix + "/{document_id}/lines/{record_id}", response_model=ReceiptLineOut)
    def read_line(document_id: UUID, record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            find(session, document_id)
            return receipt_line_output(session, find_line(session, document_id, record_id))

    @app.put(prefix + "/{document_id}/lines/{record_id}", response_model=ReceiptLineOut)
    def update_line(document_id: UUID, record_id: UUID, data: ReceiptLineUpdate, request: Request):
        with request.app.state.db.write() as session:
            parent = draft(session, document_id, data.document_version)
            row = find_line(session, parent.id, record_id)
            if row.row_version != data.row_version:
                fail(409, "入庫明細已更新，請重新讀取。")
            po = source_order(session, parent.purchase_order_id)
            source, item = source_line(session, parent, data.purchase_order_line_id)
            if session.get(Warehouse, str(data.warehouse_id)) is None:
                fail(422, "收貨倉庫不存在，請重新選擇。")
            validate_lot(session, parent, item, data)
            for key, value in data.model_dump(mode="json", exclude={"document_version", "row_version"}).items():
                setattr(row, key, value)
            row.purchase_order_line_version, row.item_id, row.unit_id = source.row_version, source.item_id, source.unit_id
            row.base_factor_snapshot, row.description_snapshot = source.base_factor_snapshot, source.description_snapshot
            row.row_version += 1
            parent.purchase_order_version, parent.row_version = po.row_version, parent.row_version+1
            session.flush()
            return receipt_line_output(session, row)

    @app.delete(prefix + "/{document_id}/lines/{record_id}", status_code=204)
    def delete_line(document_id: UUID, record_id: UUID, row_version: int, document_version: int, request: Request):
        with request.app.state.db.write() as session:
            parent = draft(session, document_id, document_version)
            row = find_line(session, parent.id, record_id)
            if row.row_version != row_version:
                fail(409, "入庫明細已更新，請重新讀取。")
            if references(session, "stock_document_line", row.id):
                fail(409, "入庫明細已被引用，不能刪除。")
            session.delete(row)
            parent.row_version += 1
            session.flush()

    def posting_fingerprint(parent, lines, version):
        header = {key: value for key, value in fields(parent).items() if key not in {"created_at", "created_by", "updated_at", "row_version", "approved_at"}}
        detail = [{key: value for key, value in fields(line).items() if key not in {"created_at", "created_by", "updated_at", "row_version", "lot_id"}} for line in lines]
        return hashlib.sha256(json.dumps({"header": header, "lines": detail, "version": version},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @app.post(prefix + "/{document_id}/post", response_model=ReceiptOut, status_code=201)
    def post_receipt(document_id: UUID, data: PostReceipt, request: Request, response: Response):
        with request.app.state.db.write() as session:
            parent = find(session, document_id)
            lines = list(session.scalars(select(StockDocumentLine).where(StockDocumentLine.stock_document_id == parent.id).order_by(StockDocumentLine.line_no)))
            fingerprint = posting_fingerprint(parent, lines, data.row_version)
            if session.connection().exec_driver_sql("SELECT 1 FROM supplier_return_posting WHERE idempotency_key=?", (str(data.request_key),)).first():
                fail(409, "過帳請求識別已用於供應商退貨，不能用於採購入庫。")
            by_key = session.scalar(select(StockPosting).where(StockPosting.idempotency_key == str(data.request_key)))
            if by_key:
                if by_key.stock_document_id != parent.id or by_key.request_fingerprint != fingerprint:
                    fail(409, "過帳請求識別已用於不同內容，請勿重複送出。")
                if by_key.posting_state != "POSTED":
                    fail(409, "過帳紀錄不完整，請保留資料並進行獨立檢查。")
                response.status_code = 200
                return receipt_output(session, parent)
            prior = session.scalar(select(StockPosting).where(StockPosting.stock_document_id == parent.id))
            if prior:
                if prior.posting_state != "POSTED":
                    fail(409, "過帳紀錄不完整，請保留資料並進行獨立檢查。")
                response.status_code = 200
                return receipt_output(session, parent)
            parent = draft(session, parent.id, data.row_version)
            po = source_order(session, parent.purchase_order_id)
            if parent.purchase_order_version != po.row_version:
                fail(409, "來源採購單已變更，請重新檢視並儲存草稿後再過帳。")
            if not lines:
                fail(422, "入庫單沒有明細，不能過帳。")
            validated = []
            for row in lines:
                source, item = source_line(session, parent, row.purchase_order_line_id)
                if row.purchase_order_line_version != source.row_version:
                    fail(409, "來源採購明細已變更，請重新檢視並逐筆儲存草稿。")
                if row.quantity.startswith("-"):
                    fail(422, "本作業為入庫，負接受量不會過帳；退貨與沖回規則尚待核定。")
                if session.get(Warehouse, row.warehouse_id) is None:
                    fail(422, "收貨倉庫不存在，不能過帳。")
                lot = validate_lot(session, parent, item, row, required=True)
                validated.append((row, item, lot))
            for row, item, lot in validated:
                if item.lot_tracking and lot is None:
                    lot = validate_lot(session, parent, item, row, required=True)
                    if lot is None:
                        lot = InventoryLot(item_id=item.id, lot_code=row.lot_code, supplier_id=parent.supplier_id,
                            supplier_lot_code=row.supplier_lot_code, manufactured_date=row.manufactured_date,
                            expiry_date=row.expiry_date, created_by=DEMO_ACTOR_ID)
                        session.add(lot)
                        session.flush()
                row.lot_id = lot.id if lot else None
                row.row_version += 1
            parent.row_version += 1
            session.flush()
            posting = StockPosting(stock_document_id=parent.id, idempotency_key=str(data.request_key),
                request_fingerprint=fingerprint, source_version=data.row_version, posting_state="PREPARING",
                posted_by=DEMO_ACTOR_ID, created_by=DEMO_ACTOR_ID)
            session.add(posting)
            session.flush()
            for row, _, _ in validated:
                query = select(StockPosition).where(StockPosition.item_id == row.item_id, StockPosition.warehouse_id == row.warehouse_id)
                query = query.where(StockPosition.lot_id == row.lot_id) if row.lot_id else query.where(StockPosition.lot_id.is_(None))
                position = session.scalar(query)
                if position is None:
                    position = StockPosition(item_id=row.item_id, warehouse_id=row.warehouse_id, lot_id=row.lot_id, created_by=DEMO_ACTOR_ID)
                    session.add(position)
                    session.flush()
                session.add(StockEntry(posting_id=posting.id, document_line_id=row.id, position_id=position.id,
                    qty_base_delta=exact_product(row.quantity, row.base_factor_snapshot), created_by=DEMO_ACTOR_ID))
            session.flush()
            posting.posting_state = "POSTED"
            session.flush()
            return receipt_output(session, parent)
