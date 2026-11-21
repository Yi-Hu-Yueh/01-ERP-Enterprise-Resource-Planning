"""Shared atomic engine with distinct QA and explicit local-demo confirmation."""
from contextlib import nullcontext
import hashlib
import json
from collections import defaultdict
from sqlalchemy import select
from .db import DEMO_ACTOR_ID
from .decimal_text import exact_sum
from .stock_ledger import compare,negate,on_hand
from .issue_models import MaterialIssue,MaterialIssueLine,MaterialIssuePosting,MaterialIssueEntry
from .receiving_models import StockPosting
from .return_models import SupplierReturnPosting
from .local_demo import runtime_for,verified_confirmation,DEMO_MESSAGE

POLICY_MESSAGE="製造工單尚未完成核准／發放政策設定，目前只能保存領料草稿，不能正式扣庫存。"


def policy(session):
    qa=session.connection().exec_driver_sql("SELECT erp_issue_test_authorized()").scalar()==1
    if qa:
        return dict(posting_allowed=True,authorization_basis="CONTROLLED_QA_ONLY",
            message="隔離 QA 測試授權；不是 Owner 生產核准。測試採明確正向領料、同基本單位及不得負庫存約定。")
    if runtime_for(session):
        return dict(posting_allowed=True,authorization_basis="LOCAL_DEMO_MANUAL",message=DEMO_MESSAGE)
    return dict(posting_allowed=False,authorization_basis=None,message=POLICY_MESSAGE)


def post_issue(session,record_id,data,fail,validate_header,validate_line):
    """Caller holds BEGIN IMMEDIATE. The only actual deduction implementation."""
    parent=session.get(MaterialIssue,str(record_id))
    if parent is None:
        fail(404,"領料單不存在，請重新讀取。")
    content=dict(domain="material-issue",id=parent.id,source_version=data.row_version,
        work_order_version=data.work_order_version)
    if data.confirmation_token is not None:
        content["confirmation_token_hash"]=hashlib.sha256(data.confirmation_token.encode()).hexdigest()
    digest=hashlib.sha256(json.dumps(content,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    by_key=session.scalar(select(MaterialIssuePosting).where(MaterialIssuePosting.idempotency_key==str(data.request_key)))
    by_document=session.scalar(select(MaterialIssuePosting).where(MaterialIssuePosting.material_issue_id==parent.id))
    for existing in [by_key,by_document]:
        if existing:
            if (existing.material_issue_id!=parent.id or existing.idempotency_key!=str(data.request_key)
                or existing.request_fingerprint!=digest or existing.posting_state!="POSTED"):
                fail(409,"過帳請求識別或原始確認內容不同，不能重複扣庫；請重新讀取。")
            # Reporting a committed transaction neither grants new authority nor
            # verifies an expired runtime token; the exact original hash must match.
            return parent,False
    authorization=policy(session)
    if not authorization["posting_allowed"]:
        fail(403,POLICY_MESSAGE)
    basis=authorization["authorization_basis"]
    if basis=="LOCAL_DEMO_MANUAL" and not data.confirmation_token:
        fail(403,"請先檢視並明確確認本次領料扣庫內容。")
    if basis=="CONTROLLED_QA_ONLY" and data.confirmation_token is not None:
        fail(422,"隔離 QA 授權與本機展示確認不可混用。")
    if parent.row_version!=data.row_version or parent.work_order_version!=data.work_order_version:
        fail(409,"領料單或工單資料版次已變更，請重新讀取。")
    for model in [StockPosting,SupplierReturnPosting]:
        if session.scalar(select(model.id).where(model.idempotency_key==str(data.request_key))):
            fail(409,"過帳請求識別已用於其他庫存單據。")
    validate_header(session,parent.work_order_id,parent.work_order_version,parent)
    lines=list(session.scalars(select(MaterialIssueLine).where(
        MaterialIssueLine.material_issue_id==parent.id).order_by(MaterialIssueLine.line_no)))
    if not lines:
        fail(422,"領料單沒有明細，不能過帳。")
    demands=defaultdict(list)
    for line in lines:
        validate_line(session,parent,line)
        if compare(line.quantity,"0")<=0:
            fail(422,"本次實際領料須為正向扣庫量；有號或零量仍可保存草稿，其他正式數值政策尚未核准。")
        demands[line.position_id].append(line.quantity)
    for position,quantities in demands.items():
        if compare(exact_sum(quantities),on_hand(session,position))>0:
            fail(422,"同倉同批的合計領料量超過實際庫存；沒有執行任何部分扣庫。")
    context=(verified_confirmation(session,parent,lines,data.confirmation_token,fail)
             if basis=="LOCAL_DEMO_MANUAL" else nullcontext(None))
    with context as grant:
        parent.row_version+=1
        session.flush()
        posting=MaterialIssuePosting(material_issue_id=parent.id,idempotency_key=str(data.request_key),
            request_fingerprint=digest,source_version=data.row_version,work_order_version=data.work_order_version,
            posting_state="PREPARING",authorization_basis=basis,
            confirmation_digest=grant["digest"] if grant else None,
            confirmation_snapshot=grant["snapshot"] if grant else None,
            confirmation_received_at=grant["received_at"] if grant else None,
            created_by=DEMO_ACTOR_ID,posted_by=DEMO_ACTOR_ID)
        session.add(posting);session.flush()
        for line in lines:
            session.add(MaterialIssueEntry(posting_id=posting.id,issue_line_id=line.id,
                position_id=line.position_id,qty_base_delta=negate(line.quantity),created_by=DEMO_ACTOR_ID))
        session.flush()
        posting.posting_state="POSTED"
        session.flush()
    return parent,True
