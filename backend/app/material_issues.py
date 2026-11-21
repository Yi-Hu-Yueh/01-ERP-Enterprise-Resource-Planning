"""Draft manufacturing issues plus a policy-gated, shared-ledger posting endpoint."""
import hashlib
import json
from uuid import UUID
from fastapi import Request,Response
from sqlalchemy import func,or_,select
from .db import DEMO_ACTOR_ID
from .models import Item,Unit,Warehouse
from .bom_models import Bom,BomComponent
from .routing_models import Routing
from .work_order_models import WorkOrder
from .work_orders import output as work_order_output
from .receiving import fields
from .receiving_models import StockPosition,InventoryLot,StockEntry,StockPosting,StockDocument,StockDocumentLine,StockCreateReceipt
from .stock_ledger import on_hand
from .issue_models import MaterialIssue,MaterialIssueLine,MaterialIssuePosting,MaterialIssueEntry
from .issue_posting import policy,post_issue
from .issue_schemas import (IssueCreate,IssueUpdate,IssueLineCreate,IssueLineUpdate,PostIssue,
                           IssueOut,IssueLineOut,IssuePosition,IssuePolicy,IssueReview)


def position_output(session,position):
    item=session.get(Item,position.item_id);unit=session.get(Unit,item.base_unit_id)
    warehouse=session.get(Warehouse,position.warehouse_id)
    lot=session.get(InventoryLot,position.lot_id) if position.lot_id else None
    origins=session.execute(select(StockDocument.id,StockDocument.document_no,StockDocumentLine.id,StockEntry.qty_base_delta)
        .join(StockPosting,StockPosting.stock_document_id==StockDocument.id)
        .join(StockEntry,StockEntry.posting_id==StockPosting.id)
        .join(StockDocumentLine,StockDocumentLine.id==StockEntry.document_line_id)
        .where(StockPosting.posting_state=="POSTED",StockEntry.position_id==position.id)
        .order_by(StockDocument.document_no,StockDocumentLine.line_no)).all()
    return IssuePosition(id=position.id,material_id=item.id,material_code=item.code,material_name=item.name,
        warehouse_id=warehouse.id,warehouse_code=warehouse.code,warehouse_name=warehouse.name,
        lot_id=position.lot_id,lot_code=lot.lot_code if lot else None,
        unit_id=unit.id,unit_code=unit.code,unit_name=unit.name,physical_qty_base=on_hand(session,position.id),
        receipt_origins=[dict(receipt_id=o[0],receipt_no=o[1],receipt_line_id=o[2],quantity_base=o[3]) for o in origins])


def line_output(session,line):
    component=session.get(BomComponent,line.bom_component_id) if line.bom_component_id else None
    return IssueLineOut(**fields(line),position=position_output(session,session.get(StockPosition,line.position_id)),
        component_purpose=component.purpose_note if component else None)


def output(session,row):
    posting=session.scalar(select(MaterialIssuePosting).where(MaterialIssuePosting.material_issue_id==row.id))
    lines=session.scalars(select(MaterialIssueLine).where(MaterialIssueLine.material_issue_id==row.id).order_by(MaterialIssueLine.line_no))
    movements=session.scalars(select(MaterialIssueEntry).where(MaterialIssueEntry.posting_id==posting.id).order_by(MaterialIssueEntry.issue_line_id)) if posting else []
    return IssueOut(**fields(row),work_order=work_order_output(session,session.get(WorkOrder,row.work_order_id)),
        posting_state=posting.posting_state if posting else "DRAFT",posting_id=posting.id if posting else None,
        posted_at=posting.posted_at if posting else None,posted_by=posting.posted_by if posting else None,
        posting_request_key=posting.idempotency_key if posting else None,
        posting_source_version=posting.source_version if posting else None,
        authorization_basis=posting.authorization_basis if posting else None,
        confirmation_digest=posting.confirmation_digest if posting else None,
        confirmation_received_at=posting.confirmation_received_at if posting else None,policy=policy(session),
        lines=[line_output(session,l) for l in lines],movements=[fields(e) for e in movements])


def install_issue_routes(app,fail,references):
    prefix="/api/material-issues"

    def find(session,record_id):
        row=session.get(MaterialIssue,str(record_id))
        if row is None:
            fail(404,"領料單不存在，請重新讀取。")
        return row

    def draft(session,record_id,version):
        row=find(session,record_id)
        if session.scalar(select(MaterialIssuePosting.id).where(MaterialIssuePosting.material_issue_id==row.id)):
            fail(409,"領料單已有過帳紀錄，只能檢視，不能修改或刪除。")
        if row.row_version!=version:
            fail(409,"領料單已更新，請重新讀取後再操作。")
        return row

    def source(session,record_id,version,parent=None):
        wo=session.get(WorkOrder,str(record_id))
        if wo is None:
            fail(422,"製造工單不存在，請明確選取。")
        if wo.row_version!=version:
            fail(409,"來源工單資料版次已變更，請重新讀取。")
        bom=session.get(Bom,wo.bom_id) if wo.bom_id else None
        route=session.get(Routing,wo.routing_id) if wo.routing_id else None
        context=dict(work_order_id=wo.id,work_order_version=wo.row_version,bom_id=wo.bom_id,
            bom_version=bom.row_version if bom else None,routing_id=wo.routing_id,
            routing_version=route.row_version if route else None)
        if parent and any(getattr(parent,k)!=v for k,v in context.items()):
            fail(409,"領料單保存的工單或來源版次不一致，請保留原資料並檢查。")
        return context

    def validate_line(session,parent,data):
        item=session.get(Item,str(data.material_id))
        warehouse=session.get(Warehouse,str(data.warehouse_id))
        position=session.get(StockPosition,str(data.position_id))
        if item is None or item.item_kind!="RAW" or warehouse is None:
            fail(422,"必須選取既有原物料與倉庫。")
        if position is None or (position.item_id,position.warehouse_id)!=(item.id,warehouse.id):
            fail(422,"來源庫存批次／位置不屬於選取的原料與倉庫，請重新明確選取。")
        lot=session.get(InventoryLot,position.lot_id) if position.lot_id else None
        if position.lot_id and (lot is None or lot.item_id!=item.id):
            fail(422,"原料與庫存批次識別不一致。")
        if hasattr(data,"unit_id") and data.unit_id!=item.base_unit_id:
            fail(409,"領料基本單位已變更，沒有核准的換算規則。")
        if data.bom_component_id:
            component=session.get(BomComponent,str(data.bom_component_id))
            if component is None or (component.bom_id,component.material_id)!=(parent.bom_id,item.id):
                fail(422,"BOM 用料來源不屬於這張工單或原料；不會自動替換來源。")
            if component.row_version!=data.bom_component_version:
                fail(409,"BOM 用料資料版次已更新，請重新讀取。")
        return item.base_unit_id

    def line_find(session,parent,record_id):
        line=session.get(MaterialIssueLine,str(record_id))
        if line is None or line.material_issue_id!=parent.id:
            fail(404,"這張領料單沒有此筆明細。")
        return line

    def digest(kind,data,parent=None):
        return hashlib.sha256(json.dumps(dict(domain="material-issue",kind=kind,parent=str(parent) if parent else None,
            data=data.model_dump(mode="json",exclude={"request_key","document_version"})),sort_keys=True,
            ensure_ascii=False,separators=(",",":")).encode()).hexdigest()

    def replay(session,kind,data,parent=None):
        fingerprint=digest(kind,data,parent)
        receipt=session.get(StockCreateReceipt,str(data.request_key))
        if receipt:
            if receipt.resource_kind!=kind or receipt.payload_hash!=fingerprint:
                fail(409,"新增請求識別已用於其他作業或不同內容。")
            row=session.get(MaterialIssue if kind=="header" else MaterialIssueLine,receipt.record_id)
            if row is None:
                fail(410,"原新增資料已刪除，不能以原請求重建。")
            return fingerprint,row
        return fingerprint,None

    def remember(session,kind,data,row,fingerprint):
        session.add(StockCreateReceipt(request_key=str(data.request_key),resource_kind=kind,
            record_id=row.id,payload_hash=fingerprint,created_by=DEMO_ACTOR_ID));session.flush()

    @app.get(prefix+"/policy",response_model=IssuePolicy)
    def posting_policy(request:Request):
        with request.app.state.db.read() as session:
            return policy(session)

    @app.get(prefix+"/stock-options",response_model=list[IssuePosition])
    def stock_options(request:Request,material_id:UUID|None=None,warehouse_id:UUID|None=None):
        query=select(StockPosition).join(Item,Item.id==StockPosition.item_id).where(Item.item_kind=="RAW")
        if material_id:query=query.where(StockPosition.item_id==str(material_id))
        if warehouse_id:query=query.where(StockPosition.warehouse_id==str(warehouse_id))
        with request.app.state.db.read() as session:
            return [position_output(session,p) for p in session.scalars(query.order_by(StockPosition.id))]

    @app.get(prefix,response_model=list[IssueOut])
    def listing(request:Request,q:str=""):
        query=select(MaterialIssue).join(WorkOrder,WorkOrder.id==MaterialIssue.work_order_id).join(Item,Item.id==WorkOrder.product_id)
        if q:
            query=query.where(or_(*[c.contains(q,autoescape=True) for c in [
                MaterialIssue.document_no,MaterialIssue.note,WorkOrder.document_no,Item.code,Item.name]]))
        with request.app.state.db.read() as session:
            return [output(session,row) for row in session.scalars(query.order_by(MaterialIssue.document_no))]

    @app.post(prefix,response_model=IssueOut,status_code=201)
    def create(data:IssueCreate,request:Request,response:Response):
        with request.app.state.db.write() as session:
            fingerprint,existing=replay(session,"header",data)
            if existing:response.status_code=200;return output(session,existing)
            context=source(session,data.work_order_id,data.work_order_version)
            row=MaterialIssue(document_no=data.document_no,business_date=data.business_date,note=data.note,
                **context,created_by=DEMO_ACTOR_ID)
            session.add(row);session.flush();remember(session,"header",data,row,fingerprint)
            return output(session,row)

    @app.get(prefix+"/{record_id}",response_model=IssueOut)
    def read(record_id:UUID,request:Request):
        with request.app.state.db.read() as session:return output(session,find(session,record_id))

    @app.put(prefix+"/{record_id}",response_model=IssueOut)
    def update(record_id:UUID,data:IssueUpdate,request:Request):
        with request.app.state.db.write() as session:
            row=draft(session,record_id,data.row_version)
            lines=list(session.scalars(select(MaterialIssueLine).where(MaterialIssueLine.material_issue_id==row.id)))
            if row.work_order_id!=str(data.work_order_id) and lines:
                fail(409,"已有領料明細，不能改派工單；請先明確刪除草稿明細或另建領料單。")
            context=source(session,data.work_order_id,data.work_order_version)
            for key,value in context.items():setattr(row,key,value)
            for line in lines:validate_line(session,row,line)
            row.document_no=data.document_no;row.business_date=data.business_date;row.note=data.note;row.row_version+=1
            session.flush();return output(session,row)

    @app.delete(prefix+"/{record_id}",status_code=204)
    def remove(record_id:UUID,row_version:int,request:Request):
        with request.app.state.db.write() as session:
            row=draft(session,record_id,row_version)
            if references(session,"material_issue",row.id,exclude_tables={"material_issue_line"}):
                fail(409,"領料單已被引用，不能刪除。")
            for line in session.scalars(select(MaterialIssueLine).where(MaterialIssueLine.material_issue_id==row.id)):
                if references(session,"material_issue_line",line.id):fail(409,"領料明細已被引用，不能刪除。")
                session.delete(line)
            session.flush();session.delete(row);session.flush()
        return Response(status_code=204)

    @app.get(prefix+"/{record_id}/lines",response_model=list[IssueLineOut])
    def lines(record_id:UUID,request:Request):
        with request.app.state.db.read() as session:return output(session,find(session,record_id)).lines

    @app.post(prefix+"/{record_id}/lines",response_model=IssueLineOut,status_code=201)
    def add_line(record_id:UUID,data:IssueLineCreate,request:Request,response:Response):
        with request.app.state.db.write() as session:
            fingerprint,existing=replay(session,"line",data,record_id)
            if existing:response.status_code=200;return line_output(session,existing)
            parent=draft(session,record_id,data.document_version)
            source(session,parent.work_order_id,parent.work_order_version,parent)
            unit=validate_line(session,parent,data)
            number=(session.scalar(select(func.max(MaterialIssueLine.line_no)).where(MaterialIssueLine.material_issue_id==parent.id)) or 0)+1
            row=MaterialIssueLine(material_issue_id=parent.id,line_no=number,unit_id=unit,created_by=DEMO_ACTOR_ID,
                **data.model_dump(mode="json",exclude={"request_key","document_version"}))
            session.add(row);parent.row_version+=1;session.flush();remember(session,"line",data,row,fingerprint)
            return line_output(session,row)

    @app.put(prefix+"/{record_id}/lines/{line_id}",response_model=IssueLineOut)
    def edit_line(record_id:UUID,line_id:UUID,data:IssueLineUpdate,request:Request):
        with request.app.state.db.write() as session:
            parent=draft(session,record_id,data.document_version);row=line_find(session,parent,line_id)
            if row.row_version!=data.row_version:fail(409,"領料明細已更新，請重新讀取。")
            source(session,parent.work_order_id,parent.work_order_version,parent)
            unit=validate_line(session,parent,data)
            for key,value in data.model_dump(mode="json",exclude={"row_version","document_version"}).items():setattr(row,key,value)
            row.unit_id=unit;row.row_version+=1;parent.row_version+=1
            session.flush();return line_output(session,row)

    @app.delete(prefix+"/{record_id}/lines/{line_id}",status_code=204)
    def delete_line(record_id:UUID,line_id:UUID,row_version:int,document_version:int,request:Request):
        with request.app.state.db.write() as session:
            parent=draft(session,record_id,document_version);line=line_find(session,parent,line_id)
            if line.row_version!=row_version:fail(409,"領料明細已更新，請重新讀取。")
            if references(session,"material_issue_line",line.id):fail(409,"領料明細已被引用，不能刪除。")
            session.delete(line);parent.row_version+=1;session.flush()
        return Response(status_code=204)

    @app.get(prefix+"/{record_id}/posting-review",response_model=IssueReview)
    def posting_review(record_id:UUID,request:Request):
        from .local_demo import make_review
        with request.app.state.db.read() as session:
            parent=find(session,record_id)
            token,movements=make_review(session,parent,fail,source,validate_line)
            return IssueReview(issue=output(session,parent),confirmation_token=token,expected_movements=movements)

    @app.post(prefix+"/{record_id}/post",response_model=IssueOut,status_code=201)
    def post(record_id:UUID,data:PostIssue,request:Request,response:Response):
        with request.app.state.db.write() as session:
            parent,created=post_issue(session,record_id,data,fail,source,validate_line)
            if not created:response.status_code=200
            return output(session,parent)
