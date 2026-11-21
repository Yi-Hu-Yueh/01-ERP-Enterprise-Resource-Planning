"""Real outbound issue invariants; all generated records use isolated databases."""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
import sqlite3
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event,select
from sqlalchemy.exc import IntegrityError
from backend.app.db import ROOT,DEMO_ACTOR_ID,make_engine,migrate
from backend.app.main import create_app
from backend.app.issue_models import MaterialIssue,MaterialIssueLine,MaterialIssuePosting,MaterialIssueEntry
from backend.app.models import Item
from backend.app.issue_posting import POLICY_MESSAGE
from backend.tests.issue_qa_runtime import new_qa_application
from backend.tests.test_foundation import setup
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns,header as return_header,line as return_line,post as return_post
from backend.tests.test_outstanding import inquiry
from backend.tests.test_mrp import seed,preview,material
from backend.tests.test_work_orders import create as create_work
from backend.tests.test_supplier_offers import snapshot
from backend.tests.test_receiving import header as receipt_header,line as receipt_line,post as receipt_post

URL="/api/material-issues"


@pytest.fixture
def issues(inquiry):
    return seed(inquiry)


@pytest.fixture
def qa(tmp_path):
    app,path=new_qa_application(tmp_path)
    with TestClient(app) as c:
        r=po.__wrapped__((c,app.state.db,path))
        yield seed(inquiry.__wrapped__(returns.__wrapped__(r)))


def current(r,h):
    response=r["client"].get(URL+"/"+h["id"])
    assert response.status_code==200,response.text
    return response.json()


def header_data(r,number="MI-001",order=0,**patch):
    wo=r["orders"][order]
    return {**dict(request_key=str(uuid4()),document_no=number,work_order_id=wo["id"],
                work_order_version=wo["row_version"],business_date="2026-11-20",note="明確領料草稿"),**patch}


def header(r,number="MI-001",order=0,**patch):
    response=r["client"].post(URL,json=header_data(r,number,order,**patch))
    assert response.status_code==201,response.text
    return response.json()


def header_edit(h,**patch):
    return {**{k:h[k] for k in ["document_no","work_order_id","work_order_version","business_date","note","row_version"]},**patch}


def position(r):
    return next(p for p in r["client"].get(URL+"/stock-options").json() if p["lot_code"]=="LOT-R8-001" and p["warehouse_id"]==r["warehouse"]["id"])


def line_data(r,h,quantity="1.25",**patch):
    p=position(r)
    return {**dict(request_key=str(uuid4()),document_version=current(r,h)["row_version"],
        material_id=p["material_id"],warehouse_id=p["warehouse_id"],position_id=p["id"],
        bom_component_id=None,bom_component_version=None,quantity=quantity,note="實際請領量"),**patch}


def add(r,h,quantity="1.25",**patch):
    response=r["client"].post(URL+"/"+h["id"]+"/lines",json=line_data(r,h,quantity,**patch))
    assert response.status_code==201,response.text
    return response.json()


def line_edit(r,h,l,**patch):
    return {**{k:l[k] for k in ["material_id","warehouse_id","position_id","bom_component_id",
        "bom_component_version","quantity","note","row_version"]},"document_version":current(r,h)["row_version"],**patch}


def post_data(r,h,**patch):
    h=current(r,h)
    return {**dict(request_key=str(uuid4()),row_version=h["row_version"],work_order_version=h["work_order_version"]),**patch}


def post(r,h,body=None):
    return r["client"].post(URL+"/"+h["id"]+"/post",json=body or post_data(r,h))


def balance(r):
    return next(p["on_hand_qty_base"] for p in r["client"].get("/api/stock-positions").json()
                if p["item_id"]==r["raws"][0]["id"] and p["warehouse_id"]==r["warehouse"]["id"] and p["lot_code"]=="LOT-R8-001")


def test_draft_header_lines_crud_search_reopen_audit_retry_and_no_stock(issues):
    r=issues;c=r["client"];before=snapshot(r["path"]);original_mrp=preview(r);original_inquiry=c.get("/api/purchase-outstanding").json()
    data=header_data(r);response=c.post(URL,json=data);assert response.status_code==201
    h=response.json();assert h["id"]!=h["document_no"] and h["work_order"]["document_no"]=="WO-TEST-001"
    assert h["posting_state"]=="DRAFT" and h["posting_id"] is None and not h["policy"]["posting_allowed"]
    assert c.post(URL,json=data).status_code==200
    assert c.post(URL,json={**data,"note":"conflicting retry"}).status_code==409
    assert c.post(URL,json=header_data(r)).status_code==409
    updated=c.put(URL+"/"+h["id"],json=header_edit(h,note="更新草稿",document_no="MI-CHANGED"))
    assert updated.status_code==200;h=updated.json();assert h["row_version"]==2
    assert h["created_by"]==DEMO_ACTOR_ID and h["created_at"]==response.json()["created_at"]
    a=add(r,h);b=add(r,h,"0.25");h=current(r,h)
    assert len(h["lines"])==2 and balance(r)=="4"
    changed=c.put(URL+"/"+h["id"]+"/lines/"+a["id"],json=line_edit(r,h,a,quantity="2.5"))
    assert changed.status_code==200;line=changed.json()
    assert line["quantity"]=="2.5" and line["row_version"]==2 and line["created_at"]==a["created_at"]
    assert c.get(URL,params={"q":"WO-TEST-001"}).json()[0]["id"]==h["id"]
    assert c.get(URL,params={"q":"%"}).json()==[]
    assert len(c.get(URL+"/"+h["id"]+"/lines").json())==2
    assert c.delete(URL+"/"+h["id"]+"/lines/"+b["id"],params={"row_version":b["row_version"],"document_version":current(r,h)["row_version"]}).status_code==204
    assert c.delete(URL+"/"+h["id"],params={"row_version":current(r,h)["row_version"]}).status_code==204
    assert c.post(URL,json=data).status_code==410
    assert balance(r)=="4" and preview(r)["materials"]==original_mrp["materials"]
    assert c.get("/api/purchase-outstanding").json()==original_inquiry
    after=snapshot(r["path"])
    assert all(after["rows"][t]==rows for t,rows in before["rows"].items() if t!="stock_create_receipt")


@pytest.mark.parametrize("quantity",["-1.25","0","0."+"0"*100+"12345678901234567890123456789","9"*5000])
def test_signed_zero_high_precision_drafts_are_preserved_not_posted(issues,quantity):
    r=issues;h=header(r);line=add(r,h,quantity)
    assert line["quantity"]==quantity
    before=snapshot(r["path"]);response=post(r,h)
    assert response.status_code==403 and POLICY_MESSAGE in response.json()["message"]
    assert snapshot(r["path"])==before and balance(r)=="4"


@pytest.mark.parametrize("patch",[
    {"material_id":str(uuid4())},{"warehouse_id":str(uuid4())},{"position_id":str(uuid4())},
    {"bom_component_id":str(uuid4()),"bom_component_version":1},
    {"bom_component_id":None,"bom_component_version":2},{"quantity":1.25},{"quantity":"NaN"}])
def test_invalid_sources_and_numeric_representations_reject_atomically(issues,patch):
    r=issues;h=header(r);before=snapshot(r["path"])
    response=r["client"].post(URL+"/"+h["id"]+"/lines",json=line_data(r,h,**patch))
    assert response.status_code==422,response.text
    assert snapshot(r["path"])==before


def test_component_identity_work_order_reassignment_and_stale_versions(issues):
    r=issues;c=r["client"];h=header(r);b=r["boms"][0];first=b["components"][0]
    line=add(r,h,bom_component_id=first["id"],bom_component_version=first["row_version"])
    before=snapshot(r["path"])
    assert c.put(URL+"/"+h["id"],json=header_edit(current(r,h),work_order_id=r["orders"][1]["id"],work_order_version=r["orders"][1]["row_version"])).status_code==409
    assert c.put(URL+"/"+h["id"]+"/lines/"+line["id"],json=line_edit(r,h,line,bom_component_id=r["boms"][1]["components"][0]["id"],bom_component_version=r["boms"][1]["components"][0]["row_version"])).status_code==422
    assert c.put(URL+"/"+h["id"]+"/lines/"+line["id"],json=line_edit(r,h,line,row_version=0)).status_code==409
    assert c.put(URL+"/"+h["id"],json=header_edit(h,note="stale")).status_code==409
    assert snapshot(r["path"])==before
    empty=header(r,"MI-EMPTY")
    moved=c.put(URL+"/"+empty["id"],json=header_edit(empty,work_order_id=r["orders"][1]["id"],work_order_version=r["orders"][1]["row_version"]))
    assert moved.status_code==200 and moved.json()["bom_id"]==r["boms"][1]["id"]
    bad=header_data(r,"MI-STALE");bad["work_order_version"]=0
    assert c.post(URL,json=bad).status_code==409


@pytest.mark.parametrize("attack",["query","headers","body","note","environment"])
def test_live_gate_cannot_be_bypassed_by_http_or_environment(issues,attack,monkeypatch):
    r=issues;h=header(r,note="authorized=true");add(r,h)
    before=snapshot(r["path"]);data=post_data(r,h);kwargs={"json":data};suffix=""
    if attack=="query":suffix="?authorized=true&qa=true&release=true"
    if attack=="headers":kwargs["headers"]={"X-QA-Authorized":"true","X-ERP-Posting-Allowed":"1"}
    if attack=="body":data["authorized"]=True
    if attack=="environment":
        monkeypatch.setenv("ERP_ISSUE_AUTHORIZED","1");monkeypatch.setenv("ERP_QA_POSTING","true")
    response=r["client"].post(URL+"/"+h["id"]+"/post"+suffix,**kwargs)
    assert response.status_code==(422 if attack=="body" else 403)
    assert snapshot(r["path"])==before and balance(r)=="4"


def test_real_qa_post_exact_movement_mrp_and_retry_after_normal_restart(qa):
    r=qa;c=r["client"];assert balance(r)=="4"
    initial=c.get("/api/purchase-outstanding").json()
    h=header(r);line=add(r,h);assert balance(r)=="4";body=post_data(r,h)
    response=post(r,h,body);assert response.status_code==201,response.text
    posted=response.json();assert balance(r)=="2.75"
    assert posted["posting_state"]=="POSTED" and posted["authorization_basis"]=="CONTROLLED_QA_ONLY"
    assert posted["work_order"]["approval_state"]=="DRAFT"
    movement=posted["movements"][0]
    assert movement["qty_base_delta"]=="-1.25" and movement["issue_line_id"]==line["id"]
    assert posted["lines"][0]["position"]["lot_code"]=="LOT-R8-001"
    assert len(posted["lines"][0]["position"]["receipt_origins"])==2
    assert material(preview(r),r)["physical_stock_qty_base"]=="2.75"
    assert material(preview(r),r)["shortage_qty_base"]=="8.8125"
    assert c.get("/api/purchase-outstanding").json()==initial
    before=snapshot(r["path"])
    assert post(r,h,body).status_code==200
    assert post(r,h,{**body,"request_key":str(uuid4())}).status_code==409
    assert post(r,h,{**body,"row_version":body["row_version"]+1}).status_code==409
    assert snapshot(r["path"])==before
    with TestClient(create_app(r["path"])) as restarted:
        assert restarted.get(URL+"/policy").json()["posting_allowed"] is False
        assert restarted.get(URL+"/"+h["id"]).json()["posting_id"]==posted["posting_id"]
        assert restarted.post(URL+"/"+h["id"]+"/post",json=body).status_code==200
        assert snapshot(r["path"])==before


def test_real_multiple_lines_positions_lots_orders_and_no_bom_cap(qa):
    r=qa;c=r["client"]
    receipt=receipt_header(r,"SECOND-LOT");receipt_line(r,receipt,"2",lot_code="LOT-SECOND")
    assert receipt_post(r,receipt).status_code==201
    choices=c.get(URL+"/stock-options").json()
    p=next(p for p in choices if p["lot_code"]=="LOT-SECOND")
    h=header(r);a=add(r,h,"0.5");b=add(r,h,"0.75");d=add(r,h,"1.5",position_id=p["id"])
    posted=post(r,h);assert posted.status_code==201,posted.text
    assert len(posted.json()["movements"])==3 and balance(r)=="2.75"
    other=header(r,"MI-OTHER-WO",1);add(r,other,"0.25")
    assert post(r,other).status_code==201 and balance(r)=="2.5"
    third=header(r,"MI-SAME-WO");add(r,third,"0.125")
    assert post(r,third).status_code==201 and balance(r)=="2.375"


def test_repeated_position_lines_aggregate_before_stock_validation(qa):
    r=qa;h=header(r);add(r,h,"2.5");add(r,h,"2.5")
    before=snapshot(r["path"]);response=post(r,h)
    assert response.status_code==422 and snapshot(r["path"])==before and balance(r)=="4"


@pytest.mark.parametrize("qty",["0","-1.25"])
def test_qa_quantity_conventions_do_not_restrict_drafts(qa,qty):
    r=qa;h=header(r);add(r,h,qty);before=snapshot(r["path"])
    assert post(r,h).status_code==422
    assert snapshot(r["path"])==before


def test_stale_document_and_work_order_post_guards(qa):
    r=qa;h=header(r);add(r,h);before=snapshot(r["path"]);body=post_data(r,h)
    assert post(r,h,{**body,"row_version":0}).status_code==409
    assert post(r,h,{**body,"work_order_version":0}).status_code==409
    assert snapshot(r["path"])==before


def test_same_document_concurrent_post_commits_once(qa):
    r=qa;h=header(r);add(r,h);body=post_data(r,h)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:post(r,h,body),range(2)))
    assert sorted(x.status_code for x in responses)==[200,201]
    assert len({x.json()["posting_id"] for x in responses})==1 and balance(r)=="2.75"


def test_competing_documents_serialize_shared_stock_with_supplier_returns(qa):
    r=qa;a=header(r,"MI-A");b=header(r,"MI-B",1);add(r,a,"3");add(r,b,"3")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda h:post(r,h),[a,b]))
    assert sorted(x.status_code for x in responses)==[201,422] and balance(r)=="1"
    # The existing supplier-return service AND its database guard now see issues.
    doc=return_header(r,"RETURN-AFTER-ISSUE");return_line(r,doc,"2",1)
    before=snapshot(r["path"])
    assert return_post(r,doc).status_code==422 and snapshot(r["path"])==before
    from backend.app.return_models import SupplierReturn,SupplierReturnPosting
    with pytest.raises(IntegrityError,match="physical stock insufficient"):
        with r["db"].write() as session:
            source=session.get(SupplierReturn,doc["id"]);version=source.row_version;source.row_version+=1;session.flush()
            session.add(SupplierReturnPosting(supplier_return_id=source.id,idempotency_key=str(uuid4()),request_fingerprint="QA direct balance guard",source_version=version,posting_state="PREPARING",created_by=DEMO_ACTOR_ID,posted_by=DEMO_ACTOR_ID));session.flush()
    assert snapshot(r["path"])==before


def test_failed_later_ledger_insert_rolls_back_header_posting_and_all_entries(qa):
    r=qa;h=header(r);add(r,h,"0.5");last=add(r,h,"0.75")
    with r["db"].write() as session:
        session.connection().exec_driver_sql("CREATE TRIGGER qa_issue_late BEFORE INSERT ON material_issue_entry WHEN NEW.issue_line_id='"+last["id"]+"' BEGIN SELECT RAISE(ABORT,'injected later material issue entry failure'); END")
    before=snapshot(r["path"]);response=post(r,h)
    assert response.status_code==409
    assert snapshot(r["path"])==before and balance(r)=="4"


def test_injected_finalization_failure_rolls_back_all_deductions(qa):
    r=qa;h=header(r);add(r,h)
    with r["db"].write() as session:
        session.connection().exec_driver_sql("CREATE TRIGGER qa_issue_finalize BEFORE UPDATE ON material_issue_posting BEGIN SELECT RAISE(ABORT,'injected finalize rollback'); END")
    before=snapshot(r["path"])
    assert post(r,h).status_code==409
    assert snapshot(r["path"])==before and balance(r)=="4"


def test_posted_immutability_and_restricted_source_masters(qa):
    r=qa;c=r["client"];h=header(r);l=add(r,h);assert post(r,h).status_code==201
    saved=current(r,h);before=snapshot(r["path"])
    assert c.put(URL+"/"+h["id"],json=header_edit(saved,note="no")).status_code==409
    assert c.delete(URL+"/"+h["id"],params={"row_version":saved["row_version"]}).status_code==409
    assert c.put(URL+"/"+h["id"]+"/lines/"+l["id"],json=line_edit(r,h,l)).status_code==409
    assert c.delete(URL+"/"+h["id"]+"/lines/"+l["id"],params={"row_version":l["row_version"],"document_version":saved["row_version"]}).status_code==409
    assert c.post(URL+"/"+h["id"]+"/lines",json=line_data(r,h)).status_code==409
    for table,where in [("material_issue","id='"+h["id"]+"'"),("material_issue_line","id='"+l["id"]+"'"),
                        ("material_issue_posting","material_issue_id='"+h["id"]+"'"),("material_issue_entry","issue_line_id='"+l["id"]+"'")]:
        for verb in ["UPDATE","DELETE"]:
            with pytest.raises(IntegrityError):
                with r["db"].write() as session:
                    sql=("UPDATE "+table+" SET created_by=created_by WHERE " if verb=="UPDATE" else "DELETE FROM "+table+" WHERE ")+where
                    session.connection().exec_driver_sql(sql)
    for table,key in [("work_order",h["work_order_id"]),("item",l["material_id"]),("warehouse",l["warehouse_id"]),("stock_position",l["position_id"]),("inventory_lot",l["position"]["lot_id"]),("bom",h["bom_id"])]:
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:session.connection().exec_driver_sql("DELETE FROM "+table+" WHERE id=?",(key,))
    assert snapshot(r["path"])==before


def test_raw_sql_cannot_prepare_posting_under_normal_runtime(issues):
    r=issues;h=header(r);add(r,h);h=current(r,h);before=snapshot(r["path"])
    with pytest.raises(IntegrityError,match="policy pending"):
        with r["db"].write() as session:
            row=session.get(MaterialIssue,h["id"]);row.row_version+=1;session.flush()
            session.add(MaterialIssuePosting(material_issue_id=h["id"],idempotency_key=str(uuid4()),
                request_fingerprint="raw",source_version=h["row_version"],work_order_version=h["work_order_version"],
                posting_state="PREPARING",authorization_basis="CONTROLLED_QA_ONLY",created_by=DEMO_ACTOR_ID,posted_by=DEMO_ACTOR_ID))
            session.flush()
    assert snapshot(r["path"])==before


def test_qa_authorization_cannot_point_to_existing_owner_or_survive_restart(tmp_path):
    with pytest.raises(ValueError):
        new_qa_application(ROOT/"data")
    app,path=new_qa_application(tmp_path)
    assert path!=ROOT/"data/erp.sqlite" and path.stat().st_nlink==1 and path.stat().st_size==0
    with TestClient(app) as c:assert c.get(URL+"/policy").json()["posting_allowed"]
    with pytest.raises(RuntimeError,match="single-runtime"):
        with TestClient(app):pass
    with TestClient(create_app(path)) as c:assert not c.get(URL+"/policy").json()["posting_allowed"]


def test_qa_rejects_physical_alias_replacement_before_authorization(tmp_path):
    import os
    app,path=new_qa_application(tmp_path)
    replacement=path.parent/"other.sqlite";replacement.write_bytes(b"")
    os.replace(replacement,path)
    with pytest.raises(RuntimeError,match="physical database"):
        with TestClient(app):pass


def test_new_migration_late_failure_rolls_back_tables_view_and_changed_guards(tmp_path,monkeypatch):
    from alembic import command
    from alembic.config import Config
    path=tmp_path/"old.sqlite";engine=make_engine(path)
    config=Config(str(ROOT/"backend/alembic.ini"));config.set_main_option("script_location",str(ROOT/"backend/migrations"))
    with engine.connect().execution_options(erp_write=True) as connection:
        config.attributes["connection"]=connection;command.upgrade(config,"0009_draft_work_orders")
    engine.dispose();before=snapshot(path)
    module=import_module("backend.migrations.versions.0010_material_issues");execute=module.op.execute;reached=[]
    def failure(statement,*args,**kwargs):
        if "CREATE TRIGGER material_issue_posting_finalize" in str(statement):
            con=module.op.get_bind()
            assert con.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='physical_stock_ledger'").first()
            assert "physical_stock_ledger" in con.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='supplier_return_posting_prepare'").scalar()
            reached.append(True);raise RuntimeError("injected late issue migration failure")
        return execute(statement,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module.op,"execute",failure)
        with pytest.raises(RuntimeError,match="injected late issue"):migrate(path)
    assert reached and snapshot(path)==before
    # Keep historical0010 fault-injection verification independent of later heads.
    for _ in range(2):
        with engine.connect().execution_options(erp_write=True) as connection:
            config.attributes["connection"]=connection
            command.upgrade(config,"0010_material_issues")
    engine.dispose()
    after=snapshot(path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0]=="0010_material_issues"
    assert len(after["rows"])==34 and all(after["rows"][t]==v for t,v in before["rows"].items() if t!="alembic_version")

def test_wrong_existing_material_warehouse_and_cross_parent_line_reject(issues):
    r=issues;c=r["client"];h=header(r);line=add(r,h);other=header(r,"MI-OTHER",1)
    before=snapshot(r["path"])
    for patch in [{"material_id":r["raws"][1]["id"]},{"warehouse_id":r["warehouse2"]["id"]},{"material_id":r["products"][0]["id"]}]:
        assert c.post(URL+"/"+h["id"]+"/lines",json=line_data(r,h,**patch)).status_code==422
    assert c.put(URL+"/"+other["id"]+"/lines/"+line["id"],json=line_edit(r,other,line)).status_code==404
    assert snapshot(r["path"])==before


def test_posting_keys_are_exclusive_across_receipts_returns_and_issues(qa):
    r=qa
    with r["db"].read() as session:
        inbound_key=session.connection().exec_driver_sql("SELECT idempotency_key FROM stock_posting LIMIT 1").scalar()
    h=header(r);add(r,h);before=snapshot(r["path"])
    assert post(r,h,post_data(r,h,request_key=inbound_key)).status_code==409
    assert snapshot(r["path"])==before
    body=post_data(r,h);assert post(r,h,body).status_code==201
    doc=return_header(r,"CROSS-KEY-RETURN");return_line(r,doc,"0.25",1)
    old=r["client"].get("/api/supplier-returns/"+doc["id"]).json()
    before=snapshot(r["path"])
    assert return_post(r,doc,{"request_key":body["request_key"],"row_version":old["row_version"]}).status_code==409
    assert snapshot(r["path"])==before


def test_qa_post_preserves_very_small_exact_decimal_and_has_no_bom_quantity_cap(qa):
    from backend.app.decimal_text import exact_difference
    r=qa;qty="0."+"0"*90+"12345678901234567890123456789"
    h=header(r);add(r,h,qty);response=post(r,h)
    assert response.status_code==201
    assert response.json()["movements"][0]["qty_base_delta"]=="-"+qty
    assert balance(r)==exact_difference("4",qty)
    # A standard BOM usage is never a per-issue upper bound: explicit actual
    # requests use physical stock under the controlled QA convention instead.
    manual=create_work(r,"WO-EXPLICIT-MANUAL",planned_qty_base="0")
    r["orders"].append(manual)
    h=header(r,"MI-MANUAL",2);add(r,h,"1")
    assert h["bom_id"] is None and post(r,h).status_code==201


def test_draft_insert_update_delete_faults_and_create_retries_never_leave_partials(issues):
    r=issues;c=r["client"];h=header(r);line=add(r,h)
    with r["db"].write() as session:
        con=session.connection()
        con.exec_driver_sql("CREATE TRIGGER qa_mi_line_insert BEFORE INSERT ON material_issue_line WHEN NEW.quantity='999' BEGIN SELECT RAISE(ABORT,'line insert rollback'); END")
        con.exec_driver_sql("CREATE TRIGGER qa_mi_line_update BEFORE UPDATE ON material_issue_line WHEN NEW.quantity='999' BEGIN SELECT RAISE(ABORT,'line update rollback'); END")
        con.exec_driver_sql("CREATE TRIGGER qa_mi_line_delete BEFORE DELETE ON material_issue_line BEGIN SELECT RAISE(ABORT,'line delete rollback'); END")
    for action in [
        lambda:c.post(URL+"/"+h["id"]+"/lines",json=line_data(r,h,"999")),
        lambda:c.put(URL+"/"+h["id"]+"/lines/"+line["id"],json=line_edit(r,h,line,quantity="999")),
        lambda:c.delete(URL+"/"+h["id"],params={"row_version":current(r,h)["row_version"]})]:
        before=snapshot(r["path"]);assert action().status_code==409;assert snapshot(r["path"])==before


def test_sqlite_replace_cannot_erase_draft_or_posted_identities(qa):
    r=qa;h=header(r);add(r,h);assert post(r,h).status_code==201
    before=snapshot(r["path"])
    for table in ["material_issue","material_issue_line","material_issue_posting","material_issue_entry"]:
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:
                con=session.connection()
                columns=[row[1] for row in con.exec_driver_sql("PRAGMA table_info("+table+")")]
                names=",".join(columns)
                con.exec_driver_sql("INSERT OR REPLACE INTO "+table+" ("+names+") SELECT "+names+" FROM "+table+" LIMIT 1")
    assert snapshot(r["path"])==before
