"""Same-position manufacturing returns, with immutable committed issue provenance."""
from collections import defaultdict
import hashlib,json
from uuid import UUID
from fastapi import Request,Response
from sqlalchemy import select,func,or_
from .db import DEMO_ACTOR_ID
from .models import Item,Unit,Warehouse
from .work_order_models import WorkOrder
from .work_orders import output as work_order_output
from .receiving import fields
from .receiving_models import StockPosition,InventoryLot,StockCreateReceipt
from .issue_models import MaterialIssue,MaterialIssueLine,MaterialIssuePosting,MaterialIssueEntry
from .material_issues import position_output
from .decimal_text import exact_sum,exact_difference
from .stock_ledger import compare,negate,on_hand
from .local_demo import canonical
from .material_return_models import MaterialReturn,MaterialReturnLine,MaterialReturnPosting,MaterialReturnEntry
from .material_return_authorization import policy,make_token
from .material_return_posting import post_return
from .material_return_schemas import (MaterialReturnCreate,MaterialReturnUpdate,MaterialReturnLineCreate,
    MaterialReturnLineUpdate,PostMaterialReturn,ReturnSource,MaterialReturnLineOut,MaterialReturnOut,MaterialReturnReview)
from .issue_schemas import IssuePolicy


def returned_quantity(session,origin_line_id):
    values=session.scalars(select(MaterialReturnEntry.qty_base_delta)
        .join(MaterialReturnLine,MaterialReturnLine.id==MaterialReturnEntry.return_line_id)
        .join(MaterialReturnPosting,MaterialReturnPosting.id==MaterialReturnEntry.posting_id)
        .join(MaterialIssueLine,MaterialIssueLine.id==MaterialReturnLine.origin_line_id)
        .where(MaterialReturnPosting.posting_state=='POSTED',MaterialReturnLine.origin_line_id==str(origin_line_id),
            MaterialReturnPosting.material_return_id==MaterialReturnLine.material_return_id,MaterialReturnEntry.leg_no==1,
            MaterialReturnEntry.position_id==MaterialIssueLine.position_id,MaterialReturnEntry.qty_base_delta==MaterialReturnLine.quantity,
            func.erp_decimal_cmp(MaterialReturnEntry.qty_base_delta,'0')>0))
    return exact_sum(values)


def source_origin(session,record_id,fail,parent=None,version=None):
    origin=session.get(MaterialIssueLine,str(record_id))
    if origin is None:fail(422,'來源領料明細不存在，請選取已過帳的實際領料來源。')
    issue=session.get(MaterialIssue,origin.material_issue_id)
    posted=session.scalar(select(MaterialIssuePosting).where(MaterialIssuePosting.material_issue_id==issue.id)) if issue else None
    if not posted or posted.posting_state!='POSTED':fail(422,'來源領料單尚未真正過帳，不能作為生產退料來源。')
    if version is not None and origin.row_version!=version:fail(409,'來源領料明細版次已變更，請重新讀取。')
    work=session.get(WorkOrder,issue.work_order_id)
    if not work or (parent and parent.work_order_id!=work.id):fail(422,'來源領料明細不屬於這張退料單的製造工單。')
    if issue.row_version!=posted.source_version+1 or issue.work_order_version!=posted.work_order_version:
        fail(409,'原領料過帳稽核版次不完整，請保留原紀錄並檢查。')
    issue_lines=list(session.scalars(select(MaterialIssueLine).where(MaterialIssueLine.material_issue_id==issue.id)))
    entries=list(session.scalars(select(MaterialIssueEntry).where(MaterialIssueEntry.posting_id==posted.id)))
    if len(entries)!=len(issue_lines):fail(409,'原領料過帳明細不完整，不能退料。')
    by_line={}
    for entry in entries:
        if entry.issue_line_id in by_line:fail(409,'原領料明細有重複庫存事件，不能退料。')
        by_line[entry.issue_line_id]=entry
    for row in issue_lines:
        entry=by_line.get(row.id)
        if (not entry or entry.position_id!=row.position_id or entry.leg_no!=1
            or entry.created_by!=posted.created_by or compare(entry.qty_base_delta,'0')>=0 or entry.qty_base_delta!=negate(row.quantity)):
            fail(409,'原領料與實際扣庫事件不一致，不能退料。')
    event=by_line.get(origin.id)
    item=session.get(Item,origin.material_id);position=session.get(StockPosition,origin.position_id)
    warehouse=session.get(Warehouse,origin.warehouse_id);unit=session.get(Unit,origin.unit_id)
    lot=session.get(InventoryLot,position.lot_id) if position and position.lot_id else None
    if (not item or item.item_kind!='RAW' or not position or not warehouse or not unit
        or (position.item_id,position.warehouse_id)!=(item.id,warehouse.id)
        or item.base_unit_id!=origin.unit_id or (position.lot_id and (not lot or lot.item_id!=item.id))):
        fail(409,'原領料原料、倉庫、批次、位置或基本單位不一致；不會自動替換。')
    issued=negate(event.qty_base_delta);returned=returned_quantity(session,origin.id)
    return dict(origin=origin,issue=issue,posting=posted,event=event,work_order=work,item=item,unit=unit,
        position=position,warehouse=warehouse,lot=lot,issued=issued,returned=returned,
        remaining=exact_difference(issued,returned))


def source_output(session,source):
    o=source['origin'];h=source['issue'];p=source['posting'];w=source['work_order']
    return ReturnSource(origin_line_id=o.id,origin_line_version=o.row_version,issue_id=h.id,issue_no=h.document_no,
        issue_line_no=o.line_no,issue_posting_id=p.id,issue_posted_at=p.posted_at,work_order_id=w.id,
        work_order_no=w.document_no,work_order_version=w.row_version,issued_qty_base=source['issued'],
        returned_qty_base=source['returned'],returnable_qty_base=source['remaining'],
        position=position_output(session,source['position']))


def validate_header(session,work_order_id,version,fail):
    work=session.get(WorkOrder,str(work_order_id))
    if work is None:fail(422,'製造工單不存在，請明確選取。')
    if work.row_version!=version:fail(409,'製造工單版次已更新，請重新讀取。')
    return work


def validate_snapshot(session,parent,fail):
    validate_header(session,parent.work_order_id,parent.work_order_version,fail)
    lines=list(session.scalars(select(MaterialReturnLine).where(MaterialReturnLine.material_return_id==parent.id).order_by(MaterialReturnLine.line_no)))
    if not lines:fail(422,'退料單沒有明細，不能檢視過帳。')
    origins={};positions={};by_origin=defaultdict(list);by_position=defaultdict(list);details=[]
    for line in lines:
        source=source_origin(session,line.origin_line_id,fail,parent,line.origin_line_version)
        if compare(line.quantity,'0')<=0:fail(422,'實際退料入庫須為正數；零或有號數量仍可保存草稿。')
        by_origin[line.origin_line_id].append(line.quantity);by_position[source['position'].id].append(line.quantity)
        if line.origin_line_id not in origins:
            origins[line.origin_line_id]={k:fields(v) for k,v in source.items() if k in {
                'origin','issue','posting','event','work_order','item','unit','position','warehouse','lot'} and v is not None}
            origins[line.origin_line_id].update(issued=source['issued'],returned=source['returned'],remaining=source['remaining'])
        positions[source['position'].id]=on_hand(session,source['position'].id)
        details.append((line,source))
    for origin,quantities in by_origin.items():
        if compare(exact_sum(quantities),origins[origin]['remaining'])>0:
            fail(422,'同一原領料明細的合計退料量超過剩餘可退量；沒有執行任何部分入庫。')
    movements=[dict(return_line_id=line.id,origin_line_id=line.origin_line_id,position_id=source['position'].id,
        qty_base_delta=line.quantity,unit_code=source['unit'].code,current_stock_qty_base=positions[source['position'].id],
        expected_stock_qty_base=exact_sum([positions[source['position'].id],*by_position[source['position'].id]]))
        for line,source in details]
    snapshot=json.loads(canonical(dict(material_return=fields(parent),lines=[fields(l) for l in lines],
        origins=origins,positions=positions,expected_movements=movements)))
    return snapshot,movements


def line_output(session,line,fail,posted=False):
    source=source_origin(session,line.origin_line_id,fail)
    proposed=exact_sum(session.scalars(select(MaterialReturnLine.quantity).where(
        MaterialReturnLine.material_return_id==line.material_return_id,MaterialReturnLine.origin_line_id==line.origin_line_id)))
    remaining=source['remaining'] if posted else exact_difference(source['remaining'],proposed)
    return MaterialReturnLineOut(**fields(line),source=source_output(session,source),proposed_qty_base=proposed,
        remaining_after_proposal_qty_base=remaining)


def output(session,row,fail):
    posting=session.scalar(select(MaterialReturnPosting).where(MaterialReturnPosting.material_return_id==row.id))
    lines=session.scalars(select(MaterialReturnLine).where(MaterialReturnLine.material_return_id==row.id).order_by(MaterialReturnLine.line_no))
    entries=session.scalars(select(MaterialReturnEntry).where(MaterialReturnEntry.posting_id==posting.id)) if posting else []
    return MaterialReturnOut(**fields(row),work_order=work_order_output(session,session.get(WorkOrder,row.work_order_id)),
        posting_state=posting.posting_state if posting else 'DRAFT',posting_id=posting.id if posting else None,
        posted_at=posting.posted_at if posting else None,posted_by=posting.posted_by if posting else None,
        posting_request_key=posting.idempotency_key if posting else None,posting_source_version=posting.source_version if posting else None,
        authorization_basis=posting.authorization_basis if posting else None,
        confirmation_digest=posting.confirmation_digest if posting else None,
        confirmation_received_at=posting.confirmation_received_at if posting else None,policy=policy(session),
        lines=[line_output(session,line,fail,bool(posting)) for line in lines],movements=[fields(e) for e in entries])


def install_material_return_routes(app,fail,references):
    prefix='/api/material-returns'
    def find(session,record_id):
        row=session.get(MaterialReturn,str(record_id))
        if row is None:fail(404,'退料單不存在，請重新讀取。')
        return row
    def draft(session,record_id,version):
        row=find(session,record_id)
        if session.scalar(select(MaterialReturnPosting.id).where(MaterialReturnPosting.material_return_id==row.id)):
            fail(409,'退料單已過帳，只能檢視，不能修改或刪除。')
        if row.row_version!=version:fail(409,'退料單版次已更新，請重新讀取。')
        return row
    def line_find(session,parent,line_id):
        row=session.get(MaterialReturnLine,str(line_id))
        if row is None or row.material_return_id!=parent.id:fail(404,'這張退料單沒有此筆明細。')
        return row
    def replay(session,kind,data,parent=None):
        digest=hashlib.sha256(canonical(dict(domain='manufacturing-material-return',kind=kind,parent=str(parent) if parent else None,
            data=data.model_dump(mode='json',exclude={'request_key','document_version'}))).encode()).hexdigest()
        receipt=session.get(StockCreateReceipt,str(data.request_key))
        if receipt:
            if receipt.resource_kind!=kind or receipt.payload_hash!=digest:fail(409,'新增請求識別已用於其他作業或不同內容。')
            row=session.get(MaterialReturn if kind=='header' else MaterialReturnLine,receipt.record_id)
            if row is None:fail(410,'原新增資料已刪除，不能以同一請求重建。')
            return digest,row
        return digest,None
    def remember(session,kind,data,row,digest):
        session.add(StockCreateReceipt(request_key=str(data.request_key),resource_kind=kind,record_id=row.id,
            payload_hash=digest,created_by=DEMO_ACTOR_ID));session.flush()

    @app.get(prefix+'/policy',response_model=IssuePolicy)
    def posting_policy(request:Request):
        with request.app.state.db.read() as session:return policy(session)
    @app.get(prefix+'/sources',response_model=list[ReturnSource])
    def sources(request:Request,work_order_id:UUID|None=None,q:str=''):
        query=select(MaterialIssueLine).join(MaterialIssue,MaterialIssue.id==MaterialIssueLine.material_issue_id).join(
            MaterialIssuePosting,MaterialIssuePosting.material_issue_id==MaterialIssue.id).where(MaterialIssuePosting.posting_state=='POSTED')
        if work_order_id:query=query.where(MaterialIssue.work_order_id==str(work_order_id))
        if q:query=query.join(WorkOrder,WorkOrder.id==MaterialIssue.work_order_id).where(or_(
            MaterialIssue.document_no.contains(q,autoescape=True),WorkOrder.document_no.contains(q,autoescape=True)))
        with request.app.state.db.read() as session:
            return [source_output(session,source_origin(session,line.id,fail)) for line in session.scalars(query.order_by(MaterialIssue.document_no,MaterialIssueLine.line_no))]
    @app.get(prefix,response_model=list[MaterialReturnOut])
    def listing(request:Request,q:str=''):
        query=select(MaterialReturn).join(WorkOrder,WorkOrder.id==MaterialReturn.work_order_id)
        if q:query=query.where(or_(*[c.contains(q,autoescape=True) for c in [MaterialReturn.document_no,MaterialReturn.note,WorkOrder.document_no]]))
        with request.app.state.db.read() as session:return [output(session,r,fail) for r in session.scalars(query.order_by(MaterialReturn.document_no))]
    @app.post(prefix,response_model=MaterialReturnOut,status_code=201)
    def create(data:MaterialReturnCreate,request:Request,response:Response):
        with request.app.state.db.write() as session:
            digest,existing=replay(session,'header',data)
            if existing:response.status_code=200;return output(session,existing,fail)
            validate_header(session,data.work_order_id,data.work_order_version,fail)
            row=MaterialReturn(**data.model_dump(mode='json',exclude={'request_key'}),created_by=DEMO_ACTOR_ID)
            session.add(row);session.flush();remember(session,'header',data,row,digest)
            return output(session,row,fail)
    @app.get(prefix+'/{record_id}',response_model=MaterialReturnOut)
    def read(record_id:UUID,request:Request):
        with request.app.state.db.read() as session:return output(session,find(session,record_id),fail)
    @app.put(prefix+'/{record_id}',response_model=MaterialReturnOut)
    def update(record_id:UUID,data:MaterialReturnUpdate,request:Request):
        with request.app.state.db.write() as session:
            row=draft(session,record_id,data.row_version)
            lines=list(session.scalars(select(MaterialReturnLine).where(MaterialReturnLine.material_return_id==row.id)))
            if lines and row.work_order_id!=str(data.work_order_id):fail(409,'已有退料明細，不能更換工單；請先明確刪除草稿明細。')
            validate_header(session,data.work_order_id,data.work_order_version,fail)
            for key,value in data.model_dump(mode='json',exclude={'row_version'}).items():setattr(row,key,value)
            for line in lines:source_origin(session,line.origin_line_id,fail,row,line.origin_line_version)
            row.row_version+=1;session.flush();return output(session,row,fail)
    @app.delete(prefix+'/{record_id}',status_code=204)
    def remove(record_id:UUID,row_version:int,request:Request):
        with request.app.state.db.write() as session:
            row=draft(session,record_id,row_version)
            if references(session,'material_return',row.id,exclude_tables={'material_return_line'}):fail(409,'退料單已被引用，不能刪除。')
            for line in session.scalars(select(MaterialReturnLine).where(MaterialReturnLine.material_return_id==row.id)):
                if references(session,'material_return_line',line.id):fail(409,'退料明細已被引用，不能刪除。')
                session.delete(line)
            session.flush();session.delete(row);session.flush()
        return Response(status_code=204)
    @app.get(prefix+'/{record_id}/lines',response_model=list[MaterialReturnLineOut])
    def lines(record_id:UUID,request:Request):
        with request.app.state.db.read() as session:return output(session,find(session,record_id),fail).lines
    @app.post(prefix+'/{record_id}/lines',response_model=MaterialReturnLineOut,status_code=201)
    def add_line(record_id:UUID,data:MaterialReturnLineCreate,request:Request,response:Response):
        with request.app.state.db.write() as session:
            digest,existing=replay(session,'line',data,record_id)
            if existing:
                response.status_code=200
                posted=session.scalar(select(MaterialReturnPosting.id).where(MaterialReturnPosting.material_return_id==existing.material_return_id))
                return line_output(session,existing,fail,bool(posted))
            parent=draft(session,record_id,data.document_version)
            validate_header(session,parent.work_order_id,parent.work_order_version,fail)
            source_origin(session,data.origin_line_id,fail,parent,data.origin_line_version)
            number=(session.scalar(select(func.max(MaterialReturnLine.line_no)).where(MaterialReturnLine.material_return_id==parent.id)) or 0)+1
            row=MaterialReturnLine(material_return_id=parent.id,line_no=number,created_by=DEMO_ACTOR_ID,
                **data.model_dump(mode='json',exclude={'request_key','document_version'}))
            session.add(row);parent.row_version+=1;session.flush();remember(session,'line',data,row,digest)
            return line_output(session,row,fail)
    @app.put(prefix+'/{record_id}/lines/{line_id}',response_model=MaterialReturnLineOut)
    def edit_line(record_id:UUID,line_id:UUID,data:MaterialReturnLineUpdate,request:Request):
        with request.app.state.db.write() as session:
            parent=draft(session,record_id,data.document_version);row=line_find(session,parent,line_id)
            if row.row_version!=data.row_version:fail(409,'退料明細版次已更新，請重新讀取。')
            validate_header(session,parent.work_order_id,parent.work_order_version,fail)
            source_origin(session,data.origin_line_id,fail,parent,data.origin_line_version)
            for key,value in data.model_dump(mode='json',exclude={'row_version','document_version'}).items():setattr(row,key,value)
            row.row_version+=1;parent.row_version+=1;session.flush();return line_output(session,row,fail)
    @app.delete(prefix+'/{record_id}/lines/{line_id}',status_code=204)
    def delete_line(record_id:UUID,line_id:UUID,row_version:int,document_version:int,request:Request):
        with request.app.state.db.write() as session:
            parent=draft(session,record_id,document_version);row=line_find(session,parent,line_id)
            if row.row_version!=row_version:fail(409,'退料明細版次已更新，請重新讀取。')
            if references(session,'material_return_line',row.id):fail(409,'退料明細已被引用，不能刪除。')
            session.delete(row);parent.row_version+=1;session.flush()
        return Response(status_code=204)
    @app.get(prefix+'/{record_id}/posting-review',response_model=MaterialReturnReview)
    def review(record_id:UUID,request:Request):
        with request.app.state.db.read() as session:
            parent=find(session,record_id)
            if session.scalar(select(MaterialReturnPosting.id).where(MaterialReturnPosting.material_return_id==parent.id)):
                fail(409,'退料單已過帳，請直接檢視永久紀錄。')
            snapshot,movements=validate_snapshot(session,parent,fail)
            return MaterialReturnReview(material_return=output(session,parent,fail),confirmation_token=make_token(session,snapshot),expected_movements=movements)
    @app.post(prefix+'/{record_id}/post',response_model=MaterialReturnOut,status_code=201)
    def post(record_id:UUID,data:PostMaterialReturn,request:Request,response:Response):
        with request.app.state.db.write() as session:
            row,created=post_return(session,record_id,data,fail,validate_snapshot)
            if not created:response.status_code=200
            return output(session,row,fail)
