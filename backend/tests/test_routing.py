from backend.tests.issue_migration_assertions import assert_issue_ledger_upgrade_objects
"""Routing identities/order/durations and actual SQLite guards, only isolated fixture DBs."""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
import sqlite3
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from backend.app.db import DEMO_ACTOR_ID, ROOT, make_engine, migrate
from backend.app.main import create_app
from backend.tests.test_foundation import setup, resources, payload
from backend.tests.test_supplier_offers import snapshot
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry, query

URL="/api/routings"
OPS="/api/operation-definitions"


@pytest.fixture
def route(setup):
    client,db,path=setup
    cat,unit=resources(client)
    book=client.post("/api/units",json={"code":"BOOK","name":"本"}).json()
    products=[client.post("/api/finished-goods",json=payload(cat,book,"FG-"+str(i))).json() for i in range(2)]
    raw=client.post("/api/raw-materials",json=payload(cat,unit,"RAW-ONLY")).json()
    centers=[client.post("/api/work-centers",json={"code":"CENTER-"+str(i),"name":"工作中心"+str(i),"description":None,"is_active":True}).json() for i in range(2)]
    operations=[client.post(OPS,json={"code":"OP-"+str(i),"name":"作業"+str(i),"instruction":"標準工法","is_active":True}).json() for i in range(3)]
    assert all("id" in r for r in products+centers+operations)
    return dict(client=client,db=db,path=path,products=products,raw=raw,centers=centers,operations=operations)


def data(r,product=0,**patch):
    return {**dict(request_key=str(uuid4()),product_id=r["products"][product]["id"],revision="A",
                   valid_from="2026-11-19",valid_to=None,is_active=True),**patch}


def header(r,product=0,**patch):
    response=r["client"].post(URL,json=data(r,product,**patch));assert response.status_code==201,response.text
    return response.json()


def get(r,h):
    return r["client"].get(URL+"/"+h["id"]).json()


def step_data(r,h,seq="10",operation=0,center=0,**patch):
    return {**dict(request_key=str(uuid4()),routing_version=get(r,h)["row_version"],sequence_no=seq,
        operation_id=r["operations"][operation]["id"],work_center_id=r["centers"][center]["id"],
        setup_minutes=None,run_minutes_per_base=None),**patch}


def add(r,h,seq="10",operation=0,center=0,**patch):
    response=r["client"].post(URL+"/"+h["id"]+"/steps",json=step_data(r,h,seq,operation,center,**patch))
    assert response.status_code==201,response.text
    return response.json()


def header_edit(h,**patch):
    return {**{k:h[k] for k in ["product_id","revision","valid_from","valid_to","is_active","row_version"]},**patch}


def step_edit(r,h,s,**patch):
    return {**{k:s[k] for k in ["sequence_no","operation_id","work_center_id","setup_minutes","run_minutes_per_base","row_version"]},
            "routing_version":get(r,h)["row_version"],**patch}


def test_order_repeated_operations_centers_cross_product_identity_crud_and_reopen(route):
    r=route;c=r["client"];a,b=header(r),header(r,1)
    one=add(r,a,"30",setup_minutes="1.25");two=add(r,a,"10",operation=1,center=1)
    three=add(r,a,"20");add(r,b,"10");other=get(r,b)
    assert len({one["id"],two["id"],three["id"]})==3
    assert [s["sequence_no"] for s in get(r,a)["steps"]]==["10","20","30"]
    target=URL+"/"+a["id"]+"/steps/"+one["id"]
    response=c.put(target,json=step_edit(r,a,one,sequence_no="15",run_minutes_per_base="0.000000000000000000000000000001"))
    assert response.status_code==200,response.text
    moved=response.json()
    assert [s["sequence_no"] for s in get(r,a)["steps"]]==["10","15","20"]
    assert (moved["id"],moved["created_at"],moved["created_by"])==(one["id"],one["created_at"],DEMO_ACTOR_ID)
    assert moved["row_version"]==2 and get(r,a)["row_version"]==5
    assert get(r,a)["product_base_unit_code"]=="BOOK"
    assert c.get(target).json()==moved
    assert c.get(URL+"/"+a["id"]+"/steps").json()==get(r,a)["steps"]
    assert c.get(URL+"/"+b["id"]+"/steps/"+one["id"]).status_code==404
    assert c.put(URL+"/"+a["id"],json=header_edit(get(r,a),product_id=b["product_id"])).status_code==409
    assert len(c.get(URL,params={"q":"OP-0"}).json())==2
    assert len(c.get(URL,params={"q":"CENTER-1"}).json())==1
    assert c.get(URL,params={"q":"%"}).json()==[]
    assert c.delete(URL+"/"+a["id"],params={"row_version":get(r,a)["row_version"]}).status_code==409
    for s in [moved,two,three]:
        assert c.delete(URL+"/"+a["id"]+"/steps/"+s["id"],params={"row_version":s["row_version"],"routing_version":get(r,a)["row_version"]}).status_code==204
    assert c.put(URL+"/"+a["id"],json=header_edit(get(r,a),revision="REVISED",is_active=False)).status_code==200
    assert c.delete(URL+"/"+a["id"],params={"row_version":get(r,a)["row_version"]}).status_code==204
    assert get(r,b)==other
    migrate(r["path"])
    with TestClient(create_app(r["path"])) as reopened:assert reopened.get(URL+"/"+b["id"]).json()==other


@pytest.mark.parametrize("value",[None,"0","-1.25","0."+"0"*1000+"1","9"*5000+"."+"7"*500])
def test_nullable_signed_high_precision_minutes_roundtrip_no_scale_or_rounding(route,value):
    r=route;h=header(r);s=add(r,h,setup_minutes=value,run_minutes_per_base=value)
    assert s["setup_minutes"]==s["run_minutes_per_base"]==value
    assert get(r,h)["steps"][0]==s
    with r["db"].read() as session:
        row=session.connection().exec_driver_sql("SELECT typeof(setup_minutes),setup_minutes,typeof(sequence_no) FROM routing_step").first()
        assert row==("null" if value is None else "text",value,"integer")


def test_user_integer_sequence_range_exact_beyond_js_safe_integer_and_no_fixed_increment(route):
    r=route;h=header(r)
    sequences=["9223372036854775807","-9223372036854775808","9007199254740993","0","-5","00001"]
    for s in sequences:add(r,h,s)
    rows=get(r,h)["steps"]
    assert [s["sequence_no"] for s in rows]==["-9223372036854775808","-5","0","1","9007199254740993","9223372036854775807"]
    for n in range(2,28):add(r,h,str(n))
    assert len(get(r,h)["steps"])==32
    before=snapshot(r["path"])
    assert r["client"].post(URL+"/"+h["id"]+"/steps",json=step_data(r,h,"01")).status_code==409
    assert snapshot(r["path"])==before


@pytest.mark.parametrize("patch",[
    {"revision":" "},{"valid_from":"2026-02-30"},{"valid_to":"2026-11-18"},{"is_active":"true"},
    {"product_id":str(uuid4())},{"release_state":"RELEASED"},{"steps":[]},{"bom_id":str(uuid4())},
    {"output_basis_qty":"1"},{"created_by":str(uuid4())}])
def test_header_rejects_invalid_unapproved_or_bom_fields_without_mutation(route,patch):
    r=route;before=snapshot(r["path"])
    assert r["client"].post(URL,json=data(r,**patch)).status_code==422
    assert snapshot(r["path"])==before


@pytest.mark.parametrize("patch",[
    {"sequence_no":1},{"sequence_no":"1.5"},{"sequence_no":"9223372036854775808"},{"sequence_no":"-9223372036854775809"},
    {"operation_id":str(uuid4())},{"work_center_id":str(uuid4())},{"setup_minutes":1.5},{"run_minutes_per_base":"NaN"},
    {"setup_minutes":"1e3"},{"run_minutes_per_base":"1,5"},{"routing_id":str(uuid4())},{"unit_id":str(uuid4())}])
def test_step_invalid_reference_numeric_or_extra_fields_roll_back(route,patch):
    r=route;h=header(r);before=snapshot(r["path"])
    assert r["client"].post(URL+"/"+h["id"]+"/steps",json=step_data(r,h,**patch)).status_code==422
    assert snapshot(r["path"])==before


def test_raw_product_missing_kind_and_actual_unique_rules(route):
    r=route;c=r["client"]
    assert c.post(URL,json=data(r,product_id=r["raw"]["id"])).status_code==422
    a=header(r);assert c.post(URL,json=data(r)).status_code==409
    assert header(r,1)["revision"]=="A"
    assert header(r,revision="B")["product_id"]==a["product_id"]
    x=add(r,a);y=add(r,a,"15")
    before=snapshot(r["path"])
    assert c.put(URL+"/"+a["id"]+"/steps/"+y["id"],json=step_edit(r,a,y,sequence_no=x["sequence_no"])).status_code==409
    assert snapshot(r["path"])==before


def test_operation_master_crud_uniqueness_audit_and_referenced_center_deletion(route):
    r=route;c=r["client"];body={"code":"NEW","name":"同名允許","instruction":None,"is_active":True}
    created=c.post(OPS,json=body);assert created.status_code==201,created.text
    row=created.json();assert c.post(OPS,json=body).status_code==409
    updated=c.put(OPS+"/"+row["id"],json={**body,"instruction":"新工法","row_version":1})
    assert updated.status_code==200
    assert updated.json()["created_at"]==row["created_at"] and updated.json()["created_by"]==DEMO_ACTOR_ID
    assert c.put(OPS+"/"+row["id"],json={**body,"row_version":1}).status_code==409
    assert len(c.get(OPS,params={"q":"新工法"}).json())==1
    assert c.get(OPS,params={"q":"%"}).json()==[]
    assert c.delete(OPS+"/"+row["id"],params={"row_version":2}).status_code==204
    h=header(r);s=add(r,h)
    assert c.delete(OPS+"/"+s["operation_id"],params={"row_version":1}).status_code==409
    assert c.delete("/api/work-centers/"+s["work_center_id"],params={"row_version":1}).status_code==409
    assert c.delete("/api/finished-goods/"+h["product_id"],params={"row_version":1}).status_code==409


def test_inactive_references_reject_new_assignments_but_preserve_existing_history(route):
    r=route;c=r["client"];h=header(r);s=add(r,h)
    with r["db"].write() as session:
        con=session.connection()
        con.exec_driver_sql("UPDATE work_center SET is_active=0 WHERE id=?",(s["work_center_id"],))
        con.exec_driver_sql("UPDATE operation_definition SET is_active=0 WHERE id=?",(s["operation_id"],))
    assert c.put(URL+"/"+h["id"]+"/steps/"+s["id"],json=step_edit(r,h,s,sequence_no="11")).status_code==200
    before=snapshot(r["path"])
    assert c.post(URL+"/"+h["id"]+"/steps",json=step_data(r,h,"20")).status_code==422
    assert snapshot(r["path"])==before
    # A different active operation/center remains a valid new assignment.
    assert add(r,h,"20",1,1)["operation_is_active"]


def test_direct_sql_fk_kind_sequence_decimal_parent_audit_and_inactive_guards(route):
    r=route;h=header(r);s=add(r,h);other=header(r,1)
    bad=[
        ("UPDATE routing SET product_id=? WHERE id=?",(r["raw"]["id"],h["id"])),
        ("UPDATE routing SET product_id=? WHERE id=?",(other["product_id"],h["id"])),
        ("UPDATE routing SET release_state='RELEASED' WHERE id=?",(h["id"],)),
        ("UPDATE routing_step SET routing_id=? WHERE id=?",(other["id"],s["id"])),
        ("UPDATE routing_step SET work_center_id=? WHERE id=?",(str(uuid4()),s["id"])),
        ("UPDATE routing_step SET sequence_no=1.5 WHERE id=?",(s["id"],)),
        ("UPDATE routing_step SET setup_minutes='1.00' WHERE id=?",(s["id"],)),
        ("UPDATE routing_step SET run_minutes_per_base='NaN' WHERE id=?",(s["id"],)),
        ("UPDATE routing_step SET id=? WHERE id=?",(str(uuid4()),s["id"])),
        ("UPDATE item SET base_unit_id=? WHERE id=?",(r["raw"]["base_unit_id"],h["product_id"])),
        ("UPDATE operation_definition SET created_at='2020-01-01' WHERE id=?",(s["operation_id"],)),
        ("DELETE FROM routing WHERE id=?",(h["id"],)),
        ("DELETE FROM work_center WHERE id=?",(s["work_center_id"],)),
        ("DELETE FROM operation_definition WHERE id=?",(s["operation_id"],))]
    for sql,args in bad:
        before=snapshot(r["path"])
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:session.connection().exec_driver_sql(sql,args)
        assert snapshot(r["path"])==before
    with r["db"].write() as session:
        session.connection().exec_driver_sql("UPDATE work_center SET is_active=0 WHERE id=?",(r["centers"][1]["id"],))
    before=snapshot(r["path"])
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:session.connection().exec_driver_sql("UPDATE routing_step SET work_center_id=? WHERE id=?",(r["centers"][1]["id"],s["id"]))
    assert snapshot(r["path"])==before


def test_actual_incoming_reference_protection_for_header_and_step(route):
    r=route;c=r["client"];h=header(r);s=add(r,h)
    with r["db"].write() as session:
        con=session.connection();con.exec_driver_sql("CREATE TABLE qa_route_ref (route_id TEXT REFERENCES routing(id) ON DELETE RESTRICT, step_id TEXT REFERENCES routing_step(id) ON DELETE RESTRICT)")
        con.exec_driver_sql("INSERT INTO qa_route_ref VALUES (?,?)",(h["id"],s["id"]))
    before=snapshot(r["path"]);base=URL+"/"+h["id"]
    assert c.put(base,json=header_edit(get(r,h),revision="OTHER")).status_code==409
    assert c.delete(base,params={"row_version":get(r,h)["row_version"]}).status_code==409
    assert c.put(base+"/steps/"+s["id"],json=step_edit(r,h,s,sequence_no="99")).status_code==409
    assert c.delete(base+"/steps/"+s["id"],params={"row_version":1,"routing_version":get(r,h)["row_version"]}).status_code==409
    assert snapshot(r["path"])==before
    # Detail-only references are independently honored even without a parent reference.
    with r["db"].write() as session:session.connection().exec_driver_sql("UPDATE qa_route_ref SET route_id=NULL")
    assert c.put(base+"/steps/"+s["id"],json=step_edit(r,h,s,setup_minutes="9")).status_code==409


def test_durable_retries_deleted_tombstones_and_concurrent_header_step_updates(route):
    r=route;c=r["client"];body=data(r)
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(lambda _:c.post(URL,json=body).status_code,range(2)))==[200,201]
    h=c.get(URL).json()[0];base=URL+"/"+h["id"];edit=header_edit(h,revision="UPDATED")
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(lambda _:c.put(base,json=edit).status_code,range(2)))==[200,409]
    assert c.post(URL,json={**body,"revision":"CONFLICT"}).status_code==409
    sb=step_data(r,h);response=c.post(base+"/steps",json=sb);assert response.status_code==201
    s=response.json();assert c.post(base+"/steps",json=sb).status_code==200
    edit=step_edit(r,h,s)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda n:c.put(base+"/steps/"+s["id"],json={**edit,"sequence_no":n}).status_code,["12","13"]))==[200,409]
    current=get(r,h)["steps"][0]
    assert c.delete(base+"/steps/"+s["id"],params={"row_version":1,"routing_version":get(r,h)["row_version"]}).status_code==409
    assert c.delete(base+"/steps/"+s["id"],params={"row_version":current["row_version"],"routing_version":get(r,h)["row_version"]}).status_code==204
    assert c.post(base+"/steps",json=sb).status_code==410
    assert c.delete(base,params={"row_version":get(r,h)["row_version"]}).status_code==204
    assert c.post(URL,json=body).status_code==410


def test_forced_dml_failures_roll_back_parent_detail_and_retry_receipt(route):
    r=route;c=r["client"];h=header(r);s=add(r,h)
    with r["db"].write() as session:
        con=session.connection()
        con.exec_driver_sql("""CREATE TRIGGER qa_step_insert_failure AFTER INSERT ON routing_step WHEN NEW.sequence_no=999
            BEGIN SELECT RAISE(ABORT,'routing forced insert rollback'); END""")
        con.exec_driver_sql("""CREATE TRIGGER qa_step_update_failure AFTER UPDATE ON routing_step WHEN NEW.sequence_no=888
            BEGIN SELECT RAISE(ABORT,'routing forced update rollback'); END""")
        con.exec_driver_sql("""CREATE TRIGGER qa_step_delete_failure AFTER DELETE ON routing_step
            BEGIN SELECT RAISE(ABORT,'routing forced delete rollback'); END""")
    for action in [
        lambda:c.post(URL+"/"+h["id"]+"/steps",json=step_data(r,h,"999")),
        lambda:c.put(URL+"/"+h["id"]+"/steps/"+s["id"],json=step_edit(r,h,s,sequence_no="888")),
        lambda:c.delete(URL+"/"+h["id"]+"/steps/"+s["id"],params={"row_version":1,"routing_version":get(r,h)["row_version"]})]:
        before=snapshot(r["path"]);assert action().status_code==409;assert snapshot(r["path"])==before


def test_additive_migration_cli_metadata_and_mid_ddl_failure(tmp_path,monkeypatch):
    from alembic import command
    from alembic.config import Config
    from backend.app.models import Base
    for fail in [False,True]:
        path=tmp_path/("fail.sqlite" if fail else "upgrade.sqlite")
        engine=make_engine(path);config=Config(str(ROOT/"backend/alembic.ini"));config.set_main_option("script_location",str(ROOT/"backend/migrations"))
        with engine.connect().execution_options(erp_write=True) as con:config.attributes["connection"]=con;command.upgrade(config,"0007_product_bom")
        engine.dispose();before=snapshot(path)
        if fail:
            migration=import_module("backend.migrations.versions.0008_manufacturing_routing");real=migration.op.create_index
            def injected(name,*args,**kwargs):
                if name=="ix_routing_step_work_center_id":raise RuntimeError("injected routing DDL failure")
                return real(name,*args,**kwargs)
            with monkeypatch.context() as m:
                m.setattr(migration.op,"create_index",injected)
                with pytest.raises(RuntimeError,match="injected routing DDL failure"):migrate(path)
            assert snapshot(path)==before
            with sqlite3.connect(path) as con:assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0]=="0007_product_bom"
        else:
            migrate(path);migrate(path);after=snapshot(path)
            assert all(after["rows"][t]==rows for t,rows in before["rows"].items())
            assert_issue_ledger_upgrade_objects(before,after)
            assert {"operation_definition","routing","routing_step"}<=set(Base.metadata.tables)
            assert all(after["rows"][t]==[] for t in ["operation_definition","routing","routing_step"])
            with sqlite3.connect(path) as con:
                from backend.app.stock_ledger import register_exact_sql
                register_exact_sql(con)
                assert len(con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall())==63
                assert con.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
                assert con.execute("PRAGMA foreign_key_check").fetchall()==[]


def test_routing_crud_preserves_boms_posted_receipts_returns_stock_and_original_sum(inquiry):
    r=inquiry;c=r["client"]
    from backend.tests.test_bom import header as bom_header,add as bom_add
    category=c.get("/api/categories").json()[0];unit=c.get("/api/units").json()[0]
    other=c.post("/api/finished-goods",json=payload(category,unit,"OTHER-FINISHED")).json()
    r["products"]=[r["finished"],other]
    a,b=bom_header(r),bom_header(r,1);bom_add(r,a,qty="2.75");bom_add(r,a,1,"0.5");bom_add(r,b,qty="1.25")
    r["centers"]=[c.post("/api/work-centers",json={"code":"CENTER","name":"中心","is_active":True}).json()]
    r["operations"]=[c.post(OPS,json={"code":"OP","name":"工法","is_active":True}).json()]
    before=snapshot(r["path"]);boms=c.get("/api/boms").json();stock=c.get("/api/stock-positions").json();oldquery=query(r)
    h=header(r);s=add(r,h);assert c.put(URL+"/"+h["id"]+"/steps/"+s["id"],json=step_edit(r,h,s,sequence_no="-5",setup_minutes="-0.125")).status_code==200
    after=snapshot(r["path"])
    assert all(after["rows"][t]==rows for t,rows in before["rows"].items() if t not in {"routing","routing_step","stock_create_receipt"})
    assert c.get("/api/boms").json()==boms and c.get("/api/stock-positions").json()==stock and query(r)==oldquery
