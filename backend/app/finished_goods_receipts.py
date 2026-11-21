"""One receipt of genuine confirmed good output, posted into the shared ledger."""
import hashlib
import json
from uuid import UUID
from fastapi import Request, Response
from sqlalchemy import or_, select
from .db import DEMO_ACTOR_ID
from .models import Item, Unit, Warehouse
from .work_order_models import WorkOrder
from .completion_models import ProductionCompletion
from .receiving_models import InventoryLot, StockPosition, StockCreateReceipt, StockPosting
from .return_models import SupplierReturnPosting
from .issue_models import MaterialIssuePosting
from .material_return_models import MaterialReturnPosting
from .finished_goods_models import FinishedGoodsReceipt, FinishedGoodsPosting, FinishedGoodsEntry
from .finished_goods_schemas import ReceiptCreate, ReceiptUpdate, PostReceipt
from .finished_goods_authorization import policy, make_token, verified_confirmation
from .decimal_text import exact_difference, exact_sum
from .stock_ledger import compare, on_hand
from .local_demo import canonical

REQUEST_FIELDS = {"request_key", "row_version"}


def fields(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns} if row else None


def received_quantity(session, completion_id):
    return exact_sum(session.connection().exec_driver_sql("""
        SELECT r.quantity FROM finished_goods_receipt r
        JOIN finished_goods_posting p ON p.receipt_id=r.id AND p.posting_state='POSTED'
        JOIN finished_goods_entry e ON e.posting_id=p.id AND e.receipt_id=r.id
        JOIN stock_position s ON s.id=e.position_id
        JOIN production_completion c ON c.id=r.completion_id
        WHERE c.id=? AND c.state='CONFIRMED' AND c.row_version=r.completion_version
          AND s.item_id=c.product_id AND s.warehouse_id=r.warehouse_id AND s.lot_id IS r.lot_id
          AND e.leg_no=1 AND e.qty_base_delta=r.quantity AND erp_decimal_cmp(e.qty_base_delta,'0')>0
          AND e.created_by=p.created_by
          AND r.row_version=p.source_version+1
        """, (str(completion_id),)).scalars())


def completion_source(session, record_id, fail, version=None):
    row = session.get(ProductionCompletion, str(record_id))
    if row is None or row.state != "CONFIRMED":
        fail(422, "入庫來源必須是既有已確認完工回報；草稿與不良品不是可入庫良品。")
    if version is not None and row.row_version != version:
        fail(409, "完工來源版次已變更，請重新讀取並選取。")
    work = session.get(WorkOrder, row.work_order_id)
    item = session.get(Item, row.product_id)
    if (not work or not item or item.item_kind != "FINISHED" or work.product_id != item.id
        or item.base_unit_id != row.unit_id or not row.confirmation_id or not row.confirmed_at):
        fail(409, "完工來源的工單、成品、基本單位或確認紀錄不一致；沒有入庫。")
    return row


def source_output(session, source):
    work = session.get(WorkOrder, source.work_order_id)
    item, unit = session.get(Item, source.product_id), session.get(Unit, source.unit_id)
    received = received_quantity(session, source.id)
    return dict(completion_id=source.id, completion_version=source.row_version, document_no=source.document_no,
        work_order_id=work.id, work_order_no=work.document_no, product_id=item.id, product_code=item.code,
        product_name=item.name, unit_id=unit.id, unit_code=unit.code, unit_name=unit.name,
        report_date=source.report_date, good_qty_base=source.good_qty_base,
        defective_qty_base=source.defective_qty_base, received_qty_base=received,
        remaining_qty_base=exact_difference(source.good_qty_base, received), lot_tracking=item.lot_tracking)


def lot_for(session, row, source, fail, required=False):
    item = session.get(Item, source.product_id)
    if not item.lot_tracking:
        if row.lot_id is not None or row.lot_code is not None:
            fail(422, "此成品主檔未啟用追批，請保持批號空白；不會建立虛構批號。")
        return None
    if not row.lot_code:
        if required or row.lot_id:
            fail(422, "追批成品必須選擇既有製造批號或填寫實際新批號。")
        return None
    lot = session.get(InventoryLot, row.lot_id) if row.lot_id else session.scalar(select(InventoryLot).where(
        InventoryLot.item_id == source.product_id, InventoryLot.lot_code == row.lot_code))
    if row.lot_id and lot is None:
        fail(422, "選取的成品批號不存在，請重新讀取。")
    if lot and (lot.item_id != source.product_id or lot.origin_kind != "MANUFACTURED"
                or lot.lot_code != row.lot_code or lot.supplier_id is not None or lot.supplier_lot_code is not None):
        fail(422, "批號必須屬於此成品的實際製造批次；不能改掛原料批次或修改既有批號資料。")
    return lot


def position_for(session, source, warehouse_id, lot):
    item = session.get(Item, source.product_id)
    if item.lot_tracking and lot is None:
        return None
    statement = select(StockPosition).where(StockPosition.item_id == source.product_id,
        StockPosition.warehouse_id == warehouse_id, StockPosition.lot_id == lot.id if lot else StockPosition.lot_id.is_(None))
    return session.scalar(statement)


def output(session, row, fail):
    source = session.get(ProductionCompletion, row.completion_id)
    warehouse = session.get(Warehouse, row.warehouse_id)
    posting = session.scalar(select(FinishedGoodsPosting).where(FinishedGoodsPosting.receipt_id == row.id))
    lot = lot_for(session, row, source, fail)
    position = position_for(session, source, row.warehouse_id, lot)
    movements = []
    if posting and posting.posting_state == "POSTED":
        for entry in session.scalars(select(FinishedGoodsEntry).where(FinishedGoodsEntry.posting_id == posting.id)):
            p = session.get(StockPosition, entry.position_id)
            l = session.get(InventoryLot, p.lot_id) if p.lot_id else None
            movements.append(dict(**fields(entry), item_id=p.item_id, warehouse_id=p.warehouse_id,
                lot_id=p.lot_id, lot_code=l.lot_code if l else None))
    return dict(**fields(row), work_order_id=source.work_order_id, product_id=source.product_id, unit_id=source.unit_id,
        source=source_output(session, source), warehouse_code=warehouse.code, warehouse_name=warehouse.name,
        posting_state="DRAFT" if posting is None else posting.posting_state,
        posting_id=posting.id if posting else None, posted_at=posting.posted_at if posting else None,
        authorization_basis=posting.authorization_basis if posting else None,
        current_stock_qty_base=on_hand(session, position.id) if position else "0", movements=movements)


def validate_snapshot(session, row, fail):
    source = completion_source(session, row.completion_id, fail, row.completion_version)
    warehouse = session.get(Warehouse, row.warehouse_id)
    if not warehouse:
        fail(422, "收貨倉庫不存在，請重新選取。")
    if compare(row.quantity, "0") <= 0:
        fail(422, "實際成品入庫量必須大於零；有號或零數量只能保留草稿。")
    received = received_quantity(session, source.id)
    remaining = exact_difference(source.good_qty_base, received)
    if compare(row.quantity, remaining) > 0:
        fail(409, "本次入庫量超過此完工來源的剩餘可入庫良品量；不良品不可入庫。")
    lot = lot_for(session, row, source, fail, required=True)
    position = position_for(session, source, row.warehouse_id, lot)
    current = on_hand(session, position.id) if position else "0"
    # All authoritative source, target and pool facts are from the caller's one transaction.
    snapshot = json.loads(canonical(dict(receipt=fields(row), completion=fields(source),
        work_order=fields(session.get(WorkOrder, source.work_order_id)),
        product=fields(session.get(Item, source.product_id)), unit=fields(session.get(Unit, source.unit_id)),
        warehouse=fields(warehouse), lot=fields(lot), position=fields(position),
        received_qty_base=received, remaining_qty_base=remaining, current_stock_qty_base=current)))
    return snapshot, lot, position


def post_receipt(session, record_id, data, fail):
    row = session.get(FinishedGoodsReceipt, str(record_id))
    if row is None:
        fail(404, "成品入庫單不存在，請重新讀取。")
    digest = hashlib.sha256(canonical(dict(domain="finished-goods-receipt", id=row.id,
        row_version=data.row_version, confirmation_token_hash=hashlib.sha256(data.confirmation_token.encode()).hexdigest())).encode()).hexdigest()
    by_key = session.scalar(select(FinishedGoodsPosting).where(FinishedGoodsPosting.idempotency_key == str(data.request_key)))
    by_doc = session.scalar(select(FinishedGoodsPosting).where(FinishedGoodsPosting.receipt_id == row.id))
    for existing in [by_key, by_doc]:
        if existing:
            if (existing.receipt_id != row.id or existing.idempotency_key != str(data.request_key)
                or existing.request_fingerprint != digest or existing.posting_state != "POSTED"):
                fail(409, "成品入庫過帳識別或原始確認內容不同，不能重複入庫；請重新讀取。")
            return row, False
    if not policy(session)["posting_allowed"]:
        fail(403, "目前服務未啟用本機手動成品入庫過帳；請使用專用 localhost 展示啟動器。")
    if row.row_version != data.row_version:
        fail(409, "成品入庫單已更新，請重新讀取後檢視。")
    for model in [StockPosting, SupplierReturnPosting, MaterialIssuePosting, MaterialReturnPosting]:
        if session.scalar(select(model.id).where(model.idempotency_key == str(data.request_key))):
            fail(409, "此過帳識別已用於其他庫存作業。")
    snapshot, lot, position = validate_snapshot(session, row, fail)
    source = session.get(ProductionCompletion, row.completion_id)
    with verified_confirmation(session, row, data.confirmation_token, snapshot, fail) as grant:
        item = session.get(Item, source.product_id)
        if item.lot_tracking and lot is None:
            lot = InventoryLot(item_id=item.id, lot_code=row.lot_code, origin_kind="MANUFACTURED",
                supplier_id=None, supplier_lot_code=None, manufactured_date=None, expiry_date=None,
                traceability_note=None, created_by=DEMO_ACTOR_ID)
            session.add(lot)
            session.flush()
        row.lot_id = lot.id if lot else None
        row.row_version += 1
        session.flush()
        if position is None:
            position = StockPosition(item_id=item.id, warehouse_id=row.warehouse_id,
                lot_id=lot.id if lot else None, created_by=DEMO_ACTOR_ID)
            session.add(position)
            session.flush()
        posting = FinishedGoodsPosting(receipt_id=row.id, idempotency_key=str(data.request_key),
            request_fingerprint=digest, source_version=data.row_version, posting_state="PREPARING",
            authorization_basis=grant["basis"], confirmation_digest=grant["digest"],
            confirmation_snapshot=grant["snapshot"], confirmation_received_at=grant["received_at"],
            posted_at=grant["received_at"], created_by=DEMO_ACTOR_ID, posted_by=DEMO_ACTOR_ID)
        session.add(posting)
        session.flush()
        session.add(FinishedGoodsEntry(posting_id=posting.id, receipt_id=row.id, position_id=position.id,
            qty_base_delta=row.quantity, created_by=DEMO_ACTOR_ID))
        session.flush()
        posting.posting_state = "POSTED"
        session.flush()
    return row, True


def install_finished_goods_routes(app, fail, references):
    prefix = "/api/finished-goods-receipts"

    def find(session, record_id, version=None):
        row = session.get(FinishedGoodsReceipt, str(record_id))
        if not row:
            fail(404, "成品入庫單不存在，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "成品入庫單已被另一個視窗更新，請重新讀取。")
            if references(session, "finished_goods_receipt", row.id):
                fail(409, "成品入庫單已過帳或被引用，無法修改或刪除；請保留原資料。")
        return row

    def validate(session, data, record_id=None):
        source = completion_source(session, data.completion_id, fail, data.completion_version)
        if session.get(Warehouse, str(data.warehouse_id)) is None:
            fail(422, "收貨倉庫不存在，請重新選取。")
        query = select(FinishedGoodsReceipt.id).where(FinishedGoodsReceipt.document_no == data.document_no)
        if record_id:
            query = query.where(FinishedGoodsReceipt.id != record_id)
        if session.scalar(query):
            fail(409, "成品入庫單號已存在，請使用不同單號。")
        lot_for(session, data, source, fail)

    @app.get(prefix + "/policy")
    def read_policy(request: Request):
        with request.app.state.db.read() as session:
            return policy(session)

    @app.get(prefix + "/sources")
    def sources(request: Request, q: str = "", completion_id: UUID | None = None):
        query = select(ProductionCompletion).join(WorkOrder, ProductionCompletion.work_order_id == WorkOrder.id).join(
            Item, ProductionCompletion.product_id == Item.id).where(ProductionCompletion.state == "CONFIRMED")
        if completion_id:
            query = query.where(ProductionCompletion.id == str(completion_id))
        if q:
            query = query.where(or_(*(col.contains(q, autoescape=True) for col in [
                ProductionCompletion.document_no, WorkOrder.document_no, Item.code, Item.name])))
        with request.app.state.db.read() as session:
            return [source_output(session, c) for c in session.scalars(query.order_by(ProductionCompletion.document_no))]

    @app.get(prefix + "/lots")
    def lots(completion_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            source = completion_source(session, completion_id, fail)
            return [fields(row) for row in session.scalars(select(InventoryLot).where(
                InventoryLot.item_id == source.product_id, InventoryLot.origin_kind == "MANUFACTURED").order_by(InventoryLot.lot_code))]

    @app.get(prefix)
    def list_receipts(request: Request, q: str = ""):
        query = select(FinishedGoodsReceipt).join(ProductionCompletion, ProductionCompletion.id == FinishedGoodsReceipt.completion_id).join(
            WorkOrder, WorkOrder.id == ProductionCompletion.work_order_id).join(Item, Item.id == ProductionCompletion.product_id)
        if q:
            query = query.where(or_(*(col.contains(q, autoescape=True) for col in [
                FinishedGoodsReceipt.document_no, FinishedGoodsReceipt.lot_code, FinishedGoodsReceipt.note,
                ProductionCompletion.document_no, WorkOrder.document_no, Item.code, Item.name])))
        with request.app.state.db.read() as session:
            return [output(session, row, fail) for row in session.scalars(query.order_by(FinishedGoodsReceipt.document_no))]

    @app.get(prefix + "/{record_id}")
    def read_receipt(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return output(session, find(session, record_id), fail)

    @app.post(prefix, status_code=201)
    def create_receipt(data: ReceiptCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest = hashlib.sha256(canonical(dict(domain="finished-goods-receipt-create",
                data=data.model_dump(mode="json", exclude={"request_key"}))).encode()).hexdigest()
            receipt = session.get(StockCreateReceipt, str(data.request_key))
            if receipt:
                if receipt.resource_kind != "header" or receipt.payload_hash != digest:
                    fail(409, "新增請求識別已用於不同內容，請重新讀取。")
                row = session.get(FinishedGoodsReceipt, receipt.record_id)
                if row is None:
                    fail(410, "原成品入庫草稿已刪除，請開始新的新增操作。")
                response.status_code = 200
                return output(session, row, fail)
            validate(session, data)
            row = FinishedGoodsReceipt(**data.model_dump(mode="json", exclude=REQUEST_FIELDS), created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            session.add(StockCreateReceipt(request_key=str(data.request_key), resource_kind="header", record_id=row.id,
                payload_hash=digest, created_by=DEMO_ACTOR_ID))
            session.flush()
            return output(session, row, fail)

    @app.put(prefix + "/{record_id}")
    def update_receipt(record_id: UUID, data: ReceiptUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, data.row_version)
            validate(session, data, row.id)
            for key, value in data.model_dump(mode="json", exclude=REQUEST_FIELDS).items():
                setattr(row, key, value)
            row.row_version += 1
            session.flush()
            return output(session, row, fail)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_receipt(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            session.delete(find(session, record_id, row_version))
            session.flush()

    @app.get(prefix + "/{record_id}/posting-review")
    def review(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            row = find(session, record_id)
            if session.scalar(select(FinishedGoodsPosting.id).where(FinishedGoodsPosting.receipt_id == row.id)):
                fail(409, "成品入庫單已過帳；請檢視原始庫存異動，不能再次入庫。")
            snapshot, _, _ = validate_snapshot(session, row, fail)
            return dict(receipt=output(session, row, fail), source=source_output(session, session.get(ProductionCompletion, row.completion_id)),
                current_stock_qty_base=snapshot["current_stock_qty_base"],
                expected_stock_qty_base=exact_sum([snapshot["current_stock_qty_base"], row.quantity]),
                remaining_after_qty_base=exact_difference(snapshot["remaining_qty_base"], row.quantity),
                confirmation_token=make_token(session, snapshot), policy=policy(session))

    @app.post(prefix + "/{record_id}/post", status_code=201)
    def post(record_id: UUID, data: PostReceipt, request: Request, response: Response):
        with request.app.state.db.write() as session:
            row, created = post_receipt(session, record_id, data, fail)
            if not created:
                response.status_code = 200
            return output(session, row, fail)
