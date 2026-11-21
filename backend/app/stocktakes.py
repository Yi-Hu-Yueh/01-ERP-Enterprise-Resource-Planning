"""Whole-warehouse counting; only signed final closure changes physical stock."""
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
from .stocktake_models import Stocktake, StocktakeLine, StocktakeException, StocktakePosting, StocktakeEntry
from .stocktake_schemas import StocktakeCreate, StocktakeUpdate, StocktakeAction, CountUpdate, ExceptionCreate, StocktakeClose
from .stocktake_authorization import edit_grant, policy, make_token, verified_confirmation
from .sales_shipments import fields
from .local_demo import canonical
from .decimal_text import exact_difference, exact_sum
from .stock_ledger import compare, on_hand
from .warehouse_freeze import assert_warehouses_unfrozen

ACTIVE = {"ACTIVE_FROZEN", "REVIEW_READY"}
POSTINGS = [StockPosting, SupplierReturnPosting, MaterialIssuePosting, MaterialReturnPosting,
    FinishedGoodsPosting, SalesShipmentPosting, CustomerReturnFinalization, StocktakePosting]


def fingerprint(domain, data):
    return hashlib.sha256(canonical(dict(domain=domain, data=data)).encode()).hexdigest()


def lines_for(session, row):
    return list(session.scalars(select(StocktakeLine).where(StocktakeLine.stocktake_id == row.id).order_by(StocktakeLine.id)))


def exceptions_for(session, row):
    return list(session.scalars(select(StocktakeException).where(StocktakeException.stocktake_id == row.id).order_by(StocktakeException.id)))


def output(session, row):
    warehouse = session.get(Warehouse, row.warehouse_id)
    posting = session.scalar(select(StocktakePosting).where(StocktakePosting.stocktake_id == row.id))
    entries = list(session.scalars(select(StocktakeEntry).where(StocktakeEntry.stocktake_id == row.id)))
    entry_by_line = {entry.stocktake_line_id: entry for entry in entries}
    lines, groups = [], {}
    summary = dict(positions=0, counted=0, uncounted=0, positive=0, negative=0, zero=0)
    for line in lines_for(session, row):
        item, unit = session.get(Item, line.item_id), session.get(Unit, line.unit_id)
        lot = session.get(InventoryLot, line.lot_id) if line.lot_id else None
        variance = exact_difference(line.counted_qty, line.book_qty) if line.counted_qty is not None else None
        entry = entry_by_line.get(line.id)
        lines.append(dict(**fields(line), item_code=item.code, item_name=item.name,
            unit_code=unit.code, unit_name=unit.name, lot_code=lot.lot_code if lot else None,
            warehouse_code=warehouse.code, warehouse_name=warehouse.name,
            counted=line.counted_qty is not None, variance=variance,
            current_stock_qty_base=on_hand(session, line.position_id), posting_entry_id=entry.id if entry else None))
        summary["positions"] += 1
        summary["uncounted" if variance is None else "counted"] += 1
        if variance is not None: summary["positive" if compare(variance, "0") > 0 else "negative" if compare(variance, "0") < 0 else "zero"] += 1
        group = groups.setdefault(unit.id, dict(unit_id=unit.id, unit_code=unit.code, unit_name=unit.name, books=[], counts=[], variances=[]))
        group["books"].append(line.book_qty)
        if variance is not None:
            group["counts"].append(line.counted_qty)
            group["variances"].append(variance)
    summary["unit_groups"] = [dict(unit_id=g["unit_id"], unit_code=g["unit_code"], unit_name=g["unit_name"],
        book_qty=exact_sum(g["books"]), counted_qty=exact_sum(g["counts"]), variance=exact_sum(g["variances"]))
        for g in sorted(groups.values(), key=lambda g: (g["unit_code"], g["unit_id"]))]
    return dict(**fields(row), warehouse_code=warehouse.code, warehouse_name=warehouse.name,
        lines=lines, exceptions=[fields(e) for e in exceptions_for(session, row)], summary=summary,
        posting_id=posting.id if posting else None, posting_state=posting.posting_state if posting else None,
        authorization_basis=posting.authorization_basis if posting else None,
        movements=[fields(e) for e in sorted(entries, key=lambda e: e.stocktake_line_id)])


def find(session, record_id, fail, version=None, states=None):
    row = session.get(Stocktake, str(record_id))
    if row is None: fail(404, "盤點單不存在，請重新讀取。")
    if version is not None and row.row_version != version: fail(409, "盤點單已更新，請重新讀取後再操作。")
    if states is not None and row.state not in states: fail(409, "目前盤點狀態不允許此操作，請重新讀取。")
    return row


def unused_key(session, key, fail):
    key = str(key)
    if session.get(StockCreateReceipt, key) or session.scalar(select(Stocktake.id).where(Stocktake.start_key == key)):
        fail(409, "請求識別已用於另一個操作，請保留原識別重試原操作。")
    if any(session.scalar(select(model.id).where(model.idempotency_key == key)) for model in POSTINGS):
        fail(409, "請求識別已用於其他庫存作業。")


def receipt_replay(session, key, digest, fail):
    receipt = session.get(StockCreateReceipt, str(key))
    if receipt:
        if receipt.resource_kind != "header" or receipt.payload_hash != digest:
            fail(409, "請求識別已使用於不同內容，不能重複操作。")
        return receipt
    unused_key(session, key, fail)
    return None


def save_receipt(session, key, digest, record_id):
    session.add(StockCreateReceipt(request_key=str(key), resource_kind="header", record_id=record_id,
        payload_hash=digest, created_by=DEMO_ACTOR_ID))
    session.flush()


def validate_header(session, data, fail, record_id=None):
    warehouse = session.get(Warehouse, str(data.warehouse_id))
    if not warehouse or not warehouse.is_active: fail(422, "請選擇真實且啟用的盤點倉庫。")
    query = select(Stocktake.id).where(Stocktake.document_no == data.document_no)
    if record_id: query = query.where(Stocktake.id != record_id)
    if session.scalar(query): fail(409, "盤點單號已存在，請使用不同單號。")


def validate_snapshot(session, row, fail, require_count=True):
    if row.state not in ACTIVE or not row.freeze_active or row.snapshot_at is None:
        fail(409, "盤點尚未啟動或已結束，無法確認差異。")
    positions = {p.id: p for p in session.scalars(select(StockPosition).where(StockPosition.warehouse_id == row.warehouse_id))}
    lines = lines_for(session, row)
    if len(lines) != len(positions) or {line.position_id for line in lines} != set(positions):
        fail(409, "整倉盤點快照範圍不完整，請保留凍結並檢查資料。")
    for line in lines:
        position = positions[line.position_id]
        item = session.get(Item, position.item_id)
        if (line.item_id != position.item_id or line.item_kind != item.item_kind or line.unit_id != item.base_unit_id
            or line.warehouse_id != row.warehouse_id or line.lot_id != position.lot_id):
            fail(409, "盤點快照的品項、倉庫、單位或批號身分不一致。")
        if on_hand(session, position.id) != line.book_qty:
            fail(409, "凍結後的庫存與原帳面快照不一致，請保留資料並檢查。")
        if require_count and line.counted_qty is None: fail(409, "尚有未盤點的位置；空白不代表零，請逐列輸入實際數量。")
        if line.counted_qty is not None and compare(line.counted_qty, "0") < 0: fail(422, "實際盤點數量不可為負。")
    exceptions = exceptions_for(session, row)
    if require_count and exceptions: fail(409, "尚有未登錄實物的未解決觀察，不能宣告整倉核對完成；可保留或明確取消盤點。")
    if require_count and row.state != "REVIEW_READY": fail(409, "請完成所有實際盤點數量後再檢視結案。")
    return json.loads(canonical(dict(stocktake=fields(row), warehouse=fields(session.get(Warehouse, row.warehouse_id)),
        lines=[dict(line=fields(line), position=fields(positions[line.position_id]),
            item=fields(session.get(Item, line.item_id)), unit=fields(session.get(Unit, line.unit_id)),
            lot=fields(session.get(InventoryLot, line.lot_id)) if line.lot_id else None,
            current_stock_qty_base=on_hand(session, line.position_id)) for line in lines],
        exceptions=[fields(e) for e in exceptions])))


def close_stocktake(session, record_id, data, fail):
    row = find(session, record_id, fail)
    digest = fingerprint("stocktake-close", dict(id=row.id, row_version=data.row_version,
        confirmation_token_hash=hashlib.sha256(data.confirmation_token.encode()).hexdigest()))
    by_key = session.scalar(select(StocktakePosting).where(StocktakePosting.idempotency_key == str(data.request_key)))
    by_doc = session.scalar(select(StocktakePosting).where(StocktakePosting.stocktake_id == row.id))
    for previous in [by_key, by_doc]:
        if previous:
            if (previous.stocktake_id != row.id or previous.idempotency_key != str(data.request_key)
                or previous.request_fingerprint != digest or previous.posting_state != "POSTED" or row.state != "CLOSED"):
                fail(409, "結案識別或原始確認內容不同，不能再次過帳；請重新讀取原結案。")
            return row, False
    unused_key(session, data.request_key, fail)
    if row.row_version != data.row_version: fail(409, "實際盤點資料已更新，請重新檢視結案。")
    snapshot = validate_snapshot(session, row, fail)
    with verified_confirmation(session, row, data.confirmation_token, snapshot, fail) as grant:
        posting = StocktakePosting(stocktake_id=row.id, idempotency_key=str(data.request_key), request_fingerprint=digest,
            source_version=row.row_version, posting_state="PREPARING", authorization_basis=grant["basis"],
            confirmation_digest=grant["digest"], confirmation_snapshot=grant["snapshot"],
            confirmation_received_at=grant["received_at"], posted_at=grant["received_at"], posted_by=DEMO_ACTOR_ID,
            created_by=DEMO_ACTOR_ID)
        session.add(posting)
        session.flush()
        for line in lines_for(session, row):
            delta = exact_difference(line.counted_qty, line.book_qty)
            if delta != "0":
                session.add(StocktakeEntry(posting_id=posting.id, stocktake_id=row.id, stocktake_line_id=line.id,
                    position_id=line.position_id, qty_base_delta=delta, created_by=DEMO_ACTOR_ID))
        session.flush()
        posting.posting_state = "POSTED"
        session.flush()
        row.state, row.freeze_active = "CLOSED", False
        row.closed_at, row.closed_by = grant["received_at"], DEMO_ACTOR_ID
        row.row_version += 1
        session.flush()
    return row, True


def install_stocktake_routes(app, fail, references):
    prefix = "/api/stocktakes"

    @app.get(prefix + "/policy")
    def read_policy(request: Request):
        with request.app.state.db.read() as session: return policy(session)

    @app.get(prefix)
    def list_stocktakes(request: Request, q: str = ""):
        query = select(Stocktake).join(Warehouse, Warehouse.id == Stocktake.warehouse_id)
        if q: query = query.where(or_(*(column.contains(q, autoescape=True) for column in
            [Stocktake.document_no, Stocktake.note, Warehouse.code, Warehouse.name])))
        with request.app.state.db.read() as session:
            return [output(session, row) for row in session.scalars(query.order_by(Stocktake.document_no))]

    @app.get(prefix + "/{record_id}")
    def read_stocktake(record_id: UUID, request: Request):
        with request.app.state.db.read() as session: return output(session, find(session, record_id, fail))

    @app.post(prefix, status_code=201)
    def create_stocktake(data: StocktakeCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest = fingerprint("stocktake-create", data.model_dump(mode="json", exclude={"request_key"}))
            receipt = receipt_replay(session, data.request_key, digest, fail)
            if receipt:
                row = session.get(Stocktake, receipt.record_id)
                if row is None: fail(410, "原盤點草稿已刪除，請開始新的新增操作。")
                response.status_code = 200
                return output(session, row)
            validate_header(session, data, fail)
            row = Stocktake(**data.model_dump(mode="json", exclude={"request_key"}), created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            save_receipt(session, data.request_key, digest, row.id)
            return output(session, row)

    @app.put(prefix + "/{record_id}")
    def update_stocktake(record_id: UUID, data: StocktakeUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail, data.row_version, {"DRAFT"})
            validate_header(session, data, fail, row.id)
            for key, value in data.model_dump(mode="json", exclude={"row_version"}).items(): setattr(row, key, value)
            row.row_version += 1
            session.flush()
            return output(session, row)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_stocktake(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail, row_version, {"DRAFT"})
            if references(session, "stocktake", row.id): fail(409, "盤點單已被引用，無法刪除。")
            session.delete(row)
            session.flush()

    @app.post(prefix + "/{record_id}/start", status_code=201)
    def start_stocktake(record_id: UUID, data: StocktakeAction, request: Request, response: Response):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail)
            digest = fingerprint("stocktake-start", dict(id=row.id, row_version=data.row_version))
            previous = session.scalar(select(Stocktake).where(Stocktake.start_key == str(data.request_key)))
            if row.start_key or previous:
                if row.start_key != str(data.request_key) or row.start_fingerprint != digest:
                    fail(409, "開始盤點識別或內容不同，不能重新擷取快照。")
                response.status_code = 200
                return output(session, row)
            find(session, row.id, fail, data.row_version, {"DRAFT"})
            unused_key(session, data.request_key, fail)
            warehouse = session.get(Warehouse, row.warehouse_id)
            if not warehouse or not warehouse.is_active: fail(409, "盤點倉庫目前不可使用。")
            assert_warehouses_unfrozen(session, [row.warehouse_id], fail)
            with edit_grant(session, row.id, "START"):
                row.state, row.freeze_active = "STARTING", True
                row.snapshot_at, row.started_by = now_utc(), DEMO_ACTOR_ID
                row.start_key, row.start_fingerprint = str(data.request_key), digest
                row.row_version += 1
                session.flush()
                positions = list(session.scalars(select(StockPosition).where(StockPosition.warehouse_id == row.warehouse_id).order_by(StockPosition.id)))
                for position in positions:
                    item = session.get(Item, position.item_id)
                    session.add(StocktakeLine(stocktake_id=row.id, position_id=position.id, item_id=item.id,
                        item_kind=item.item_kind, unit_id=item.base_unit_id, warehouse_id=row.warehouse_id,
                        lot_id=position.lot_id, book_qty=on_hand(session, position.id), created_by=DEMO_ACTOR_ID))
                session.flush()
                row.state = "ACTIVE_FROZEN" if positions else "REVIEW_READY"
                session.flush()
            return output(session, row)

    @app.put(prefix + "/{record_id}/lines/{line_id}")
    def save_count(record_id: UUID, line_id: UUID, data: CountUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail, data.stocktake_version, ACTIVE)
            line = session.get(StocktakeLine, str(line_id))
            if not line or line.stocktake_id != row.id: fail(404, "盤點明細不存在。")
            if line.row_version != data.row_version: fail(409, "此列盤數已更新，請重新讀取後再輸入。")
            with edit_grant(session, row.id, "COUNT"):
                line.counted_qty, line.note = data.counted_qty, data.note
                line.counted_at = now_utc() if data.counted_qty is not None else None
                line.counted_by = DEMO_ACTOR_ID if data.counted_qty is not None else None
                line.row_version += 1
                session.flush()
                row.state = "REVIEW_READY" if all(l.counted_qty is not None for l in lines_for(session, row)) else "ACTIVE_FROZEN"
                row.row_version += 1
                session.flush()
            return output(session, row)

    @app.post(prefix + "/{record_id}/exceptions", status_code=201)
    def add_exception(record_id: UUID, data: ExceptionCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail)
            digest = fingerprint("stocktake-exception", dict(id=row.id, **data.model_dump(mode="json", exclude={"request_key"})))
            receipt = receipt_replay(session, data.request_key, digest, fail)
            if receipt:
                observation = session.get(StocktakeException, receipt.record_id)
                if not observation or observation.stocktake_id != row.id: fail(409, "觀察記錄對應不一致。")
                response.status_code = 200
                return output(session, row)
            find(session, row.id, fail, data.stocktake_version, ACTIVE)
            with edit_grant(session, row.id, "EXCEPTION"):
                observation = StocktakeException(stocktake_id=row.id, description=data.description, created_by=DEMO_ACTOR_ID)
                session.add(observation)
                session.flush()
                row.row_version += 1
                session.flush()
                save_receipt(session, data.request_key, digest, observation.id)
            return output(session, row)

    @app.post(prefix + "/{record_id}/cancel")
    def cancel_stocktake(record_id: UUID, data: StocktakeAction, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, fail)
            digest = fingerprint("stocktake-cancel", dict(id=row.id, row_version=data.row_version))
            receipt = receipt_replay(session, data.request_key, digest, fail)
            if receipt:
                if receipt.record_id != row.id or row.state != "CANCELLED": fail(409, "取消記錄不一致。")
                return output(session, row)
            find(session, row.id, fail, data.row_version, ACTIVE | {"DRAFT"})
            with edit_grant(session, row.id, "CANCEL"):
                row.state, row.freeze_active = "CANCELLED", False
                row.cancelled_at, row.cancelled_by = now_utc(), DEMO_ACTOR_ID
                row.row_version += 1
                session.flush()
                save_receipt(session, data.request_key, digest, row.id)
            return output(session, row)

    @app.get(prefix + "/{record_id}/closing-review")
    def review_closure(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            row = find(session, record_id, fail, states=ACTIVE)
            snapshot = validate_snapshot(session, row, fail, require_count=False)
            document = output(session, row)
            blockers = []
            if document["summary"]["uncounted"]:
                blockers.append("尚有未盤點的位置；空白不代表零，請逐列輸入實際數量。")
            if document["exceptions"]:
                blockers.append("尚有未登錄實物的未解決觀察，不能宣告整倉核對完成；可保留或明確取消盤點。")
            eligible = not blockers and row.state == "REVIEW_READY"
            authority = policy(session)
            authority["posting_allowed"] = authority["posting_allowed"] and eligible
            if blockers: authority["message"] += " " + " ".join(blockers)
            expected = [dict(stocktake_line_id=line["id"], position_id=line["position_id"],
                qty_base_delta=line["variance"], expected_stock_qty_base=line["counted_qty"],
                unit_id=line["unit_id"], unit_code=line["unit_code"], item_code=line["item_code"], lot_code=line["lot_code"])
                for line in document["lines"] if line["variance"] not in {None, "0"}]
            return dict(stocktake=document, confirmation_token=make_token(session, snapshot) if eligible else None,
                expected_movements=expected, **authority)

    @app.post(prefix + "/{record_id}/close", status_code=201)
    def close(record_id: UUID, data: StocktakeClose, request: Request, response: Response):
        with request.app.state.db.write() as session:
            row, created = close_stocktake(session, record_id, data, fail)
            if not created: response.status_code = 200
            return output(session, row)
