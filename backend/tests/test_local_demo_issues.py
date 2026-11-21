"""Actual scoped local-demo authorization, using only fresh synthetic databases."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import tempfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT, Database, DEMO_ACTOR_ID
from backend.app.main import create_app
from backend.app.issue_models import MaterialIssue, MaterialIssuePosting
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
from backend.tests.test_mrp import seed, preview, material
from backend.tests.test_material_issues import (
    URL, header, add, current, header_edit, line_edit, line_data, balance,
)
from backend.tests.test_supplier_offers import snapshot


def seed_demo_database(path):
    """Browser/API helper: fresh artifact-only data, never a live Owner copy."""
    path=Path(path).resolve()
    assert path.is_relative_to((ROOT/"artifacts").resolve()) and not path.exists()
    path.parent.mkdir(parents=True,exist_ok=True)
    with TestClient(create_app(path)) as client:
        r=po.__wrapped__((client,client.app.state.db,path))
        seed(inquiry.__wrapped__(returns.__wrapped__(r)))
        issue=header(r,"ISSUE-DEMO-001")
        add(r,issue,"1.75")
        issue=current(r,issue)
        assert balance(r)=="4" and issue["posting_state"]=="DRAFT"
        assert issue["lines"][0]["bom_component_id"] is None
        return dict(database=str(path),issue=issue,**{k:r[k] for k in [
            "orders","raws","warehouse","warehouse2","boms"]})


@pytest.fixture
def demo(tmp_path):
    from backend.app.local_demo import create_local_demo_app
    parent=tmp_path.resolve()
    if not parent.is_relative_to((ROOT/"artifacts").resolve()):
        base=ROOT/"artifacts";base.mkdir(exist_ok=True)
        parent=Path(tempfile.mkdtemp(prefix="local-demo-tests-",dir=base))
    path=parent/"demo.sqlite"
    metadata=seed_demo_database(path)
    app=create_local_demo_app(path,backend_port=8024,frontend_port=5187,bind_host="127.0.0.1")
    with TestClient(app,base_url="http://127.0.0.1:8024",client=("127.0.0.1",50000)) as client:
        yield {**metadata,"client":client,"db":app.state.db,"path":path,"app":app}


def review(r,h=None):
    h=h or r["issue"]
    response=r["client"].get(URL+"/"+h["id"]+"/posting-review")
    assert response.status_code==200,response.text
    return response.json()


def confirmed_body(r,h=None,**patch):
    data=review(r,h)
    issue=data["issue"]
    return {**dict(request_key=str(uuid4()),row_version=issue["row_version"],
        work_order_version=issue["work_order_version"],confirmation_token=data["confirmation_token"]),**patch}


def execute(r,h=None,body=None):
    h=h or r["issue"]
    return r["client"].post(URL+"/"+h["id"]+"/post",json=body or confirmed_body(r,h))


def test_review_is_readonly_and_actual_demo_post_has_distinct_audit_and_exact_stock(demo):
    r=demo;c=r["client"];h=r["issue"]
    baseline=snapshot(r["path"]);purchasing=c.get("/api/purchase-outstanding").json()
    reviewed=review(r)
    assert snapshot(r["path"])==baseline and balance(r)=="4"
    assert reviewed["issue"]["document_no"]=="ISSUE-DEMO-001"
    assert reviewed["issue"]["work_order"]["approval_state"]=="DRAFT"
    assert reviewed["expected_movements"][0]["issue_line_id"]==h["lines"][0]["id"]
    assert reviewed["expected_movements"][0]["position_id"]==h["lines"][0]["position_id"]
    assert reviewed["expected_movements"][0]["qty_base_delta"]=="-1.75"
    response=execute(r,body=confirmed_body(r))
    assert response.status_code==201,response.text
    result=response.json()
    assert result["posting_state"]=="POSTED" and result["authorization_basis"]=="LOCAL_DEMO_MANUAL"
    assert result["work_order"]["approval_state"]=="DRAFT"
    assert result["work_order"]["bom"]["release_state"]=="DRAFT"
    assert result["lines"][0]["bom_component_id"] is None
    assert result["movements"][0]["qty_base_delta"]=="-1.75" and balance(r)=="2.25"
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT count(*) FROM material_issue_posting").scalar()==1
        assert session.connection().exec_driver_sql("SELECT authorization_basis FROM material_issue_posting").scalar()=="LOCAL_DEMO_MANUAL"
        audit=session.connection().exec_driver_sql(
            "SELECT confirmation_digest,confirmation_snapshot,confirmation_received_at FROM material_issue_posting").one()
        assert all(audit) and len(audit[0])==64
        assert isinstance(json.loads(audit[1]),dict)
        assert h["id"] in audit[1] and h["lines"][0]["id"] in audit[1]
        assert h["lines"][0]["position_id"] in audit[1] and "1.75" in audit[1]
    assert c.get("/api/purchase-outstanding").json()==purchasing
    data=preview(r)
    assert (material(data,r)["gross_demand_qty_base"],material(data,r)["physical_stock_qty_base"],material(data,r)["shortage_qty_base"])==("11.5625","2.25","9.3125")
    assert any("已領" in warning and ("剩餘" in warning or "需求" in warning) for warning in data["warnings"])


def test_same_key_retry_conflicts_and_normal_restart_never_deduct_twice(demo):
    r=demo;h=r["issue"];body=confirmed_body(r)
    first=execute(r,body=body);assert first.status_code==201
    before=snapshot(r["path"])
    again=execute(r,body=body);assert again.status_code==200
    assert again.json()["posting_id"]==first.json()["posting_id"]
    assert execute(r,body={**body,"request_key":str(uuid4())}).status_code==409
    assert execute(r,body={**body,"row_version":0}).status_code==409
    assert snapshot(r["path"])==before and balance(r)=="2.25"
    with TestClient(create_app(r["path"])) as ordinary:
        assert not ordinary.get(URL+"/policy").json()["posting_allowed"]
        retry=ordinary.post(URL+"/"+h["id"]+"/post",json=body)
        assert retry.status_code==200 and retry.json()["posting_id"]==first.json()["posting_id"]
    assert snapshot(r["path"])==before


def test_stock_change_invalidates_review_even_when_quantity_still_fits(demo):
    r=demo;original=confirmed_body(r)
    other=header(r,"DEMO-STOCK-CHANGE");add(r,other,"0.25")
    assert execute(r,other,confirmed_body(r,other)).status_code==201
    assert balance(r)=="3.75"
    before=snapshot(r["path"])
    assert execute(r,body=original).status_code==409
    assert snapshot(r["path"])==before and balance(r)=="3.75"
    assert current(r,r["issue"])["posting_state"]=="DRAFT"


def test_unposted_confirmation_cannot_move_to_a_new_demo_runtime(demo):
    from backend.app.local_demo import create_local_demo_app
    r=demo;body=confirmed_body(r);before=snapshot(r["path"])
    fresh=create_local_demo_app(r["path"],backend_port=8024,frontend_port=5187)
    with TestClient(fresh,base_url="http://127.0.0.1:8024",client=("127.0.0.1",50001)) as client:
        response=client.post(URL+"/"+r["issue"]["id"]+"/post",json=body)
        assert response.status_code==403,response.text
        assert client.get(URL+"/policy").json()["posting_allowed"]
    assert snapshot(r["path"])==before and balance(r)=="4"


def test_foreign_origin_cannot_borrow_loopback_demo_context(demo):
    r=demo;body=confirmed_body(r);before=snapshot(r["path"])
    for origin in ["https://foreign.example","http://127.0.0.1:9999","null"]:
        response=r["client"].post(URL+"/"+r["issue"]["id"]+"/post",
                                  json=body,headers={"Origin":origin})
        assert response.status_code==403,response.text
        assert snapshot(r["path"])==before
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT erp_issue_manual_authorized()").scalar()==0
    assert balance(r)=="4"


def test_0011_late_restoration_failure_rolls_back_exact_old_schema_and_rows(tmp_path,monkeypatch):
    from importlib import import_module
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import event
    from backend.app.db import make_engine,migrate

    path=tmp_path/"old0010.sqlite";engine=make_engine(path)
    config=Config(str(ROOT/"backend/alembic.ini"))
    config.set_main_option("script_location",str(ROOT/"backend/migrations"))
    with engine.connect().execution_options(erp_write=True) as connection:
        config.attributes["connection"]=connection;command.upgrade(config,"0010_material_issues")
    engine.dispose()
    database=Database(path);database.seed_operator();database.engine.dispose()

    def original_snapshot():
        with sqlite3.connect(path) as db:
            names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return dict(columns={name:[c[1] for c in db.execute('PRAGMA table_info("'+name+'")')] for name in names},
                rows={name:db.execute('SELECT rowid,* FROM "'+name+'" ORDER BY rowid').fetchall() for name in names},
                objects=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall())
    before=original_snapshot()
    module=import_module("backend.migrations.versions.0011_local_demo_issue_confirmation")
    real=module.op.execute;fired=[]
    def late(statement,*args,**kwargs):
        result=real(statement,*args,**kwargs)
        if "CREATE TRIGGER supplier_return_posting_prepare" in str(statement):
            con=module.op.get_bind()
            assert con.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='physical_stock_ledger'").first()
            assert "confirmation_snapshot" in [row[1] for row in con.exec_driver_sql("PRAGMA table_info(material_issue_posting)")]
            fired.append(True);raise RuntimeError("injected late0011 restored-trigger failure")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(module.op,"execute",late)
        with pytest.raises(RuntimeError,match="late0011"):migrate(path)
    assert fired and original_snapshot()==before

    # This historical restoration proof intentionally ends at0011.
    engine=make_engine(path)
    try:
        with engine.connect().execution_options(erp_write=True) as connection:
            config.attributes["connection"]=connection
            command.upgrade(config,module.revision)
    finally:
        engine.dispose()
    after=original_snapshot()
    assert set(after["rows"])==set(before["rows"])
    changed={"material_issue_posting","material_issue_posting_prepare",
             "material_issue_entry_outbound","material_issue_posting_finalize"}
    assert all(obj in after["objects"] for obj in before["objects"] if obj[1] not in changed)
    with sqlite3.connect(path) as db:
        for name,columns in before["columns"].items():
            if name=="alembic_version":continue
            projection=",".join('"'+column+'"' for column in columns)
            assert db.execute('SELECT rowid,'+projection+' FROM "'+name+'" ORDER BY rowid').fetchall()==before["rows"][name]
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0]==module.revision
        assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall()==[]
    assert set(after["columns"]["material_issue_posting"])-set(before["columns"]["material_issue_posting"])=={
        "confirmation_digest","confirmation_snapshot","confirmation_received_at"}

def test_unconfirmed_or_tampered_confirmation_is_not_authorization(demo):
    r=demo;before=snapshot(r["path"]);body=confirmed_body(r)
    for patch in [{"confirmation_token":"forged"},{"confirmation_token":""}]:
        assert execute(r,body={**body,**patch}).status_code in {403,422}
        assert snapshot(r["path"])==before
    missing={k:v for k,v in body.items() if k!="confirmation_token"}
    assert execute(r,body=missing).status_code in {403,422}
    assert snapshot(r["path"])==before and balance(r)=="4"


def test_plain_runtime_and_client_flags_cannot_enable_new_post(demo,monkeypatch):
    r=demo;body=confirmed_body(r);h=r["issue"];before=snapshot(r["path"])
    monkeypatch.setenv("ERP_ISSUE_AUTHORIZED","1")
    monkeypatch.setenv("ERP_QA_POSTING","true")
    with TestClient(create_app(r["path"])) as ordinary:
        for suffix,headers in [("?local_demo=true&authorized=true",{}),
                               ("",{"X-QA-Authorized":"true","X-Local-Demo":"true"})]:
            response=ordinary.post(URL+"/"+h["id"]+"/post"+suffix,headers=headers,json=body)
            assert response.status_code==403,response.text
        assert ordinary.post(URL+"/"+h["id"]+"/post",json={**body,"authorized":True}).status_code==422
    assert snapshot(r["path"])==before and balance(r)=="4"


def test_wrong_http_client_host_and_port_do_not_receive_demo_authority(demo):
    r=demo;h=r["issue"];body=confirmed_body(r);before=snapshot(r["path"])
    for base,client_address in [
        ("http://127.0.0.1:8024",("198.51.100.24",50000)),
        ("http://127.0.0.1:9999",("127.0.0.1",50000)),
        ("http://foreign.example:8024",("127.0.0.1",50000))]:
        # Do not enter a second lifespan; use the already-started app with a
        # different real ASGI request scope.
        client=TestClient(r["app"],base_url=base,client=client_address)
        try:
            response=client.post(URL+"/"+h["id"]+"/post",json=body)
            assert response.status_code in {400,403},response.text
        finally:client.close()
    assert snapshot(r["path"])==before and balance(r)=="4"


def test_demo_requires_loopback_binding_and_exact_database_instance(demo,tmp_path):
    from backend.app.local_demo import create_local_demo_app
    with pytest.raises((ValueError,RuntimeError)):
        create_local_demo_app(demo["path"],bind_host="0.0.0.0")
    r=demo;body=confirmed_body(r);before=snapshot(r["path"]);original=r["app"].state.db
    other=tmp_path/"other.sqlite"
    with sqlite3.connect(r["path"]) as source,sqlite3.connect(other) as copy:
        source.backup(copy)
    other_before=snapshot(other)
    replacement=Database(other)
    r["app"].state.db=replacement
    try:
        response=execute(r,body=body)
        assert response.status_code in {403,409}
    finally:
        r["app"].state.db=original;replacement.engine.dispose()
    assert snapshot(r["path"])==before and balance(r)=="4"
    assert snapshot(other)==other_before


def test_confirmation_becomes_stale_after_line_edit_and_source_versions_reject(demo):
    r=demo;c=r["client"];h=r["issue"];body=confirmed_body(r)
    line=current(r,h)["lines"][0]
    changed=c.put(URL+"/"+h["id"]+"/lines/"+line["id"],json=line_edit(r,h,line,quantity="1.5"))
    assert changed.status_code==200
    before=snapshot(r["path"])
    assert execute(r,body=body).status_code==409
    assert snapshot(r["path"])==before and balance(r)=="4"
    valid=confirmed_body(r)
    assert execute(r,body={**valid,"work_order_version":0}).status_code==409
    assert execute(r,body={**valid,"row_version":0}).status_code==409
    assert snapshot(r["path"])==before


def test_duplicate_concurrent_confirmations_create_exactly_one_posting(demo):
    r=demo;body=confirmed_body(r)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:execute(r,body=body),range(2)))
    assert sorted(x.status_code for x in responses)==[200,201]
    assert len({x.json()["posting_id"] for x in responses})==1 and balance(r)=="2.25"


def test_competing_documents_and_same_position_line_aggregation(demo):
    r=demo;a=header(r,"DEMO-COMPETE-A");b=header(r,"DEMO-COMPETE-B",1)
    add(r,a,"3");add(r,b,"3")
    bodies=[confirmed_body(r,a),confirmed_body(r,b)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda pair:execute(r,pair[0],pair[1]),zip([a,b],bodies)))
    statuses=sorted(x.status_code for x in responses)
    assert statuses in ([201,409],[201,422]) and balance(r)=="1"
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT count(*) FROM material_issue_posting").scalar()==1


def test_multiple_lines_cannot_independently_spend_shared_stock(demo):
    r=demo;h=header(r,"DEMO-AGGREGATE");add(r,h,"2.5");add(r,h,"2.5")
    before=snapshot(r["path"])
    # Review may refuse known insufficiency, or final atomic posting must.
    response=r["client"].get(URL+"/"+h["id"]+"/posting-review")
    if response.status_code==200:
        reviewed=response.json();issue=reviewed["issue"]
        body=dict(request_key=str(uuid4()),row_version=issue["row_version"],
                  work_order_version=issue["work_order_version"],confirmation_token=reviewed["confirmation_token"])
        assert execute(r,h,body).status_code==422
    else:
        assert response.status_code==422
    assert snapshot(r["path"])==before and balance(r)=="4"


def test_signed_zero_drafts_remain_saved_but_local_demo_posting_rejects(demo):
    r=demo
    for index,qty in enumerate(["0","-1.75"]):
        h=header(r,"DEMO-SIGNED-"+str(index));line=add(r,h,qty)
        assert line["quantity"]==qty
        before=snapshot(r["path"])
        response=r["client"].get(URL+"/"+h["id"]+"/posting-review")
        if response.status_code==200:
            data=response.json();issue=data["issue"]
            body=dict(request_key=str(uuid4()),row_version=issue["row_version"],
                      work_order_version=issue["work_order_version"],confirmation_token=data["confirmation_token"])
            assert execute(r,h,body).status_code==422
        else:
            assert response.status_code==422
        assert snapshot(r["path"])==before
    assert balance(r)=="4"


def test_later_entry_failure_rolls_back_every_line_document_and_confirmation_audit(demo):
    r=demo;h=header(r,"DEMO-ATOMIC");add(r,h,"0.5");last=add(r,h,"0.75")
    body=confirmed_body(r,h)
    with r["db"].write() as session:
        session.connection().exec_driver_sql(
            "CREATE TRIGGER demo_fail_later BEFORE INSERT ON material_issue_entry WHEN NEW.issue_line_id='"+
            last["id"]+"' BEGIN SELECT RAISE(ABORT,'injected demo later-line failure'); END")
    before=snapshot(r["path"])
    assert execute(r,h,body).status_code==409
    assert snapshot(r["path"])==before and balance(r)=="4"
    # A request-scoped database grant must not survive the rollback.
    with r["db"].read() as session:
        grant=session.connection().exec_driver_sql(
            "SELECT erp_issue_manual_authorized(),erp_issue_manual_document(),"
            "erp_issue_manual_digest(),erp_issue_manual_snapshot(),erp_issue_manual_time()").one()
        assert tuple(grant)==(0,None,None,None,None)
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:
            parent=session.get(MaterialIssue,h["id"]);version=parent.row_version;parent.row_version+=1;session.flush()
            session.add(MaterialIssuePosting(material_issue_id=h["id"],idempotency_key=str(uuid4()),
                request_fingerprint="unconfirmed direct SQL",source_version=version,
                work_order_version=h["work_order_version"],posting_state="PREPARING",
                authorization_basis="LOCAL_DEMO_MANUAL",created_by=DEMO_ACTOR_ID,posted_by=DEMO_ACTOR_ID))
            session.flush()
    assert snapshot(r["path"])==before


def test_posted_demo_history_and_sources_remain_immutable(demo):
    r=demo;c=r["client"];h=r["issue"];body=confirmed_body(r)
    assert execute(r,body=body).status_code==201
    posted=current(r,h);line=posted["lines"][0];before=snapshot(r["path"])
    assert c.put(URL+"/"+h["id"],json=header_edit(posted,note="forbidden")).status_code==409
    assert c.delete(URL+"/"+h["id"],params={"row_version":posted["row_version"]}).status_code==409
    assert c.put(URL+"/"+h["id"]+"/lines/"+line["id"],json=line_edit(r,h,line,quantity="99")).status_code==409
    for table,key in [("work_order",posted["work_order_id"]),("stock_position",line["position_id"]),
                      ("inventory_lot",line["position"]["lot_id"]),("item",line["material_id"])]:
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:
                session.connection().exec_driver_sql("DELETE FROM "+table+" WHERE id=?",(key,))
    assert snapshot(r["path"])==before and balance(r)=="2.25"


def test_manual_grant_is_bound_to_one_physical_connection_within_request_context(demo):
    from sqlalchemy import event
    r=demo;body=confirmed_body(r);observed=[];checking=False

    def before_insert(connection,cursor,statement,parameters,context,executemany):
        nonlocal checking
        if checking or not statement.lstrip().upper().startswith("INSERT INTO MATERIAL_ISSUE_POSTING"):
            return
        # The nested queries run in the same worker/request ContextVar. Set
        # this flag before either query to avoid recursively observing them.
        checking=True
        try:
            same=connection.exec_driver_sql("SELECT erp_issue_manual_authorized()").scalar()
            with r["db"].engine.connect() as other:
                assert other.connection.driver_connection is not connection.connection.driver_connection
                separate=other.exec_driver_sql("SELECT erp_issue_manual_authorized()").scalar()
            restored=connection.exec_driver_sql("SELECT erp_issue_manual_authorized()").scalar()
            observed.append((same,separate,restored))
        finally:
            checking=False

    event.listen(r["db"].engine,"before_cursor_execute",before_insert)
    try:
        response=execute(r,body=body)
    finally:
        event.remove(r["db"].engine,"before_cursor_execute",before_insert)
    assert response.status_code==201,response.text
    assert observed==[(1,0,1)] and balance(r)=="2.25"

