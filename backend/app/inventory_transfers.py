"""Direct interwarehouse transfers: two conserved legs of one physical batch."""
import hashlib
import json
from uuid import UUID
from fastapi import Request, Response
from sqlalchemy import or_, select
from .db import DEMO_ACTOR_ID
from .models import Warehouse, Item, now_utc
from .receiving_models import InventoryLot, StockPosition, StockCreateReceipt
from .stocktake_models import Stocktake
from .transfer_models import InventoryTransfer, InventoryTransferPosting, InventoryTransferEntry
from .transfer_schemas import TransferCreate, TransferUpdate, TransferAction, TransferPost
from .transfer_authorization import policy, make_token, verified_confirmation, edit_grant
from .inventory_adjustments import resolve_position, source_output, fingerprint, POSTINGS
from .sales_shipments import fields
from .decimal_text import exact_sum
from .stock_ledger import compare, negate, on_hand
from .local_demo import canonical
from .warehouse_freeze import assert_warehouses_unfrozen


def destination_position(session, source, warehouse_id):
    return session.scalar(select(StockPosition).where(StockPosition.item_id == source.item_id,
        StockPosition.warehouse_id == warehouse_id, StockPosition.lot_id == source.lot_id))


def destination_output(session, position_id, warehouse_id, fail):
    position, item, unit, source_warehouse, lot = resolve_position(session, position_id, fail)
    warehouse = session.get(Warehouse, str(warehouse_id))
    if not warehouse: fail(422, "請選擇真實存在的目的倉庫。")
    target = destination_position(session, position, warehouse.id)
    if target:
        resolved, target_item, target_unit, _, target_lot = resolve_position(session, target.id, fail)
        if (target_item.id != item.id or target_unit.id != unit.id or resolved.lot_id != position.lot_id
            or (target_lot and fields(target_lot) != fields(lot))):
            fail(409, "目的位置的品項、單位或原始批號來源不相容，不能合併。")
    frozen = session.scalar(select(Stocktake).where(Stocktake.warehouse_id == warehouse.id, Stocktake.freeze_active.is_(True)))
    return dict(id=target.id if target else None, position_id=target.id if target else None,
        item_id=item.id, item_kind=item.item_kind, item_code=item.code, item_name=item.name,
        unit_id=unit.id, unit_code=unit.code, unit_name=unit.name,
        warehouse_id=warehouse.id, warehouse_code=warehouse.code, warehouse_name=warehouse.name,
        lot_id=position.lot_id, lot_code=lot.lot_code if lot else None,
        current_stock_qty_base=on_hand(session, target.id) if target else "0", will_create=target is None,
        frozen=frozen is not None, active_stocktake_id=frozen.id if frozen else None,
        active_stocktake_no=frozen.document_no if frozen else None,
        **provenance(lot))


def provenance(lot):
    return {"lot_" + key if key == "origin_kind" else key: getattr(lot, key) if lot else None
        for key in ["origin_kind", "supplier_id", "supplier_lot_code", "manufactured_date", "expiry_date", "traceability_note"]}


def sources_for(session, position_id, destination_id, fail):
    source = source_output(session, position_id, fail)
    _, _, _, _, lot = resolve_position(session, position_id, fail)
    source.update(provenance(lot))
    return source, destination_output(session, position_id, destination_id, fail)


def blockers_for(row, source, destination):
    blockers = []
    if source["warehouse_id"] == destination["warehouse_id"]: blockers.append("來源與目的必須是不同倉庫。")
    for name, value in [("來源", source), ("目的", destination)]:
        if value["frozen"]: blockers.append(name + "倉庫已凍結（盤點單 " + value["active_stocktake_no"] + "），請先由盤點程序合法處理並重新檢視。")
    if compare(row.quantity, source["current_stock_qty_base"]) > 0: blockers.append("調撥數量超過來源倉此批實際庫存，不能產生負庫存。")
    return blockers


def output(session, row, fail):
    source, destination = sources_for(session, row.source_position_id, row.destination_warehouse_id, fail)
    posting = session.scalar(select(InventoryTransferPosting).where(InventoryTransferPosting.transfer_id == row.id))
    before_source, before_destination = source["current_stock_qty_base"], destination["current_stock_qty_base"]
    if posting:
        snapshot = json.loads(posting.confirmation_snapshot)["snapshot"]
        before_source, before_destination = snapshot["source_before_qty_base"], snapshot["destination_before_qty_base"]
    return dict(**fields(row), source=source, destination=destination,
        source_before_qty_base=before_source, destination_before_qty_base=before_destination,
        source_delta_qty_base=negate(row.quantity), destination_delta_qty_base=row.quantity, net_qty_base="0",
        source_after_qty_base=exact_sum([before_source, negate(row.quantity)]),
        destination_after_qty_base=exact_sum([before_destination, row.quantity]),
        blockers=blockers_for(row, source, destination) if row.state == "DRAFT" else [],
        posting_id=posting.id if posting else None, posting_state=posting.posting_state if posting else row.state,
        authorization_basis=posting.authorization_basis if posting else None,
        movements=[fields(entry) for entry in session.scalars(select(InventoryTransferEntry).where(
            InventoryTransferEntry.transfer_id == row.id).order_by(InventoryTransferEntry.leg_no))])


def find(session, record_id, fail, version=None, draft=False):
    row = session.get(InventoryTransfer, str(record_id))
    if row is None: fail(404, "倉庫調撥單不存在，請重新讀取。")
    if version is not None and row.row_version != version: fail(409, "調撥單已更新，請重新讀取。")
    if draft and row.state != "DRAFT": fail(409, "此調撥單已過帳或取消，不能修改、刪除或再次過帳。")
    return row


def unused_key(session, key, fail):
    key = str(key)
    if (session.get(StockCreateReceipt, key) or session.scalar(select(Stocktake.id).where(Stocktake.start_key == key))
        or any(session.scalar(select(model.id).where(model.idempotency_key == key)) for model in POSTINGS + [InventoryTransferPosting])):
        fail(409, "請求識別已用於其他操作，請保留原識別重試原操作。")


def receipt_replay(session, key, digest, fail):
    receipt = session.get(StockCreateReceipt, str(key))
    if receipt:
        if receipt.resource_kind != "header" or receipt.payload_hash != digest: fail(409, "請求識別對應不同內容，不能重複執行。")
        return receipt
    unused_key(session, key, fail)
    return None


def save_receipt(session, key, digest, record_id):
    session.add(StockCreateReceipt(request_key=str(key), resource_kind="header", record_id=record_id,
        payload_hash=digest, created_by=DEMO_ACTOR_ID))
    session.flush()


def validate_input(session, data, fail, record_id=None):
    position, item, unit, warehouse, lot = resolve_position(session, data.source_position_id, fail)
    target = session.get(Warehouse, str(data.destination_warehouse_id))
    if not target: fail(422, "目的倉庫不存在，不能建立虛構倉庫。")
    if target.id == warehouse.id: fail(422, "來源與目的必須是不同倉庫。")
    if not target.is_active or not warehouse.is_active: fail(409, "來源與目的倉庫均須啟用。")
    destination_output(session, position.id, target.id, fail)
    query = select(InventoryTransfer.id).where(InventoryTransfer.document_no == data.document_no)
    if record_id: query = query.where(InventoryTransfer.id != record_id)
    if session.scalar(query): fail(409, "倉庫調撥單號已存在，請使用不同單號。")
    return dict(item_id=item.id, item_kind=item.item_kind, unit_id=unit.id,
        source_warehouse_id=warehouse.id, lot_id=position.lot_id)


def freeze_history(session, warehouse_id):
    return [dict(id=s.id, row_version=s.row_version, state=s.state, freeze_active=s.freeze_active, snapshot_at=s.snapshot_at)
        for s in session.scalars(select(Stocktake).where(Stocktake.warehouse_id == warehouse_id,
            Stocktake.snapshot_at.is_not(None)).order_by(Stocktake.id))]


def source_snapshot(session, row, fail):
    position, item, unit, warehouse, lot = resolve_position(session, row.source_position_id, fail)
    current = dict(item_id=item.id, item_kind=item.item_kind, unit_id=unit.id, source_warehouse_id=warehouse.id, lot_id=position.lot_id)
    if any(getattr(row, key) != value for key, value in current.items()): fail(409, "調撥單與原始品項、單位、倉庫或批號來源不一致。")
    target_warehouse = session.get(Warehouse, row.destination_warehouse_id)
    if not target_warehouse or not target_warehouse.is_active or not warehouse.is_active: fail(409, "來源與目的倉庫必須真實存在且啟用。")
    target = destination_position(session, position, row.destination_warehouse_id)
    destination_output(session, position.id, row.destination_warehouse_id, fail)
    return json.loads(canonical(dict(transfer=fields(row), source_position=fields(position), item=fields(item), unit=fields(unit),
        source_warehouse=fields(warehouse), destination_warehouse=fields(target_warehouse), lot=fields(lot) if lot else None,
        destination_position=fields(target) if target else None,
        source_before_qty_base=on_hand(session, position.id), destination_before_qty_base=on_hand(session, target.id) if target else "0",
        source_freeze_history=freeze_history(session, warehouse.id), destination_freeze_history=freeze_history(session, target_warehouse.id))))


def post_transfer(session, record_id, data, fail):
    row = find(session, record_id, fail)
    digest = fingerprint("inventory-transfer-post", dict(id=row.id, row_version=data.row_version,
        confirmation_token_hash=hashlib.sha256(data.confirmation_token.encode()).hexdigest()))
    by_key = session.scalar(select(InventoryTransferPosting).where(InventoryTransferPosting.idempotency_key == str(data.request_key)))
    by_doc = session.scalar(select(InventoryTransferPosting).where(InventoryTransferPosting.transfer_id == row.id))
    for previous in [by_key, by_doc]:
        if previous:
            if (previous.transfer_id != row.id or previous.idempotency_key != str(data.request_key)
                or previous.request_fingerprint != digest or previous.posting_state != "POSTED" or row.state != "POSTED"):
                fail(409, "調撥識別或原始內容不同，不能再次產生庫存異動；請重新讀取。")
            return row, False
    find(session, row.id, fail, data.row_version, draft=True)
    unused_key(session, data.request_key, fail)
    snapshot = source_snapshot(session, row, fail)
    source, destination = sources_for(session, row.source_position_id, row.destination_warehouse_id, fail)
    assert_warehouses_unfrozen(session, [row.source_warehouse_id, row.destination_warehouse_id], fail)
    blockers = blockers_for(row, source, destination)
    if blockers: fail(409, " ".join(blockers))
    with verified_confirmation(session, row, data.confirmation_token, snapshot, fail) as grant:
        posting = InventoryTransferPosting(transfer_id=row.id, idempotency_key=str(data.request_key), request_fingerprint=digest,
            source_version=row.row_version, posting_state="PREPARING", authorization_basis=grant["basis"],
            confirmation_digest=grant["digest"], confirmation_snapshot=grant["snapshot"], confirmation_received_at=grant["received_at"],
            posted_at=grant["received_at"], posted_by=DEMO_ACTOR_ID, created_by=DEMO_ACTOR_ID)
        session.add(posting); session.flush()
        target = destination_position(session, session.get(StockPosition, row.source_position_id), row.destination_warehouse_id)
        if not target:
            target = StockPosition(item_id=row.item_id, warehouse_id=row.destination_warehouse_id, lot_id=row.lot_id, created_by=DEMO_ACTOR_ID)
            session.add(target); session.flush()
        row.destination_position_id = target.id
        session.flush()
        for leg_no, position_id, delta in [(1, row.source_position_id, negate(row.quantity)), (2, target.id, row.quantity)]:
            session.add(InventoryTransferEntry(posting_id=posting.id, transfer_id=row.id, position_id=position_id,
                leg_no=leg_no, qty_base_delta=delta, created_by=DEMO_ACTOR_ID))
            session.flush()
        posting.posting_state = "POSTED"; session.flush()
        row.state, row.posted_at, row.posted_by = "POSTED", grant["received_at"], DEMO_ACTOR_ID
        row.row_version += 1; session.flush()
    return row, True


def install_transfer_routes(app, fail, references):
    prefix = "/api/inventory-transfers"

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
            result = []
            for position in session.scalars(query.order_by(Item.code, Warehouse.code, InventoryLot.lot_code, StockPosition.id)):
                source = source_output(session, position.id, fail)
                source.update(provenance(session.get(InventoryLot, position.lot_id) if position.lot_id else None)); result.append(source)
            return result

    @app.get(prefix + "/destinations")
    def destinations(source_position_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            position, _, _, _, _ = resolve_position(session, source_position_id, fail)
            return [destination_output(session, position.id, warehouse.id, fail) for warehouse in session.scalars(
                select(Warehouse).where(Warehouse.id != position.warehouse_id, Warehouse.is_active.is_(True)).order_by(Warehouse.code))]

    @app.get(prefix)
    def list_transfers(request: Request, q: str = ""):
        query = select(InventoryTransfer).join(Item, Item.id == InventoryTransfer.item_id).outerjoin(InventoryLot, InventoryLot.id == InventoryTransfer.lot_id)
        if q:
            warehouse_ids = select(Warehouse.id).where(or_(Warehouse.code.contains(q, autoescape=True), Warehouse.name.contains(q, autoescape=True)))
            query = query.where(or_(*(column.contains(q, autoescape=True) for column in [InventoryTransfer.document_no,
                InventoryTransfer.reason, InventoryTransfer.note, Item.code, Item.name, InventoryLot.lot_code]),
                InventoryTransfer.source_warehouse_id.in_(warehouse_ids), InventoryTransfer.destination_warehouse_id.in_(warehouse_ids)))
        with request.app.state.db.read() as session: return [output(session, row, fail) for row in session.scalars(query.order_by(InventoryTransfer.document_no))]

    @app.get(prefix + "/{record_id}")
    def read_transfer(record_id: UUID, request: Request):
        with request.app.state.db.read() as session: return output(session, find(session, record_id, fail), fail)

    @app.post(prefix, status_code=201)
    def create_transfer(data: TransferCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest = fingerprint("inventory-transfer-create", data.model_dump(mode="json", exclude={"request_key"}))
            receipt = receipt_replay(session, data.request_key, digest, fail)
            if receipt:
                row = session.get(InventoryTransfer, receipt.record_id)
                if row is None: fail(410, "原調撥草稿已刪除，請開始新的新增操作。")
                response.status_code = 200; return output(session, row, fail)
            values = validate_input(session, data, fail)
            row = InventoryTransfer(**data.model_dump(mode="json", exclude={"request_key"}), **values, created_by=DEMO_ACTOR_ID)
            session.add(row); session.flush(); save_receipt(session, data.request_key, digest, row.id)
            return output(session, row, fail)

    @app.put(prefix + "/{record_id}")
    def update_transfer(record_id: UUID, data: TransferUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail, data.row_version, draft=True)
            values = validate_input(session, data, fail, row.id)
            for key, value in {**data.model_dump(mode="json", exclude={"row_version"}), **values}.items(): setattr(row, key, value)
            row.row_version += 1; session.flush()
            return output(session, row, fail)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_transfer(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail, row_version, draft=True)
            if references(session, "inventory_transfer", row.id): fail(409, "調撥單已被引用，無法刪除。")
            session.delete(row); session.flush()

    @app.post(prefix + "/{record_id}/cancel")
    def cancel_transfer(record_id: UUID, data: TransferAction, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail)
            digest = fingerprint("inventory-transfer-cancel", dict(id=row.id, row_version=data.row_version))
            receipt = receipt_replay(session, data.request_key, digest, fail)
            if receipt:
                if receipt.record_id != row.id or row.state != "CANCELLED": fail(409, "取消記錄不一致。")
                return output(session, row, fail)
            find(session, row.id, fail, data.row_version, draft=True)
            with edit_grant(session, row.id, "CANCEL"):
                row.state, row.cancelled_at, row.cancelled_by = "CANCELLED", now_utc(), DEMO_ACTOR_ID
                row.row_version += 1; session.flush(); save_receipt(session, data.request_key, digest, row.id)
            return output(session, row, fail)

    @app.get(prefix + "/{record_id}/posting-review")
    def review(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            row = find(session, record_id, fail, draft=True)
            snapshot = source_snapshot(session, row, fail); document = output(session, row, fail); authority = policy(session)
            eligible = not document["blockers"]; authority["posting_allowed"] = authority["posting_allowed"] and eligible
            if document["blockers"]: authority["message"] += " " + " ".join(document["blockers"])
            names = ["source", "destination", "source_before_qty_base", "destination_before_qty_base", "source_delta_qty_base",
                "destination_delta_qty_base", "source_after_qty_base", "destination_after_qty_base", "net_qty_base"]
            return dict(transfer=document, **{key: document[key] for key in names},
                confirmation_token=make_token(session, snapshot) if eligible else None, **authority)

    @app.post(prefix + "/{record_id}/post", status_code=201)
    def post(record_id: UUID, data: TransferPost, request: Request, response: Response):
        with request.app.state.db.write() as session:
            row, created = post_transfer(session, record_id, data, fail)
            if not created: response.status_code = 200
            return output(session, row, fail)
