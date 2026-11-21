"""Reasoned ordinary adjustments to genuine existing positions; no stocktake mutation."""
import hashlib
import json
from uuid import UUID
from fastapi import Request, Response
from sqlalchemy import or_, select
from .db import DEMO_ACTOR_ID
from .models import Item, Unit, Warehouse, now_utc
from .receiving_models import InventoryLot, StockPosition, StockCreateReceipt, StockPosting
from .return_models import SupplierReturnPosting
from .issue_models import MaterialIssuePosting
from .material_return_models import MaterialReturnPosting
from .finished_goods_models import FinishedGoodsPosting
from .sales_shipment_models import SalesShipmentPosting
from .customer_return_models import CustomerReturnFinalization
from .stocktake_models import Stocktake, StocktakePosting
from .adjustment_models import InventoryAdjustment, InventoryAdjustmentPosting, InventoryAdjustmentEntry
from .adjustment_schemas import AdjustmentCreate, AdjustmentUpdate, AdjustmentAction, AdjustmentPost
from .adjustment_authorization import policy, make_token, verified_confirmation, edit_grant
from .sales_shipments import fields
from .decimal_text import exact_sum
from .stock_ledger import compare, negate, on_hand
from .local_demo import canonical
from .warehouse_freeze import assert_warehouses_unfrozen

POSTINGS = [StockPosting, SupplierReturnPosting, MaterialIssuePosting, MaterialReturnPosting,
    FinishedGoodsPosting, SalesShipmentPosting, CustomerReturnFinalization, StocktakePosting, InventoryAdjustmentPosting]
SOURCE_FIELDS = ["item_id", "item_kind", "unit_id", "warehouse_id", "lot_id"]


def fingerprint(domain, data):
    return hashlib.sha256(canonical(dict(domain=domain, data=data)).encode()).hexdigest()


def resolve_position(session, position_id, fail):
    position = session.get(StockPosition, str(position_id))
    if not position: fail(422, "請選擇真實已登記的庫存位置，不能以文字建立新批號或庫存。")
    item = session.get(Item, position.item_id)
    unit = session.get(Unit, item.base_unit_id) if item else None
    warehouse = session.get(Warehouse, position.warehouse_id)
    lot = session.get(InventoryLot, position.lot_id) if position.lot_id else None
    if (not item or item.item_kind not in {"RAW", "FINISHED"} or not unit or not warehouse
        or (item.lot_tracking and (not lot or lot.item_id != item.id))
        or (not item.lot_tracking and position.lot_id is not None)):
        fail(409, "庫存位置的品項、基本單位、倉庫或原批號身分不一致。")
    return position, item, unit, warehouse, lot


def source_output(session, position_id, fail):
    position, item, unit, warehouse, lot = resolve_position(session, position_id, fail)
    frozen = session.scalar(select(Stocktake).where(Stocktake.warehouse_id == warehouse.id, Stocktake.freeze_active.is_(True)))
    return dict(id=position.id, position_id=position.id, item_id=item.id, item_kind=item.item_kind,
        item_code=item.code, item_name=item.name, unit_id=unit.id, unit_code=unit.code, unit_name=unit.name,
        warehouse_id=warehouse.id, warehouse_code=warehouse.code, warehouse_name=warehouse.name,
        lot_id=position.lot_id, lot_code=lot.lot_code if lot else None,
        current_stock_qty_base=on_hand(session, position.id), frozen=frozen is not None,
        active_stocktake_id=frozen.id if frozen else None, active_stocktake_no=frozen.document_no if frozen else None)


def derived_values(session, data, fail):
    position, item, unit, warehouse, lot = resolve_position(session, data.position_id, fail)
    return dict(item_id=item.id, item_kind=item.item_kind, unit_id=unit.id,
        warehouse_id=warehouse.id, lot_id=position.lot_id)


def signed_quantity(row):
    # Invalid draft magnitude is never silently changed with abs() or a sign flip.
    if compare(row.quantity, "0") <= 0: return None
    return row.quantity if row.direction == "INCREASE" else negate(row.quantity)


def blockers_for(row, source):
    blockers = []
    if compare(row.quantity, "0") <= 0: blockers.append("調整數量必須大於零；負數或零不能作為調整幅度過帳，也不會自動反轉方向。")
    if not row.reason.strip(): blockers.append("過帳前必須填寫有意義的調整原因；本機展示請明確註明非實際損耗。")
    if source["frozen"]: blockers.append("倉庫已凍結（盤點單 " + source["active_stocktake_no"] + "）；請由盤點程序合法結案或取消後重新檢視。")
    if row.direction == "DECREASE" and compare(row.quantity, "0") > 0 and compare(row.quantity, source["current_stock_qty_base"]) > 0:
        blockers.append("減少數量超過此倉此批的實際庫存；不能產生負庫存。")
    return blockers


def output(session, row, fail):
    source = source_output(session, row.position_id, fail)
    posting = session.scalar(select(InventoryAdjustmentPosting).where(InventoryAdjustmentPosting.adjustment_id == row.id))
    movements = list(session.scalars(select(InventoryAdjustmentEntry).where(InventoryAdjustmentEntry.adjustment_id == row.id)))
    delta = signed_quantity(row)
    before = source["current_stock_qty_base"]
    if posting:
        before = json.loads(posting.confirmation_snapshot)["snapshot"]["current_stock_qty_base"]
    expected = exact_sum([before, delta]) if delta is not None and row.state != "CANCELLED" else None
    return dict(**fields(row), source=source, signed_qty_base_delta=delta, stock_before_qty_base=before,
        expected_stock_qty_base=expected, posted_stock_qty_base=expected if row.state == "POSTED" else None,
        blockers=blockers_for(row, source) if row.state == "DRAFT" else [],
        posting_id=posting.id if posting else None, posting_state=posting.posting_state if posting else row.state,
        authorization_basis=posting.authorization_basis if posting else None,
        movements=[fields(entry) for entry in sorted(movements, key=lambda entry: entry.id)])


def find(session, record_id, fail, version=None, draft=False):
    row = session.get(InventoryAdjustment, str(record_id))
    if row is None: fail(404, "庫存調整單不存在，請重新讀取。")
    if version is not None and row.row_version != version: fail(409, "庫存調整單已更新，請重新讀取。")
    if draft and row.state != "DRAFT": fail(409, "此調整單已過帳或取消，不能修改、刪除或再次過帳。")
    return row


def unused_key(session, key, fail):
    key = str(key)
    if session.get(StockCreateReceipt, key) or session.scalar(select(Stocktake.id).where(Stocktake.start_key == key)):
        fail(409, "請求識別已使用於其他操作，請保留原識別重試原操作。")
    if any(session.scalar(select(model.id).where(model.idempotency_key == key)) for model in POSTINGS):
        fail(409, "請求識別已用於其他庫存過帳，不能移用此調整。")


def receipt_replay(session, key, digest, fail):
    receipt = session.get(StockCreateReceipt, str(key))
    if receipt:
        if receipt.resource_kind != "header" or receipt.payload_hash != digest:
            fail(409, "請求識別對應不同內容，不能重複執行。")
        return receipt
    unused_key(session, key, fail)
    return None


def save_receipt(session, key, digest, record_id):
    session.add(StockCreateReceipt(request_key=str(key), resource_kind="header", record_id=record_id,
        payload_hash=digest, created_by=DEMO_ACTOR_ID))
    session.flush()


def validate_input(session, data, fail, record_id=None):
    values = derived_values(session, data, fail)
    query = select(InventoryAdjustment.id).where(InventoryAdjustment.document_no == data.document_no)
    if record_id: query = query.where(InventoryAdjustment.id != record_id)
    if session.scalar(query): fail(409, "庫存調整單號已存在，請使用不同單號。")
    return values


def source_snapshot(session, row, fail):
    position, item, unit, warehouse, lot = resolve_position(session, row.position_id, fail)
    current = dict(item_id=item.id, item_kind=item.item_kind, unit_id=unit.id, warehouse_id=warehouse.id, lot_id=position.lot_id)
    if any(getattr(row, field) != current[field] for field in SOURCE_FIELDS):
        fail(409, "調整單與真實庫存位置的品項、單位、倉庫或批號身分不一致，請重新選取來源。")
    # Durable lifecycle identities detect a freeze that began and ended after a
    # preview, even if a zero close/cancel restored the same stock and boolean.
    history = [dict(id=s.id, row_version=s.row_version, state=s.state, freeze_active=s.freeze_active, snapshot_at=s.snapshot_at)
        for s in session.scalars(select(Stocktake).where(Stocktake.warehouse_id == warehouse.id,
            Stocktake.snapshot_at.is_not(None)).order_by(Stocktake.id))]
    return json.loads(canonical(dict(adjustment=fields(row), position=fields(position), item=fields(item),
        unit=fields(unit), warehouse=fields(warehouse), lot=fields(lot) if lot else None,
        current_stock_qty_base=on_hand(session, position.id), freeze_history=history)))


def post_adjustment(session, record_id, data, fail):
    row = find(session, record_id, fail)
    digest = fingerprint("inventory-adjustment-post", dict(id=row.id, row_version=data.row_version,
        confirmation_token_hash=hashlib.sha256(data.confirmation_token.encode()).hexdigest()))
    by_key = session.scalar(select(InventoryAdjustmentPosting).where(InventoryAdjustmentPosting.idempotency_key == str(data.request_key)))
    by_doc = session.scalar(select(InventoryAdjustmentPosting).where(InventoryAdjustmentPosting.adjustment_id == row.id))
    for previous in [by_key, by_doc]:
        if previous:
            if (previous.adjustment_id != row.id or previous.idempotency_key != str(data.request_key)
                or previous.request_fingerprint != digest or previous.posting_state != "POSTED" or row.state != "POSTED"):
                fail(409, "調整過帳識別或原始內容不同，不能再次產生庫存異動；請重新讀取。")
            return row, False
    find(session, row.id, fail, data.row_version, draft=True)
    unused_key(session, data.request_key, fail)
    snapshot = source_snapshot(session, row, fail)
    source = source_output(session, row.position_id, fail)
    assert_warehouses_unfrozen(session, [row.warehouse_id], fail)
    blockers = blockers_for(row, source)
    if blockers: fail(422 if compare(row.quantity, "0") <= 0 or not row.reason.strip() else 409, " ".join(blockers))
    with verified_confirmation(session, row, data.confirmation_token, snapshot, fail) as grant:
        posting = InventoryAdjustmentPosting(adjustment_id=row.id, idempotency_key=str(data.request_key),
            request_fingerprint=digest, source_version=row.row_version, posting_state="PREPARING",
            authorization_basis=grant["basis"], confirmation_digest=grant["digest"], confirmation_snapshot=grant["snapshot"],
            confirmation_received_at=grant["received_at"], posted_at=grant["received_at"], posted_by=DEMO_ACTOR_ID,
            created_by=DEMO_ACTOR_ID)
        session.add(posting)
        session.flush()
        session.add(InventoryAdjustmentEntry(posting_id=posting.id, adjustment_id=row.id, position_id=row.position_id,
            qty_base_delta=signed_quantity(row), created_by=DEMO_ACTOR_ID))
        session.flush()
        posting.posting_state = "POSTED"
        session.flush()
        row.state, row.posted_at, row.posted_by = "POSTED", grant["received_at"], DEMO_ACTOR_ID
        row.row_version += 1
        session.flush()
    return row, True


def install_adjustment_routes(app, fail, references):
    prefix = "/api/inventory-adjustments"

    @app.get(prefix + "/policy")
    def read_policy(request: Request):
        with request.app.state.db.read() as session: return policy(session)

    @app.get(prefix + "/sources")
    def sources(request: Request, q: str = "", item_id: UUID | None = None, warehouse_id: UUID | None = None):
        query = select(StockPosition).join(Item, Item.id == StockPosition.item_id).join(Warehouse, Warehouse.id == StockPosition.warehouse_id).outerjoin(InventoryLot, InventoryLot.id == StockPosition.lot_id)
        if item_id: query = query.where(StockPosition.item_id == str(item_id))
        if warehouse_id: query = query.where(StockPosition.warehouse_id == str(warehouse_id))
        if q: query = query.where(or_(*(column.contains(q, autoescape=True) for column in [Item.code, Item.name, Warehouse.code, Warehouse.name, InventoryLot.lot_code])))
        with request.app.state.db.read() as session:
            if item_id and session.get(Item, str(item_id)) is None: fail(422, "品項不存在。")
            if warehouse_id and session.get(Warehouse, str(warehouse_id)) is None: fail(422, "倉庫不存在。")
            return [source_output(session, position.id, fail) for position in session.scalars(query.order_by(Item.code, Warehouse.code, InventoryLot.lot_code, StockPosition.id))]

    @app.get(prefix)
    def list_adjustments(request: Request, q: str = ""):
        query = select(InventoryAdjustment).join(Item, Item.id == InventoryAdjustment.item_id).join(Warehouse, Warehouse.id == InventoryAdjustment.warehouse_id).outerjoin(InventoryLot, InventoryLot.id == InventoryAdjustment.lot_id)
        if q: query = query.where(or_(*(column.contains(q, autoescape=True) for column in [InventoryAdjustment.document_no,
            InventoryAdjustment.reason, InventoryAdjustment.note, Item.code, Item.name, Warehouse.code, Warehouse.name, InventoryLot.lot_code])))
        with request.app.state.db.read() as session:
            return [output(session, row, fail) for row in session.scalars(query.order_by(InventoryAdjustment.document_no))]

    @app.get(prefix + "/{record_id}")
    def read_adjustment(record_id: UUID, request: Request):
        with request.app.state.db.read() as session: return output(session, find(session, record_id, fail), fail)

    @app.post(prefix, status_code=201)
    def create_adjustment(data: AdjustmentCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest = fingerprint("inventory-adjustment-create", data.model_dump(mode="json", exclude={"request_key"}))
            receipt = receipt_replay(session, data.request_key, digest, fail)
            if receipt:
                row = session.get(InventoryAdjustment, receipt.record_id)
                if row is None: fail(410, "原調整草稿已刪除，請開始新的新增操作。")
                response.status_code = 200
                return output(session, row, fail)
            values = validate_input(session, data, fail)
            row = InventoryAdjustment(**data.model_dump(mode="json", exclude={"request_key"}), **values, created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            save_receipt(session, data.request_key, digest, row.id)
            return output(session, row, fail)

    @app.put(prefix + "/{record_id}")
    def update_adjustment(record_id: UUID, data: AdjustmentUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail, data.row_version, draft=True)
            values = validate_input(session, data, fail, row.id)
            for key, value in {**data.model_dump(mode="json", exclude={"row_version"}), **values}.items(): setattr(row, key, value)
            row.row_version += 1
            session.flush()
            return output(session, row, fail)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_adjustment(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail, row_version, draft=True)
            if references(session, "inventory_adjustment", row.id): fail(409, "調整單已被引用，無法刪除。")
            session.delete(row)
            session.flush()

    @app.post(prefix + "/{record_id}/cancel")
    def cancel_adjustment(record_id: UUID, data: AdjustmentAction, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail)
            digest = fingerprint("inventory-adjustment-cancel", dict(id=row.id, row_version=data.row_version))
            receipt = receipt_replay(session, data.request_key, digest, fail)
            if receipt:
                if receipt.record_id != row.id or row.state != "CANCELLED": fail(409, "取消記錄不一致。")
                return output(session, row, fail)
            find(session, row.id, fail, data.row_version, draft=True)
            with edit_grant(session, row.id, "CANCEL"):
                row.state, row.cancelled_at, row.cancelled_by = "CANCELLED", now_utc(), DEMO_ACTOR_ID
                row.row_version += 1
                session.flush()
                save_receipt(session, data.request_key, digest, row.id)
            return output(session, row, fail)

    @app.get(prefix + "/{record_id}/posting-review")
    def review(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            row = find(session, record_id, fail, draft=True)
            snapshot = source_snapshot(session, row, fail)
            document = output(session, row, fail)
            authority = policy(session)
            eligible = not document["blockers"]
            authority["posting_allowed"] = authority["posting_allowed"] and eligible
            if document["blockers"]: authority["message"] += " " + " ".join(document["blockers"])
            return dict(adjustment=document, source=document["source"], current_stock_qty_base=document["source"]["current_stock_qty_base"],
                signed_qty_base_delta=document["signed_qty_base_delta"], expected_stock_qty_base=document["expected_stock_qty_base"],
                confirmation_token=make_token(session, snapshot) if eligible else None, **authority)

    @app.post(prefix + "/{record_id}/post", status_code=201)
    def post(record_id: UUID, data: AdjustmentPost, request: Request, response: Response):
        with request.app.state.db.write() as session:
            row, created = post_adjustment(session, record_id, data, fail)
            if not created: response.status_code = 200
            return output(session, row, fail)
