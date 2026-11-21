"""One atomic positive return event in the existing physical inventory ledger."""
import hashlib,json
from sqlalchemy import select
from .db import DEMO_ACTOR_ID
from .issue_models import MaterialIssuePosting
from .receiving_models import StockPosting
from .return_models import SupplierReturnPosting
from .material_return_models import MaterialReturn,MaterialReturnLine,MaterialReturnPosting,MaterialReturnEntry
from .material_return_authorization import policy,verified_confirmation,CLOSED_MESSAGE


def post_return(session,record_id,data,fail,validate_snapshot):
    parent=session.get(MaterialReturn,str(record_id))
    if parent is None:fail(404,'退料單不存在，請重新讀取。')
    content=dict(domain='manufacturing-material-return',id=parent.id,row_version=data.row_version,
        work_order_version=data.work_order_version,confirmation_token_hash=
        hashlib.sha256(data.confirmation_token.encode()).hexdigest() if data.confirmation_token else None)
    digest=hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    by_key=session.scalar(select(MaterialReturnPosting).where(MaterialReturnPosting.idempotency_key==str(data.request_key)))
    by_doc=session.scalar(select(MaterialReturnPosting).where(MaterialReturnPosting.material_return_id==parent.id))
    for existing in [by_key,by_doc]:
        if existing:
            if (existing.material_return_id!=parent.id or existing.idempotency_key!=str(data.request_key)
                or existing.request_fingerprint!=digest or existing.posting_state!='POSTED'):
                fail(409,'退料過帳識別或原始確認內容不同，不能重複入庫；請重新讀取。')
            return parent,False
    if not policy(session)['posting_allowed']:fail(403,CLOSED_MESSAGE)
    if parent.row_version!=data.row_version or parent.work_order_version!=data.work_order_version:
        fail(409,'退料單或工單版次已變更，請重新讀取。')
    for model in [StockPosting,SupplierReturnPosting,MaterialIssuePosting]:
        if session.scalar(select(model.id).where(model.idempotency_key==str(data.request_key))):
            fail(409,'此過帳識別已用於其他庫存作業。')
    snapshot,_=validate_snapshot(session,parent,fail)
    with verified_confirmation(session,parent,data.confirmation_token,snapshot,fail) as grant:
        parent.row_version+=1;session.flush()
        posting=MaterialReturnPosting(material_return_id=parent.id,idempotency_key=str(data.request_key),
            request_fingerprint=digest,source_version=data.row_version,work_order_version=data.work_order_version,
            posting_state='PREPARING',authorization_basis=grant['basis'],confirmation_digest=grant['digest'],
            confirmation_snapshot=grant['snapshot'],confirmation_received_at=grant['received_at'],
            created_by=DEMO_ACTOR_ID,posted_by=DEMO_ACTOR_ID)
        session.add(posting);session.flush()
        from .issue_models import MaterialIssueLine
        for line in session.scalars(select(MaterialReturnLine).where(MaterialReturnLine.material_return_id==parent.id).order_by(MaterialReturnLine.line_no)):
            origin=session.get(MaterialIssueLine,line.origin_line_id)
            session.add(MaterialReturnEntry(posting_id=posting.id,return_line_id=line.id,position_id=origin.position_id,
                qty_base_delta=line.quantity,created_by=DEMO_ACTOR_ID))
        session.flush();posting.posting_state='POSTED';session.flush()
    return parent,True
