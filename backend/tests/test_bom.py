from backend.tests.issue_migration_assertions import assert_issue_ledger_upgrade_objects
"""Real SQLite/API BOM verification; fixture databases only."""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from uuid import uuid4
import sqlite3
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from backend.app.db import DEMO_ACTOR_ID, make_engine, migrate
from backend.app.main import create_app
from backend.tests.test_foundation import setup, resources, payload
from backend.tests.test_supplier_offers import snapshot
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry, query

URL = "/api/boms"


@pytest.fixture
def bom(setup):
    client, db, path = setup
    cat, unit = resources(client)
    other = client.post("/api/units", json={"code": "PCS", "name": "本"}).json()
    products = [client.post("/api/finished-goods", json=payload(cat, other, "FG-"+str(i))).json() for i in range(2)]
    raws = [client.post("/api/raw-materials", json=payload(cat, unit, "RAW-"+str(i))).json() for i in range(2)]
    return dict(client=client, db=db, path=path, products=products, raws=raws, unit=unit, product_unit=other)


def data(b, product=0, **patch):
    return {**dict(request_key=str(uuid4()), product_id=b["products"][product]["id"], revision="A",
                output_basis_qty="10", valid_from="2026-11-19", valid_to=None, is_active=True), **patch}


def header(b, product=0, **patch):
    response = b["client"].post(URL, json=data(b, product, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def get(b, h):
    return b["client"].get(URL+"/"+h["id"]).json()


def component_data(b, h, material=0, qty="0.123456789012345678901234567890123456789", **patch):
    return dict(request_key=str(uuid4()), bom_version=get(b,h)["row_version"],
                material_id=b["raws"][material]["id"], usage_qty_base=qty, purpose_note="內頁", **patch)


def add(b, h, material=0, qty="0.123456789012345678901234567890123456789"):
    response = b["client"].post(URL+"/"+h["id"]+"/components", json=component_data(b,h,material,qty))
    assert response.status_code == 201, response.text
    return response.json()


def edit_header(h, **patch):
    return {**{k:h[k] for k in ["product_id","revision","output_basis_qty","valid_from","valid_to","is_active","row_version"]}, **patch}


def edit_component(b,h,c,**patch):
    return {**{k:c[k] for k in ["material_id","usage_qty_base","purpose_note","row_version"]},
            "bom_version":get(b,h)["row_version"], **patch}


def test_relationships_repeated_raw_units_isolation_crud_audit_and_reopen(bom):
    b=bom;client=b["client"]
    a,other=header(b),header(b,1)
    one,two,repeat=add(b,a),add(b,a,1,"12.75"),add(b,a,0,"2")
    add(b,other,0,"9.25"); frozen=get(b,other)
    saved=get(b,a)
    assert [c["material_id"] for c in saved["components"]]==[b["raws"][0]["id"],b["raws"][1]["id"],b["raws"][0]["id"]]
    assert saved["output_basis_qty"]=="10" and saved["product_base_unit_code"]=="PCS"
    assert all(c["material_base_unit_code"]=="SHEET" for c in saved["components"])
    assert saved["created_by"]==one["created_by"]==DEMO_ACTOR_ID
    assert len(client.get(URL,params={"q":"RAW-0"}).json())==2
    assert len(client.get(URL,params={"q":"FG-0"}).json())==1
    assert client.get(URL,params={"q":"%"}).json()==[]
    target=URL+"/"+a["id"]+"/components/"+one["id"]
    updated=client.put(target,json=edit_component(b,a,one,usage_qty_base="4.000000000000000000000000001")).json()
    assert updated["row_version"]==2
    assert (updated["id"],updated["created_at"],updated["created_by"])==(one["id"],one["created_at"],one["created_by"])
    assert client.get(URL+"/"+a["id"]+"/components").json()==get(b,a)["components"]
    assert client.get(target).json()==updated
    assert client.get(URL+"/"+other["id"]+"/components/"+one["id"]).status_code==404
    assert client.delete(URL+"/"+a["id"],params={"row_version":get(b,a)["row_version"]}).status_code==409
    assert client.put(URL+"/"+a["id"],json=edit_header(get(b,a),product_id=other["product_id"])).status_code==409
    for c in [updated,two,repeat]:
        assert client.delete(URL+"/"+a["id"]+"/components/"+c["id"],
            params={"row_version":c["row_version"],"bom_version":get(b,a)["row_version"]}).status_code==204
    assert client.put(URL+"/"+a["id"],json=edit_header(get(b,a),revision="B",is_active=False)).status_code==200
    assert client.delete(URL+"/"+a["id"],params={"row_version":get(b,a)["row_version"]}).status_code==204
    assert get(b,other)==frozen
    migrate(b["path"])
    with TestClient(create_app(b["path"])) as reopened:
        assert reopened.get(URL+"/"+other["id"]).json()==frozen


@pytest.mark.parametrize("qty",["0","-2.5","0."+"0"*1000+"1","9"*5000+"."+"7"*500])
def test_unbounded_exact_signed_draft_text_without_float_or_rounding(bom,qty):
    b=bom;h=header(b)
    c=add(b,h,qty=qty)
    assert c["usage_qty_base"]==get(b,h)["components"][0]["usage_qty_base"]==qty
    with b["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT typeof(usage_qty_base),usage_qty_base FROM bom_component").first()==("text",qty)


@pytest.mark.parametrize("patch",[
    {"output_basis_qty":"0"},{"output_basis_qty":"-1"},{"output_basis_qty":1.2},
    {"output_basis_qty":"NaN"},{"output_basis_qty":"1e3"},{"valid_from":"2026-02-30"},
    {"valid_to":"2026-11-18"},{"revision":" "},{"is_active":"true"},{"release_state":"RELEASED"},
    {"components":[]},{"scrap_allowance_base":"0"},{"created_by":str(uuid4())}])
def test_header_rejects_invalid_or_unapproved_fields_without_partial_rows(bom,patch):
    b=bom;before=snapshot(b["path"]);body={**data(b),**patch}
    assert b["client"].post(URL,json=body).status_code==422
    assert snapshot(b["path"])==before


@pytest.mark.parametrize("patch",[
    {"usage_qty_base":1.2},{"usage_qty_base":"NaN"},{"usage_qty_base":"1e3"},{"usage_qty_base":"1,2"},
    {"usage_qty_base":""},{"material_id":str(uuid4())},{"scrap_allowance_base":"0"},
    {"line_no":55},{"bom_id":str(uuid4())}])
def test_component_validation_atomicity(bom,patch):
    b=bom;h=header(b);before=snapshot(b["path"])
    assert b["client"].post(URL+"/"+h["id"]+"/components",json={**component_data(b,h),**patch}).status_code==422
    assert snapshot(b["path"])==before


def test_kind_missing_fk_uniqueness_and_business_identity(bom):
    b=bom;client=b["client"]
    for product in [b["raws"][0]["id"],str(uuid4())]:
        assert client.post(URL,json={**data(b),"product_id":product}).status_code==422
    h=header(b)
    assert client.post(URL,json=data(b)).status_code==409
    assert header(b,1)["revision"]=="A"
    assert header(b,revision="B")["product_id"]==h["product_id"]
    for material in [b["products"][0]["id"],str(uuid4())]:
        assert client.post(URL+"/"+h["id"]+"/components",json={**component_data(b,h),"material_id":material}).status_code==422
    with b["db"].write() as session:
        connection=session.connection()
        assert connection.exec_driver_sql("SELECT count(*) FROM bom").scalar()==3
        assert len(list(connection.exec_driver_sql("PRAGMA foreign_key_list(bom_component)")))==3


def test_sql_guards_reference_identity_sealed_history_and_no_reparent(bom):
    b=bom;client=b["client"];h=header(b);c=add(b,h);other=header(b,1)
    bad=[
        ("UPDATE bom SET product_id=? WHERE id=?",(b["raws"][0]["id"],h["id"])),
        ("UPDATE bom_component SET material_id=? WHERE id=?",(b["products"][0]["id"],c["id"])),
        ("UPDATE bom_component SET bom_id=? WHERE id=?",(other["id"],c["id"])),
        ("UPDATE bom SET output_basis_qty='0' WHERE id=?",(h["id"],)),
        ("UPDATE bom SET output_basis_qty='1e3' WHERE id=?",(h["id"],)),
        ("UPDATE bom_component SET usage_qty_base='1.00' WHERE id=?",(c["id"],)),
        ("UPDATE bom_component SET created_by=? WHERE id=?",(str(uuid4()),c["id"])),
        ("UPDATE item SET code='NEW' WHERE id=?",(h["product_id"],)),
        ("UPDATE item SET item_kind='FINISHED' WHERE id=?",(c["material_id"],)),
        ("UPDATE item SET base_unit_id=? WHERE id=?",(b["product_unit"]["id"],c["material_id"])),
        ("DELETE FROM bom WHERE id=?",(h["id"],)),
        ("DELETE FROM item WHERE id=?",(c["material_id"],))]
    for sql,args in bad:
        before=snapshot(b["path"])
        with pytest.raises(IntegrityError):
            with b["db"].write() as session:session.connection().exec_driver_sql(sql,args)
        assert snapshot(b["path"])==before
    for kind,row in [("finished-goods",b["products"][0]),("raw-materials",b["raws"][0])]:
        assert client.delete("/api/"+kind+"/"+row["id"],params={"row_version":1}).status_code==409
    with b["db"].write() as session:session.connection().exec_driver_sql("UPDATE bom SET release_state='RELEASED' WHERE id=?",(h["id"],))
    before=snapshot(b["path"])
    assert client.put(URL+"/"+h["id"],json=edit_header(get(b,h),revision="X")).status_code==409
    assert client.post(URL+"/"+h["id"]+"/components",json=component_data(b,h)).status_code==409
    for sql,args in [
        ("UPDATE bom SET is_active=0 WHERE id=?",(h["id"],)),
        ("UPDATE bom_component SET usage_qty_base='7' WHERE id=?",(c["id"],)),
        ("DELETE FROM bom_component WHERE id=?",(c["id"],))]:
        with pytest.raises(IntegrityError):
            with b["db"].write() as session:session.connection().exec_driver_sql(sql,args)
    assert snapshot(b["path"])==before


def test_actual_incoming_references_block_header_and_component_mutations(bom):
    b=bom;client=b["client"];h=header(b);c=add(b,h)
    with b["db"].write() as session:
        con=session.connection()
        con.exec_driver_sql("CREATE TABLE qa_reference (bom_id TEXT REFERENCES bom(id) ON DELETE RESTRICT, component_id TEXT REFERENCES bom_component(id) ON DELETE RESTRICT)")
        con.exec_driver_sql("INSERT INTO qa_reference VALUES (?,?)",(h["id"],c["id"]))
    before=snapshot(b["path"]);base=URL+"/"+h["id"]
    assert client.put(base,json=edit_header(get(b,h),revision="Z")).status_code==409
    assert client.delete(base,params={"row_version":get(b,h)["row_version"]}).status_code==409
    assert client.put(base+"/components/"+c["id"],json=edit_component(b,h,c,usage_qty_base="9")).status_code==409
    assert client.delete(base+"/components/"+c["id"],params={"row_version":c["row_version"],"bom_version":get(b,h)["row_version"]}).status_code==409
    assert snapshot(b["path"])==before


def test_durable_retry_tombstones_and_cross_domain_conflicts(bom):
    b=bom;client=b["client"];body=data(b)
    response=client.post(URL,json=body);h=response.json()
    assert response.status_code==201
    assert client.post(URL,json=body).status_code==200
    assert client.post(URL,json={**body,"revision":"X"}).status_code==409
    d=component_data(b,h);base=URL+"/"+h["id"]+"/components"
    c=client.post(base,json=d).json()
    assert client.post(base,json=d).status_code==200
    assert len(get(b,h)["components"])==1
    assert client.post(URL,json={**data(b,1),"request_key":d["request_key"]}).status_code==409
    assert client.delete(base+"/"+c["id"],params={"row_version":1,"bom_version":get(b,h)["row_version"]}).status_code==204
    assert client.post(base,json=d).status_code==410
    assert client.delete(URL+"/"+h["id"],params={"row_version":get(b,h)["row_version"]}).status_code==204
    assert client.post(URL,json=body).status_code==410


def test_concurrent_header_detail_cas_and_forced_rollback(bom):
    b=bom;client=b["client"];body=data(b)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(lambda _:client.post(URL,json=body).status_code,range(2)))
    assert sorted(statuses)==[200,201]
    h=client.get(URL).json()[0];c=add(b,h);base=URL+"/"+h["id"]+"/components/"+c["id"]
    d=edit_component(b,h,c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(lambda n:client.put(base,json={**d,"usage_qty_base":n}).status_code,["2","3"]))
    assert sorted(statuses)==[200,409]
    assert client.delete(URL+"/"+h["id"],params={"row_version":1}).status_code==409
    saved=get(b,h);c=saved["components"][0]
    with b["db"].write() as session:
        session.connection().exec_driver_sql("""CREATE TRIGGER qa_failure AFTER UPDATE ON bom_component
            WHEN NEW.usage_qty_base='999' BEGIN SELECT RAISE(ABORT,'bom forced rollback'); END""")
    before=snapshot(b["path"])
    assert client.put(base,json=edit_component(b,h,c,usage_qty_base="999")).status_code==409
    assert snapshot(b["path"])==before
    # Parent's version advance and pending create receipt also roll back on insert failure.
    with b["db"].write() as session:
        session.connection().exec_driver_sql("""CREATE TRIGGER qa_insert_failure AFTER INSERT ON bom_component
            WHEN NEW.usage_qty_base='888' BEGIN SELECT RAISE(ABORT,'bom forced create rollback'); END""")
    before=snapshot(b["path"])
    assert client.post(URL+"/"+h["id"]+"/components",json=component_data(b,h,qty="888")).status_code==409
    assert snapshot(b["path"])==before


def test_additive_migration_and_injected_ddl_failure_restore_exact_prior_schema(tmp_path,monkeypatch):
    from alembic import command
    from alembic.config import Config
    from backend.app.db import ROOT
    for fail in [False,True]:
        path=tmp_path/("failed.sqlite" if fail else "upgrade.sqlite")
        engine=make_engine(path);config=Config(str(ROOT/"backend/alembic.ini"))
        config.set_main_option("script_location",str(ROOT/"backend/migrations"))
        with engine.connect().execution_options(erp_write=True) as con:
            config.attributes["connection"]=con;command.upgrade(config,"0006_supplier_returns")
        engine.dispose();before=snapshot(path)
        if fail:
            migration=import_module("backend.migrations.versions.0007_product_bom")
            real=migration.op.create_index
            def injected(name,*args,**kwargs):
                if name=="ix_bom_component_material_id":raise RuntimeError("injected BOM DDL failure")
                return real(name,*args,**kwargs)
            with monkeypatch.context() as m:
                m.setattr(migration.op,"create_index",injected)
                with pytest.raises(RuntimeError,match="injected BOM DDL failure"):migrate(path)
            assert snapshot(path)==before
            with sqlite3.connect(path) as con:assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0]=="0006_supplier_returns"
        else:
            migrate(path);migrate(path);after=snapshot(path)
            assert all(after["rows"][t]==rows for t,rows in before["rows"].items())
            assert_issue_ledger_upgrade_objects(before,after)
            assert after["rows"]["bom"]==after["rows"]["bom_component"]==[]
            with sqlite3.connect(path) as con:
                from backend.app.stock_ledger import register_exact_sql
                register_exact_sql(con)
                assert con.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
                assert con.execute("PRAGMA foreign_key_check").fetchall()==[]


def test_bom_crud_never_changes_posted_receipts_returns_draft_or_stock(inquiry):
    r=inquiry;client=r["client"]
    r["products"]=[r["finished"]]
    before=snapshot(r["path"]);old_query=query(r);stock=client.get("/api/stock-positions").json()
    h=header(r);c=add(r,h)
    assert client.put(URL+"/"+h["id"]+"/components/"+c["id"],json=edit_component(r,h,c,usage_qty_base="7")).status_code==200
    assert client.get("/api/stock-positions").json()==stock
    assert query(r)==old_query
    after=snapshot(r["path"])
    assert all(after["rows"][t]==rows for t,rows in before["rows"].items() if t not in {"bom","bom_component","stock_create_receipt"})


def test_header_cas_duplicate_revision_and_component_stale_delete(bom):
    b=bom;client=b["client"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(lambda _:client.post(URL,json=data(b)).status_code,range(2)))
    assert sorted(statuses)==[201,409]
    h=client.get(URL).json()[0];body=edit_header(h,output_basis_qty="123456789012345678901234567890.1234567890123456789")
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(lambda _:client.put(URL+"/"+h["id"],json=body).status_code,range(2)))
    assert sorted(statuses)==[200,409]
    assert get(b,h)["output_basis_qty"]==body["output_basis_qty"]
    c=add(b,h);path=URL+"/"+h["id"]+"/components/"+c["id"]
    updated=client.put(path,json=edit_component(b,h,c,purpose_note="第二版")).json()
    before=snapshot(b["path"])
    assert client.delete(path,params={"row_version":1,"bom_version":get(b,h)["row_version"]}).status_code==409
    assert client.put(path,json=edit_component(b,h,updated,bom_version=1)).status_code==409
    assert snapshot(b["path"])==before


def test_raw_sql_insert_kind_fk_and_duplicate_line_constraints(bom):
    b=bom;h=header(b);c=add(b,h)
    inserts=[
        ("INSERT INTO bom(id,created_at,created_by,updated_at,row_version,product_id,revision,output_basis_qty,valid_from,valid_to,release_state,is_active) SELECT ?,created_at,created_by,updated_at,1,?,'WRONG','1',valid_from,NULL,'DRAFT',1 FROM bom WHERE id=?",(str(uuid4()),b["raws"][0]["id"],h["id"])),
        ("INSERT INTO bom_component(id,created_at,created_by,updated_at,row_version,bom_id,line_no,material_id,usage_qty_base,purpose_note) SELECT ?,created_at,created_by,updated_at,1,bom_id,99,?,'1',NULL FROM bom_component WHERE id=?",(str(uuid4()),h["product_id"],c["id"])),
        ("INSERT INTO bom_component(id,created_at,created_by,updated_at,row_version,bom_id,line_no,material_id,usage_qty_base,purpose_note) SELECT ?,created_at,created_by,updated_at,1,?,99,material_id,'1',NULL FROM bom_component WHERE id=?",(str(uuid4()),str(uuid4()),c["id"])),
        ("INSERT INTO bom_component(id,created_at,created_by,updated_at,row_version,bom_id,line_no,material_id,usage_qty_base,purpose_note) SELECT ?,created_at,created_by,updated_at,1,bom_id,line_no,material_id,'1',NULL FROM bom_component WHERE id=?",(str(uuid4()),c["id"]))]
    for sql,args in inserts:
        before=snapshot(b["path"])
        with pytest.raises(IntegrityError):
            with b["db"].write() as session:session.connection().exec_driver_sql(sql,args)
        assert snapshot(b["path"])==before
