"""One real sales line, one existing FG position, one atomic negative movement."""
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
from .sales_shipment_schemas import ShipmentCreate, ShipmentUpdate, PostShipment
from .sales_shipment_authorization import policy, make_token, verified_confirmation
from .decimal_text import exact_difference, exact_sum
from .stock_ledger import compare, negate, on_hand
from .local_demo import canonical

INPUT_SYSTEM_FIELDS={"request_key","row_version"}


def fields(row):
    return {c.name:getattr(row,c.name) for c in row.__table__.columns} if row else None


def shipped_quantity(session,line_id):
    """Count authentic posted movements without depending on mutable source versions."""
    return exact_sum(session.connection().exec_driver_sql("""
        SELECT r.quantity FROM sales_shipment r
        JOIN sales_shipment_posting p ON p.shipment_id=r.id AND p.posting_state='POSTED'
        JOIN sales_shipment_entry e ON e.shipment_id=r.id AND e.posting_id=p.id
        JOIN stock_position s ON s.id=e.position_id
        JOIN sales_order_line l ON l.id=r.sales_order_line_id
        JOIN sales_order o ON o.id=l.sales_order_id
        WHERE l.id=? AND l.sales_order_id=r.sales_order_id AND o.customer_id=r.customer_id
          AND l.item_id=r.product_id AND l.unit_id=r.unit_id
          AND s.id=r.position_id AND s.item_id=r.product_id AND s.warehouse_id=r.warehouse_id
          AND s.lot_id IS r.lot_id AND e.leg_no=1 AND e.qty_base_delta=erp_decimal_neg(r.quantity)
          AND erp_decimal_cmp(r.quantity,'0')>0 AND r.row_version=p.source_version+1
          AND e.created_by=p.created_by
        """,(str(line_id),)).scalars())


def line_source(session,line_id,fail,line_version=None,order_version=None):
    line=session.get(SalesLine,str(line_id))
    if line is None:fail(422,"來源銷售訂單明細不存在，請重新選擇真實明細。")
    order=session.get(SalesOrder,line.sales_order_id)
    product=session.get(Item,line.item_id)
    if (not order or not session.get(Customer,order.customer_id) or not product
        or product.item_kind!="FINISHED" or product.base_unit_id!=line.unit_id):
        fail(409,"來源訂單、客戶、成品或基本單位不一致，沒有出貨。")
    if ((line_version is not None and line.row_version!=line_version)
        or (order_version is not None and order.row_version!=order_version)):
        fail(409,"來源銷售訂單或明細版次已變更，請重新讀取並選取。")
    return line,order


def source_output(session,line):
    order=session.get(SalesOrder,line.sales_order_id)
    customer=session.get(Customer,order.customer_id)
    product=session.get(Item,line.item_id);unit=session.get(Unit,line.unit_id)
    shipped=shipped_quantity(session,line.id)
    return dict(source_line_id=line.id,line_no=line.line_no,line_version=line.row_version,
        order_version=order.row_version,sales_order_id=order.id,sales_order_no=order.document_no,
        customer_id=customer.id,customer_code=customer.code,customer_name=customer.name,
        product_id=product.id,product_code=product.code,product_name=product.name,
        unit_id=unit.id,unit_code=unit.code,unit_name=unit.name,ordered_qty_base=line.ordered_qty,
        shipped_qty_base=shipped,remaining_qty_base=exact_difference(line.ordered_qty,shipped),
        lot_tracking=product.lot_tracking)


def position_for(session,position_id,line,fail):
    position=session.get(StockPosition,str(position_id));product=session.get(Item,line.item_id)
    if not position or position.item_id!=line.item_id or not session.get(Warehouse,position.warehouse_id):
        fail(422,"必須選擇來源成品的真實既有倉庫與庫存位置，不能使用原料或其他成品。")
    lot=session.get(InventoryLot,position.lot_id) if position.lot_id else None
    if product.lot_tracking:
        if (not lot or lot.item_id!=line.item_id or lot.origin_kind!="MANUFACTURED"
            or lot.supplier_id is not None or lot.supplier_lot_code is not None):
            fail(422,"追批成品必須選擇此倉庫內真實既有的成品製造批號。")
    elif position.lot_id is not None:
        fail(422,"未追批成品僅能使用其真實無批號庫存位置，不會建立虛構批號。")
    return position,lot


def position_output(session,position):
    warehouse=session.get(Warehouse,position.warehouse_id)
    lot=session.get(InventoryLot,position.lot_id) if position.lot_id else None
    return dict(id=position.id,item_id=position.item_id,warehouse_id=warehouse.id,
        warehouse_code=warehouse.code,warehouse_name=warehouse.name,lot_id=position.lot_id,
        lot_code=lot.lot_code if lot else None,on_hand_qty_base=on_hand(session,position.id))


def derived_values(session,data,fail):
    line,order=line_source(session,data.sales_order_line_id,fail,data.line_version,data.order_version)
    position,_=position_for(session,data.position_id,line,fail)
    return dict(sales_order_id=order.id,customer_id=order.customer_id,product_id=line.item_id,
        unit_id=line.unit_id,warehouse_id=position.warehouse_id,lot_id=position.lot_id)


def output(session,row,fail):
    line=session.get(SalesLine,row.sales_order_line_id)
    warehouse=session.get(Warehouse,row.warehouse_id)
    lot=session.get(InventoryLot,row.lot_id) if row.lot_id else None
    posting=session.scalar(select(SalesShipmentPosting).where(SalesShipmentPosting.shipment_id==row.id))
    movements=[]
    if posting and posting.posting_state=="POSTED":
        for entry in session.scalars(select(SalesShipmentEntry).where(SalesShipmentEntry.posting_id==posting.id)):
            p=session.get(StockPosition,entry.position_id)
            movements.append(dict(**fields(entry),item_id=p.item_id,warehouse_id=p.warehouse_id,
                lot_id=p.lot_id,lot_code=lot.lot_code if lot else None))
    return dict(**fields(row),source=source_output(session,line),warehouse_code=warehouse.code,
        warehouse_name=warehouse.name,lot_code=lot.lot_code if lot else None,
        posting_state="DRAFT" if posting is None else posting.posting_state,
        posting_id=posting.id if posting else None,posted_at=posting.posted_at if posting else None,
        authorization_basis=posting.authorization_basis if posting else None,
        current_stock_qty_base=on_hand(session,row.position_id),movements=movements)


def validate_snapshot(session,row,fail):
    line,order=line_source(session,row.sales_order_line_id,fail,row.line_version,row.order_version)
    if (row.sales_order_id!=order.id or row.customer_id!=order.customer_id
        or row.product_id!=line.item_id or row.unit_id!=line.unit_id):
        fail(409,"出貨草稿與真實來源客戶、訂單或成品單位不一致，請重新選取來源。")
    position,lot=position_for(session,row.position_id,line,fail)
    if row.warehouse_id!=position.warehouse_id or row.lot_id!=position.lot_id:
        fail(409,"出貨倉庫或批號與真實庫存位置不一致。")
    if compare(row.quantity,"0")<=0:
        fail(422,"實際出貨數量必須大於零；零或有號量只能保存草稿，不能當作退貨。")
    shipped=shipped_quantity(session,line.id);remaining=exact_difference(line.ordered_qty,shipped)
    if compare(row.quantity,remaining)>0:fail(409,"本次出貨量超過此筆訂單明細的剩餘未出貨量，沒有扣庫。")
    current=on_hand(session,position.id)
    if compare(row.quantity,current)>0:fail(409,"所選倉庫與批號的實際庫存不足，沒有出貨或扣庫。")
    snapshot=json.loads(canonical(dict(shipment=fields(row),sales_line=fields(line),sales_order=fields(order),
        customer=fields(session.get(Customer,order.customer_id)),product=fields(session.get(Item,line.item_id)),
        unit=fields(session.get(Unit,line.unit_id)),warehouse=fields(session.get(Warehouse,position.warehouse_id)),
        lot=fields(lot),position=fields(position),shipped_qty_base=shipped,
        remaining_qty_base=remaining,current_stock_qty_base=current)))
    return snapshot


def post_shipment(session,record_id,data,fail):
    row=session.get(SalesShipment,str(record_id))
    if row is None:fail(404,"銷售出貨單不存在，請重新讀取。")
    digest=hashlib.sha256(canonical(dict(domain="sales-shipment",id=row.id,row_version=data.row_version,
        confirmation_token_hash=hashlib.sha256(data.confirmation_token.encode()).hexdigest())).encode()).hexdigest()
    by_key=session.scalar(select(SalesShipmentPosting).where(SalesShipmentPosting.idempotency_key==str(data.request_key)))
    by_doc=session.scalar(select(SalesShipmentPosting).where(SalesShipmentPosting.shipment_id==row.id))
    for previous in [by_key,by_doc]:
        if previous:
            if (previous.shipment_id!=row.id or previous.idempotency_key!=str(data.request_key)
                or previous.request_fingerprint!=digest or previous.posting_state!="POSTED"):
                fail(409,"出貨過帳識別或原始確認內容不同，不能重複扣庫；請重新讀取。")
            return row,False
    if not policy(session)["posting_allowed"]:fail(403,"目前未啟用本機手動銷售出貨過帳，請使用專用 localhost 展示服務。")
    if row.row_version!=data.row_version:fail(409,"出貨草稿已更新，請重新讀取後檢視。")
    for model in [StockPosting,SupplierReturnPosting,MaterialIssuePosting,MaterialReturnPosting,FinishedGoodsPosting]:
        if session.scalar(select(model.id).where(model.idempotency_key==str(data.request_key))):
            fail(409,"此過帳識別已用於其他庫存作業，不能移用出貨。")
    snapshot=validate_snapshot(session,row,fail)
    with verified_confirmation(session,row,data.confirmation_token,snapshot,fail) as grant:
        row.row_version+=1;session.flush()
        posting=SalesShipmentPosting(shipment_id=row.id,idempotency_key=str(data.request_key),
            request_fingerprint=digest,source_version=data.row_version,posting_state="PREPARING",
            authorization_basis=grant["basis"],confirmation_digest=grant["digest"],
            confirmation_snapshot=grant["snapshot"],confirmation_received_at=grant["received_at"],
            posted_at=grant["received_at"],created_by=DEMO_ACTOR_ID,posted_by=DEMO_ACTOR_ID)
        session.add(posting);session.flush()
        session.add(SalesShipmentEntry(posting_id=posting.id,shipment_id=row.id,position_id=row.position_id,
            qty_base_delta=negate(row.quantity),created_by=DEMO_ACTOR_ID));session.flush()
        posting.posting_state="POSTED";session.flush()
    return row,True


def protect_source_line_edit(session,row,data,fail):
    # Draft shipment snapshots may go stale; only actual POSTED lineage freezes identity.
    posted=session.scalar(select(SalesShipmentPosting.id).join(SalesShipment,
        SalesShipment.id==SalesShipmentPosting.shipment_id).where(
        SalesShipment.sales_order_line_id==row.id,SalesShipmentPosting.posting_state=="POSTED"))
    if not posted:return
    if row.item_id!=str(data.item_id) or row.unit_id!=str(data.unit_id):
        fail(409,"此明細已有實際出貨，不能改掛成品或基本單位。")
    if compare(data.ordered_qty,shipped_quantity(session,row.id))<0:
        fail(409,"訂購數量不得低於此明細真正已過帳的累計出貨量。")


def protect_source_header_edit(session,row,data,fail):
    if row.customer_id==str(data.customer_id):return
    posted=session.scalar(select(SalesShipmentPosting.id).join(SalesShipment,
        SalesShipment.id==SalesShipmentPosting.shipment_id).where(
        SalesShipment.sales_order_id==row.id,SalesShipmentPosting.posting_state=="POSTED"))
    if posted:fail(409,"此訂單已有實際出貨，不能改掛原客戶。")


def install_sales_shipment_routes(app,fail,references):
    prefix="/api/sales-shipments"
    def find(session,record_id,version=None):
        row=session.get(SalesShipment,str(record_id))
        if row is None:fail(404,"銷售出貨單不存在，請重新讀取。")
        if version is not None:
            if row.row_version!=version:fail(409,"銷售出貨單已更新，請重新讀取。")
            if references(session,"sales_shipment",row.id):fail(409,"銷售出貨單已過帳或被引用，無法修改或刪除。")
        return row
    def validate(session,data,record_id=None):
        values=derived_values(session,data,fail)
        query=select(SalesShipment.id).where(SalesShipment.document_no==data.document_no)
        if record_id:query=query.where(SalesShipment.id!=record_id)
        if session.scalar(query):fail(409,"銷售出貨單號已存在，請使用不同單號。")
        return values
    @app.get(prefix+"/policy")
    def read_policy(request:Request):
        with request.app.state.db.read() as session:return policy(session)
    @app.get(prefix+"/sources")
    def sources(request:Request,q:str="",sales_order_line_id:UUID|None=None):
        query=select(SalesLine).join(SalesOrder,SalesOrder.id==SalesLine.sales_order_id).join(
            Customer,Customer.id==SalesOrder.customer_id).join(Item,Item.id==SalesLine.item_id).where(Item.item_kind=="FINISHED")
        if sales_order_line_id:query=query.where(SalesLine.id==str(sales_order_line_id))
        if q:query=query.where(or_(*(c.contains(q,autoescape=True) for c in [SalesOrder.document_no,Customer.code,Customer.name,Item.code,Item.name])))
        with request.app.state.db.read() as session:
            return [source_output(session,l) for l in session.scalars(query.order_by(SalesOrder.document_no,SalesLine.line_no))]
    @app.get(prefix+"/positions")
    def positions(sales_order_line_id:UUID,request:Request):
        with request.app.state.db.read() as session:
            line,_=line_source(session,sales_order_line_id,fail)
            rows=session.scalars(select(StockPosition).where(StockPosition.item_id==line.item_id).order_by(StockPosition.warehouse_id,StockPosition.id))
            return [position_output(session,p) for p in rows]
    @app.get(prefix)
    def list_shipments(request:Request,q:str=""):
        query=select(SalesShipment).join(SalesOrder,SalesOrder.id==SalesShipment.sales_order_id).join(
            Customer,Customer.id==SalesShipment.customer_id).join(Item,Item.id==SalesShipment.product_id).outerjoin(
            InventoryLot,InventoryLot.id==SalesShipment.lot_id)
        if q:query=query.where(or_(*(c.contains(q,autoescape=True) for c in [SalesShipment.document_no,SalesShipment.note,
            SalesOrder.document_no,Customer.code,Customer.name,Item.code,Item.name,InventoryLot.lot_code])))
        with request.app.state.db.read() as session:return [output(session,r,fail) for r in session.scalars(query.order_by(SalesShipment.document_no))]
    @app.get(prefix+"/{record_id}")
    def read_shipment(record_id:UUID,request:Request):
        with request.app.state.db.read() as session:return output(session,find(session,record_id),fail)
    @app.post(prefix,status_code=201)
    def create_shipment(data:ShipmentCreate,request:Request,response:Response):
        with request.app.state.db.write() as session:
            digest=hashlib.sha256(canonical(dict(domain="sales-shipment-create",data=data.model_dump(mode="json",exclude={"request_key"}))).encode()).hexdigest()
            receipt=session.get(StockCreateReceipt,str(data.request_key))
            if receipt:
                if receipt.resource_kind!="header" or receipt.payload_hash!=digest:fail(409,"新增請求識別已用於不同內容。")
                row=session.get(SalesShipment,receipt.record_id)
                if row is None:fail(410,"原出貨草稿已刪除，請開始新的新增操作。")
                response.status_code=200;return output(session,row,fail)
            values=validate(session,data)
            row=SalesShipment(**data.model_dump(mode="json",exclude=INPUT_SYSTEM_FIELDS),**values,created_by=DEMO_ACTOR_ID)
            session.add(row);session.flush()
            session.add(StockCreateReceipt(request_key=str(data.request_key),resource_kind="header",record_id=row.id,payload_hash=digest,created_by=DEMO_ACTOR_ID));session.flush()
            return output(session,row,fail)
    @app.put(prefix+"/{record_id}")
    def update_shipment(record_id:UUID,data:ShipmentUpdate,request:Request):
        with request.app.state.db.write() as session:
            row=find(session,record_id,data.row_version);values=validate(session,data,row.id)
            for k,v in {**data.model_dump(mode="json",exclude=INPUT_SYSTEM_FIELDS),**values}.items():setattr(row,k,v)
            row.row_version+=1;session.flush();return output(session,row,fail)
    @app.delete(prefix+"/{record_id}",status_code=204)
    def delete_shipment(record_id:UUID,row_version:int,request:Request):
        with request.app.state.db.write() as session:session.delete(find(session,record_id,row_version));session.flush()
    @app.get(prefix+"/{record_id}/posting-review")
    def review(record_id:UUID,request:Request):
        with request.app.state.db.read() as session:
            row=find(session,record_id)
            if session.scalar(select(SalesShipmentPosting.id).where(SalesShipmentPosting.shipment_id==row.id)):fail(409,"出貨已過帳，只能檢視原始庫存異動。")
            snapshot=validate_snapshot(session,row,fail)
            return dict(shipment=output(session,row,fail),source=source_output(session,session.get(SalesLine,row.sales_order_line_id)),
                current_stock_qty_base=snapshot["current_stock_qty_base"],
                expected_stock_qty_base=exact_difference(snapshot["current_stock_qty_base"],row.quantity),
                remaining_after_qty_base=exact_difference(snapshot["remaining_qty_base"],row.quantity),
                confirmation_token=make_token(session,snapshot),policy=policy(session))
    @app.post(prefix+"/{record_id}/post",status_code=201)
    def post(record_id:UUID,data:PostShipment,request:Request,response:Response):
        with request.app.state.db.write() as session:
            row,created=post_shipment(session,record_id,data,fail)
            if not created:response.status_code=200
            return output(session,row,fail)
