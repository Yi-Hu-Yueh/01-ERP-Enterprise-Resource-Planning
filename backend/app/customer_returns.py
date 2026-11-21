"""Customer return registration and explicitly designated same-position restocking."""
import hashlib
import json
from uuid import UUID
from fastapi import Request, Response
from sqlalchemy import or_, select
from .db import DEMO_ACTOR_ID
from .models import Customer, Item, Unit, Warehouse
from .sales_models import SalesLine, SalesOrder
from .receiving_models import InventoryLot, StockPosition, StockCreateReceipt, StockPosting
from .return_models import SupplierReturnPosting
from .issue_models import MaterialIssuePosting
from .material_return_models import MaterialReturnPosting
from .finished_goods_models import FinishedGoodsPosting
from .sales_shipment_models import SalesShipment, SalesShipmentPosting, SalesShipmentEntry
from .sales_shipments import fields, line_source, position_for
from .customer_return_models import CustomerReturn, CustomerReturnFinalization, CustomerReturnEntry
from .customer_return_schemas import ReturnCreate, ReturnUpdate, FinalizeReturn
from .customer_return_authorization import policy, make_token, verified_confirmation
from .decimal_text import exact_difference, exact_sum
from .stock_ledger import compare, negate, on_hand
from .local_demo import canonical

INPUT_SYSTEM_FIELDS = {"request_key", "row_version"}
FINAL_STATES = {"POSTED", "RECORDED_NO_RESTOCK"}


def shipment_source(session, shipment_id, fail, version=None):
    shipment = session.get(SalesShipment, str(shipment_id))
    if shipment is None: fail(422, "來源銷售出貨單不存在，請選擇真實已過帳出貨。")
    posting = session.scalar(select(SalesShipmentPosting).where(SalesShipmentPosting.shipment_id == shipment.id))
    entries = list(session.scalars(select(SalesShipmentEntry).where(SalesShipmentEntry.shipment_id == shipment.id)))
    if (not posting or posting.posting_state != "POSTED" or len(entries) != 1
        or shipment.row_version != posting.source_version + 1 or compare(shipment.quantity, "0") <= 0):
        fail(409, "客戶退貨只能引用具有真實負庫存異動的已過帳出貨。")
    entry = entries[0]
    if (entry.posting_id != posting.id or entry.position_id != shipment.position_id
        or entry.leg_no != 1 or entry.qty_base_delta != negate(shipment.quantity)
        or entry.created_by != posting.created_by):
        fail(409, "原出貨與實際庫存異動不一致，無法退貨。")
    if version is not None and shipment.row_version != version:
        fail(409, "來源出貨版次不一致，請重新讀取。")
    # A POSTED shipment is immutable. Safe later sales-order edits must not
    # make its historical versions disappear from the returnable pool.
    line, order = line_source(session, shipment.sales_order_line_id, fail)
    position, _ = position_for(session, shipment.position_id, line, fail)
    if (shipment.sales_order_id != order.id or shipment.customer_id != order.customer_id
        or shipment.product_id != line.item_id or shipment.unit_id != line.unit_id
        or shipment.warehouse_id != position.warehouse_id or shipment.lot_id != position.lot_id):
        fail(409, "原出貨客戶、訂單明細、成品單位或原批號身分不一致。")
    return shipment, posting, entry


def returned_quantity(session, shipment_id):
    """Both genuine completed outcomes consume the same immutable shipment pool."""
    return exact_sum(session.connection().exec_driver_sql("""
        SELECT r.quantity FROM customer_return r
        JOIN customer_return_finalization f ON f.return_id=r.id
        JOIN sales_shipment s ON s.id=r.shipment_id
        JOIN sales_shipment_posting sp ON sp.shipment_id=s.id AND sp.posting_state='POSTED'
        JOIN sales_shipment_entry se ON se.posting_id=sp.id AND se.shipment_id=s.id
        WHERE s.id=? AND s.row_version=sp.source_version+1 AND r.shipment_version=s.row_version
          AND se.position_id=s.position_id AND se.leg_no=1
          AND se.qty_base_delta=erp_decimal_neg(s.quantity) AND se.created_by=sp.created_by
          AND r.sales_order_line_id=s.sales_order_line_id AND r.sales_order_id=s.sales_order_id
          AND r.customer_id=s.customer_id AND r.product_id=s.product_id AND r.unit_id=s.unit_id
          AND r.position_id=s.position_id AND r.warehouse_id=s.warehouse_id AND r.lot_id IS s.lot_id
          AND r.row_version=f.source_version+1 AND erp_decimal_cmp(r.quantity,'0')>0
          AND ((f.finalization_state='RECORDED_NO_RESTOCK' AND r.restock_eligible=0
            AND NOT EXISTS(SELECT 1 FROM customer_return_entry e WHERE e.finalization_id=f.id))
           OR (f.finalization_state='POSTED' AND r.restock_eligible=1
            AND EXISTS(SELECT 1 FROM customer_return_entry e WHERE e.finalization_id=f.id
              AND e.return_id=r.id AND e.position_id=r.position_id AND e.leg_no=1
              AND e.qty_base_delta=r.quantity AND e.created_by=f.created_by)))
        """, (str(shipment_id),)).scalars())


def source_output(session, shipment):
    order = session.get(SalesOrder, shipment.sales_order_id)
    customer = session.get(Customer, shipment.customer_id)
    product = session.get(Item, shipment.product_id)
    unit = session.get(Unit, shipment.unit_id)
    warehouse = session.get(Warehouse, shipment.warehouse_id)
    lot = session.get(InventoryLot, shipment.lot_id) if shipment.lot_id else None
    returned = returned_quantity(session, shipment.id)
    return dict(shipment_id=shipment.id, shipment_version=shipment.row_version, shipment_no=shipment.document_no,
        sales_order_line_id=shipment.sales_order_line_id, sales_order_id=order.id, sales_order_no=order.document_no,
        customer_id=customer.id, customer_code=customer.code, customer_name=customer.name,
        product_id=product.id, product_code=product.code, product_name=product.name,
        unit_id=unit.id, unit_code=unit.code, unit_name=unit.name, position_id=shipment.position_id,
        warehouse_id=warehouse.id, warehouse_code=warehouse.code, warehouse_name=warehouse.name,
        lot_id=shipment.lot_id, lot_code=lot.lot_code if lot else None,
        original_shipped_qty_base=shipment.quantity, finalized_returned_qty_base=returned,
        remaining_returnable_qty_base=exact_difference(shipment.quantity, returned),
        current_stock_qty_base=on_hand(session, shipment.position_id))


def derived_values(session, data, fail):
    shipment, _, _ = shipment_source(session, data.shipment_id, fail, data.shipment_version)
    return {key: getattr(shipment, key) for key in ["sales_order_line_id", "sales_order_id", "customer_id",
        "product_id", "unit_id", "position_id", "warehouse_id", "lot_id"]}


def output(session, row):
    shipment = session.get(SalesShipment, row.shipment_id)
    finalization = session.scalar(select(CustomerReturnFinalization).where(CustomerReturnFinalization.return_id == row.id))
    source = source_output(session, shipment)
    movements = []
    if finalization and finalization.finalization_state == "POSTED":
        for entry in session.scalars(select(CustomerReturnEntry).where(CustomerReturnEntry.finalization_id == finalization.id)):
            movements.append(dict(**fields(entry), item_id=row.product_id, warehouse_id=row.warehouse_id,
                lot_id=row.lot_id, lot_code=source["lot_code"]))
    return dict(**fields(row), source=source,
        finalization_state=finalization.finalization_state if finalization else "DRAFT",
        finalization_id=finalization.id if finalization else None,
        finalized_at=finalization.finalized_at if finalization else None,
        authorization_basis=finalization.authorization_basis if finalization else None,
        current_stock_qty_base=source["current_stock_qty_base"], movements=movements)


def validate_snapshot(session, row, fail):
    shipment, posting, entry = shipment_source(session, row.shipment_id, fail, row.shipment_version)
    if any(getattr(row, key) != getattr(shipment, key) for key in ["sales_order_line_id", "sales_order_id",
        "customer_id", "product_id", "unit_id", "position_id", "warehouse_id", "lot_id"]):
        fail(409, "客戶退貨與原出貨的真實身分或批號不一致，請重新選取來源。")
    if compare(row.quantity, "0") <= 0:
        fail(422, "完成退貨數量必須大於零；零或有號量只能保存草稿，不能作為額外出貨。")
    returned = returned_quantity(session, shipment.id)
    remaining = exact_difference(shipment.quantity, returned)
    if compare(row.quantity, remaining) > 0:
        fail(409, "本次退貨量超過原出貨的剩餘可退貨量，沒有完成登記或回庫。")
    position = session.get(StockPosition, shipment.position_id)
    snapshot = dict(return_document=fields(row), shipment=fields(shipment), shipment_posting=fields(posting),
        shipment_entry=fields(entry), position=fields(position),
        customer=fields(session.get(Customer, row.customer_id)), product=fields(session.get(Item, row.product_id)),
        unit=fields(session.get(Unit, row.unit_id)), warehouse=fields(session.get(Warehouse, row.warehouse_id)),
        lot=fields(session.get(InventoryLot, row.lot_id)) if row.lot_id else None,
        finalized_returned_qty_base=returned, remaining_returnable_qty_base=remaining,
        current_stock_qty_base=on_hand(session, row.position_id))
    return json.loads(canonical(snapshot))


def finalize_return(session, record_id, data, fail):
    row = session.get(CustomerReturn, str(record_id))
    if row is None: fail(404, "客戶退貨單不存在，請重新讀取。")
    digest = hashlib.sha256(canonical(dict(domain="customer-return", id=row.id, row_version=data.row_version,
        confirmation_token_hash=hashlib.sha256(data.confirmation_token.encode()).hexdigest())).encode()).hexdigest()
    by_key = session.scalar(select(CustomerReturnFinalization).where(CustomerReturnFinalization.idempotency_key == str(data.request_key)))
    by_doc = session.scalar(select(CustomerReturnFinalization).where(CustomerReturnFinalization.return_id == row.id))
    for previous in [by_key, by_doc]:
        if previous:
            if (previous.return_id != row.id or previous.idempotency_key != str(data.request_key)
                or previous.request_fingerprint != digest or previous.finalization_state not in FINAL_STATES):
                fail(409, "退貨確認識別或原始內容不同，不能重複完成登記或回庫；請重新讀取。")
            return row, False
    if not policy(session)["posting_allowed"]:
        fail(403, "請使用專用 localhost 展示服務，逐單檢視並明確確認退貨處置。")
    if row.row_version != data.row_version: fail(409, "退貨草稿已更新，請重新讀取後檢視。")
    for model in [StockPosting, SupplierReturnPosting, MaterialIssuePosting, MaterialReturnPosting,
        FinishedGoodsPosting, SalesShipmentPosting]:
        if session.scalar(select(model.id).where(model.idempotency_key == str(data.request_key))):
            fail(409, "此確認識別已用於其他庫存作業，不能移用客戶退貨。")
    snapshot = validate_snapshot(session, row, fail)
    with verified_confirmation(session, row, data.confirmation_token, snapshot, fail) as grant:
        row.row_version += 1
        session.flush()
        finalization = CustomerReturnFinalization(return_id=row.id, idempotency_key=str(data.request_key),
            request_fingerprint=digest, source_version=data.row_version, finalization_state="PREPARING",
            authorization_basis=grant["basis"], confirmation_digest=grant["digest"],
            confirmation_snapshot=grant["snapshot"], confirmation_received_at=grant["received_at"],
            finalized_at=grant["received_at"], created_by=DEMO_ACTOR_ID, finalized_by=DEMO_ACTOR_ID)
        session.add(finalization)
        session.flush()
        if row.restock_eligible:
            session.add(CustomerReturnEntry(finalization_id=finalization.id, return_id=row.id,
                position_id=row.position_id, qty_base_delta=row.quantity, created_by=DEMO_ACTOR_ID))
            session.flush()
        finalization.finalization_state = "POSTED" if row.restock_eligible else "RECORDED_NO_RESTOCK"
        session.flush()
    return row, True


def install_customer_return_routes(app, fail, references):
    prefix = "/api/customer-returns"

    def find(session, record_id, version=None):
        row = session.get(CustomerReturn, str(record_id))
        if row is None: fail(404, "客戶退貨單不存在，請重新讀取。")
        if version is not None:
            if row.row_version != version: fail(409, "客戶退貨單已更新，請重新讀取。")
            if references(session, "customer_return", row.id): fail(409, "退貨已完成或被引用，無法修改或刪除。")
        return row

    def validate(session, data, record_id=None):
        values = derived_values(session, data, fail)
        query = select(CustomerReturn.id).where(CustomerReturn.document_no == data.document_no)
        if record_id: query = query.where(CustomerReturn.id != record_id)
        if session.scalar(query): fail(409, "客戶退貨單號已存在，請使用不同單號。")
        return values

    @app.get(prefix + "/policy")
    def read_policy(request: Request):
        with request.app.state.db.read() as session: return policy(session)

    @app.get(prefix + "/sources")
    def sources(request: Request, q: str = "", shipment_id: UUID | None = None):
        query = select(SalesShipment).join(SalesShipmentPosting, SalesShipmentPosting.shipment_id == SalesShipment.id).join(
            SalesOrder, SalesOrder.id == SalesShipment.sales_order_id).join(Customer, Customer.id == SalesShipment.customer_id).join(
            Item, Item.id == SalesShipment.product_id).where(SalesShipmentPosting.posting_state == "POSTED")
        if shipment_id: query = query.where(SalesShipment.id == str(shipment_id))
        if q: query = query.where(or_(*(c.contains(q, autoescape=True) for c in [SalesShipment.document_no,
            SalesOrder.document_no, Customer.code, Customer.name, Item.code, Item.name])))
        with request.app.state.db.read() as session:
            result = []
            for row in session.scalars(query.order_by(SalesShipment.document_no)):
                shipment_source(session, row.id, fail)
                result.append(source_output(session, row))
            return result

    @app.get(prefix)
    def list_returns(request: Request, q: str = ""):
        query = select(CustomerReturn).join(SalesShipment, SalesShipment.id == CustomerReturn.shipment_id).join(
            SalesOrder, SalesOrder.id == CustomerReturn.sales_order_id).join(Customer, Customer.id == CustomerReturn.customer_id).join(
            Item, Item.id == CustomerReturn.product_id).outerjoin(InventoryLot, InventoryLot.id == CustomerReturn.lot_id)
        if q: query = query.where(or_(*(c.contains(q, autoescape=True) for c in [CustomerReturn.document_no,
            CustomerReturn.reason, CustomerReturn.note, SalesShipment.document_no, SalesOrder.document_no,
            Customer.code, Customer.name, Item.code, Item.name, InventoryLot.lot_code])))
        with request.app.state.db.read() as session:
            return [output(session, row) for row in session.scalars(query.order_by(CustomerReturn.document_no))]

    @app.get(prefix + "/{record_id}")
    def read_return(record_id: UUID, request: Request):
        with request.app.state.db.read() as session: return output(session, find(session, record_id))

    @app.post(prefix, status_code=201)
    def create_return(data: ReturnCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest = hashlib.sha256(canonical(dict(domain="customer-return-create",
                data=data.model_dump(mode="json", exclude={"request_key"}))).encode()).hexdigest()
            receipt = session.get(StockCreateReceipt, str(data.request_key))
            if receipt:
                if receipt.resource_kind != "header" or receipt.payload_hash != digest: fail(409, "新增請求識別已用於不同內容。")
                row = session.get(CustomerReturn, receipt.record_id)
                if row is None: fail(410, "原退貨草稿已刪除，請開始新的新增操作。")
                response.status_code = 200
                return output(session, row)
            values = validate(session, data)
            row = CustomerReturn(**data.model_dump(mode="json", exclude=INPUT_SYSTEM_FIELDS), **values, created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            session.add(StockCreateReceipt(request_key=str(data.request_key), resource_kind="header", record_id=row.id,
                payload_hash=digest, created_by=DEMO_ACTOR_ID))
            session.flush()
            return output(session, row)

    @app.put(prefix + "/{record_id}")
    def update_return(record_id: UUID, data: ReturnUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, data.row_version)
            values = validate(session, data, row.id)
            for key, value in {**data.model_dump(mode="json", exclude=INPUT_SYSTEM_FIELDS), **values}.items(): setattr(row, key, value)
            row.row_version += 1
            session.flush()
            return output(session, row)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_return(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            session.delete(find(session, record_id, row_version))
            session.flush()

    @app.get(prefix + "/{record_id}/finalization-review")
    def review(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            row = find(session, record_id)
            if session.scalar(select(CustomerReturnFinalization.id).where(CustomerReturnFinalization.return_id == row.id)):
                fail(409, "退貨已完成，只能檢視原始處置與實際庫存異動。")
            snapshot = validate_snapshot(session, row, fail)
            return dict(return_document=output(session, row), source=source_output(session, session.get(SalesShipment, row.shipment_id)),
                current_stock_qty_base=snapshot["current_stock_qty_base"],
                expected_stock_qty_base=exact_sum([snapshot["current_stock_qty_base"], row.quantity]) if row.restock_eligible else snapshot["current_stock_qty_base"],
                remaining_after_qty_base=exact_difference(snapshot["remaining_returnable_qty_base"], row.quantity),
                confirmation_token=make_token(session, snapshot), policy=policy(session))

    @app.post(prefix + "/{record_id}/finalize", status_code=201)
    def finalize(record_id: UUID, data: FinalizeReturn, request: Request, response: Response):
        with request.app.state.db.write() as session:
            row, created = finalize_return(session, record_id, data, fail)
            if not created: response.status_code = 200
            return output(session, row)
