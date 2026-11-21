"""Real, exact customer shipments on new isolated SQLite databases only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from fractions import Fraction
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT
from backend.app.local_demo import create_local_demo_app
from backend.app.main import create_app
from backend.tests.test_finished_goods_receipts import (seed_small, add_completion,
    create as receive_create, post as receive_post, snapshot)
from backend.tests.test_sales import (create as create_order, add as add_sales_line, get as get_order,
    header_edit, line_edit)

URL = "/api/sales-shipments"
PORT, UI_PORT = 8036, 5199
TABLES = ["sales_shipment", "sales_shipment_posting", "sales_shipment_entry"]


def client_for(app):
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))


def artifact_parent(tmp_path):
    if tmp_path.resolve().is_relative_to(ROOT / "artifacts"):
        return tmp_path
    parent = ROOT / "artifacts" / "round20-backend-tests"
    parent.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="shipment-", dir=parent))


def seed_fast(client):
    r = dict(client=client, **seed_small(client))
    currency = client.post("/api/currencies", json=dict(code="QA-CUR", name="隔離幣別")).json()
    r["customers"] = [client.post("/api/customers", json=dict(code="QA-C"+str(i), name="隔離客戶"+str(i),
        currency_id=currency["id"], is_active=True)).json() for i in range(2)]
    r["completions"] = [add_completion(r, "COMP-SHIP-A"), add_completion(r, "COMP-SHIP-B", order=1)]
    receipt = receive_create(r, "FG-SHIP-BASELINE", lot_code="LOT-SHIP-QA")
    response = receive_post(r, receipt)
    assert response.status_code == 201, response.text
    r["receipt"] = response.json()
    order = create_order(r, "SO-QA-SHIPMENT")
    add_sales_line(r, order, "1.25", "40")
    add_sales_line(r, order, "2.5", "7.25", 1)
    r["order"] = get_order(r, order)
    r["sales_lines"] = r["order"]["lines"]
    r["position"] = next(p for p in client.get("/api/stock-positions").json() if p["item_id"] == r["products"][0]["id"])
    assert r["position"]["on_hand_qty_base"] == "1.25"
    return r


@pytest.fixture
def shipping(tmp_path):
    path = artifact_parent(tmp_path) / "shipments.sqlite"
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        yield dict(seed_fast(client), app=app, db=app.state.db, path=path)


def source(r, line_id=None):
    line_id = line_id or r["sales_lines"][0]["id"]
    response = r["client"].get(URL + "/sources")
    assert response.status_code == 200, response.text
    return next(s for s in response.json() if s["source_line_id"] == line_id)


def body(r, number="SHIP-QA", **patch):
    line_id = patch.get("sales_order_line_id", r["sales_lines"][0]["id"])
    actual = source(r, line_id) if line_id in [s["source_line_id"] for s in r["client"].get(URL + "/sources").json()] else source(r)
    return {**dict(request_key=str(uuid4()), document_no=number, sales_order_line_id=line_id,
        line_version=actual["line_version"], order_version=actual["order_version"],
        position_id=r["position"]["id"], business_date="2026-11-20", quantity="0.75", note="獨立出貨 QA"), **patch}


def create(r, number="SHIP-QA", **patch):
    response = r["client"].post(URL, json=body(r, number, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def current(r, document):
    response = r["client"].get(URL + "/" + document["id"])
    assert response.status_code == 200, response.text
    return response.json()


def edit(row, **patch):
    return {**{k: row[k] for k in ["document_no", "sales_order_line_id", "line_version", "order_version",
        "position_id", "business_date", "quantity", "note", "row_version"]}, **patch}


def review(r, document):
    response = r["client"].get(URL + "/" + document["id"] + "/posting-review")
    assert response.status_code == 200, response.text
    return response.json()


def post_body(r, document, **patch):
    inspected = review(r, document)
    return {**dict(request_key=str(uuid4()), row_version=inspected["shipment"]["row_version"],
        confirmation_token=inspected["confirmation_token"]), **patch}


def post(r, document, data=None):
    return r["client"].post(URL + "/" + document["id"] + "/post", json=data if data is not None else post_body(r, document))


def stock(r, position=None):
    position = position or r["position"]
    return next(p for p in r["client"].get("/api/stock-positions").json() if p["id"] == position["id"])["on_hand_qty_base"]


def counts(r):
    with r["db"].read() as session:
        return {table: session.connection().exec_driver_sql("SELECT COUNT(*) FROM " + table).scalar()
            for table in TABLES + ["inventory_lot", "stock_position"]}


def test_partial_075_05_real_negative_entries_source_independence_and_unchanged_sale_sum(shipping):
    r=shipping; c=r["client"]; baseline=get_order(r,r["order"]); initial=counts(r)
    a=create(r); before=snapshot(r["path"]); inspected=review(r,a)
    assert snapshot(r["path"])==before
    assert (inspected["source"]["ordered_qty_base"],inspected["source"]["shipped_qty_base"],inspected["source"]["remaining_qty_base"])==("1.25","0","1.25")
    assert (inspected["current_stock_qty_base"],inspected["expected_stock_qty_base"],inspected["remaining_after_qty_base"])==("1.25","0.5","0.5")
    first=post(r,a); assert first.status_code==201,first.text
    saved=first.json(); assert saved["posting_state"]=="POSTED" and stock(r)=="0.5"
    assert len(saved["movements"])==1 and saved["movements"][0]["qty_base_delta"]=="-0.75"
    assert saved["movements"][0]["position_id"]==r["position"]["id"]
    assert saved["customer_id"]==r["customers"][0]["id"] and saved["sales_order_id"]==r["order"]["id"]
    assert (source(r)["shipped_qty_base"],source(r)["remaining_qty_base"])==("0.75","0.5")
    second_source=source(r,r["sales_lines"][1]["id"])
    assert (second_source["shipped_qty_base"],second_source["remaining_qty_base"])==("0","2.5")
    b=create(r,"SHIP-QA-B",quantity="0.5"); assert post(r,b).status_code==201
    assert stock(r)=="0" and (source(r)["shipped_qty_base"],source(r)["remaining_qty_base"])==("1.25","0")
    extra=create(r,"SHIP-QA-EXCESS",quantity="0.01"); unchanged=snapshot(r["path"])
    assert c.get(URL+"/"+extra["id"]+"/posting-review").status_code==409
    assert post(r,extra,dict(request_key=str(uuid4()),row_version=1,confirmation_token="invalid")).status_code==409
    assert snapshot(r["path"])==unchanged and get_order(r,r["order"])==baseline
    assert counts(r)["inventory_lot"]==initial["inventory_lot"] and counts(r)["stock_position"]==initial["stock_position"]
    with r["db"].read() as s:
        con=s.connection()
        assert con.exec_driver_sql("SELECT qty_base_delta,work_order_id FROM physical_stock_ledger WHERE domain='SALES_SHIPMENT' ORDER BY qty_base_delta").all()==[("-0.5",None),("-0.75",None)]
        assert con.exec_driver_sql("SELECT typeof(qty_base_delta) FROM sales_shipment_entry LIMIT 1").scalar()=="text"


def test_draft_crud_replay_search_delete_and_no_physical_mutation(shipping):
    r=shipping;c=r["client"]; initial=counts(r); data=body(r); a=c.post(URL,json=data).json()
    assert a["posting_state"]=="DRAFT" and a["movements"]==[] and stock(r)=="1.25"
    assert c.post(URL,json=data).status_code==200
    assert c.post(URL,json={**data,"quantity":"0.5"}).status_code==409
    assert c.post(URL,json=body(r)).status_code==409
    changed=c.put(URL+"/"+a["id"],json=edit(a,quantity="-0.125",note="草稿修訂"))
    assert changed.status_code==200 and changed.json()["quantity"]=="-0.125"
    assert len(c.get(URL,params=dict(q="草稿修訂")).json())==1 and c.get(URL,params=dict(q="%" )).json()==[]
    assert c.put(URL+"/"+a["id"],json=edit(a,quantity="0.5")).status_code==409
    assert c.delete(URL+"/"+a["id"],params=dict(row_version=1)).status_code==409
    assert c.delete(URL+"/"+a["id"],params=dict(row_version=2)).status_code==204
    assert c.post(URL,json=data).status_code==410
    assert counts(r)==initial and stock(r)=="1.25"


@pytest.mark.parametrize("quantity",["0","-0.5","-0.000000000000000000000000000000000000001"])
def test_signed_zero_drafts_cannot_post_or_become_undocumented_returns(shipping,quantity):
    r=shipping;a=create(r,quantity=quantity); before=snapshot(r["path"])
    assert r["client"].get(URL+"/"+a["id"]+"/posting-review").status_code==422
    assert post(r,a,dict(request_key=str(uuid4()),row_version=1,confirmation_token="forged")).status_code==422
    assert snapshot(r["path"])==before and stock(r)=="1.25"


@pytest.mark.parametrize("patch",[{"sales_order_line_id":str(uuid4())},{"position_id":str(uuid4())},{"document_no":" "},
    {"business_date":"2026-02-30"},{"quantity":0.75},{"quantity":"NaN"},{"quantity":"1e3"},
    {"customer_id":str(uuid4())},{"sales_order_id":str(uuid4())},{"product_id":str(uuid4())},
    {"unit_id":str(uuid4())},{"warehouse_id":str(uuid4())},{"lot_id":str(uuid4())},{"posting_state":"POSTED"}])
def test_invalid_sources_and_client_identity_overrides_leave_database_unchanged(shipping,patch):
    r=shipping;a=create(r);before=snapshot(r["path"]);b=body(r,"INVALID");b.update(patch)
    assert r["client"].post(URL,json=b).status_code==422
    assert r["client"].put(URL+"/"+a["id"],json=edit(a,**patch)).status_code==422
    assert snapshot(r["path"])==before


def test_insufficient_exact_position_stock_and_no_stock_product_reject_without_deduction(shipping):
    r=shipping;c=r["client"]; line=r["sales_lines"][0]
    changed=c.put("/api/sales-orders/"+r["order"]["id"]+"/lines/"+line["id"],json=line_edit(r,r["order"],line,ordered_qty="4"))
    assert changed.status_code==200
    # More stock genuinely exists in a different warehouse, but cannot be
    # borrowed by the selected position's deduction.
    extra_source=add_completion(r,"COMP-OTHER-WAREHOUSE")
    other_receipt=c.post("/api/finished-goods-receipts",json=dict(request_key=str(uuid4()),document_no="FG-OTHER-WAREHOUSE",
        completion_id=extra_source["id"],completion_version=extra_source["row_version"],warehouse_id=r["warehouse2"]["id"],
        lot_id=None,lot_code="LOT-OTHER-WAREHOUSE",business_date="2026-11-20",quantity="0.75",note=None))
    assert other_receipt.status_code==201,other_receipt.text
    assert receive_post(r,other_receipt.json()).status_code==201
    a=create(r,quantity="1.5");before=snapshot(r["path"])
    assert c.get(URL+"/"+a["id"]+"/posting-review").status_code==409
    assert snapshot(r["path"])==before and stock(r)=="1.25"
    no_stock=r["sales_lines"][1]; positions=c.get(URL+"/positions",params=dict(sales_order_line_id=no_stock["id"])).json()
    assert positions==[]
    assert c.post(URL,json=body(r,"WRONG-PRODUCT",sales_order_line_id=no_stock["id"])).status_code==422
    assert stock(r)=="1.25" and counts(r)["sales_shipment_entry"]==0


def test_multiple_order_lines_same_product_have_independent_caps_and_lot_stock(shipping):
    r=shipping;c=r["client"];other=create_order(r,"SO-SHIP-OTHER",customer_id=r["customers"][1]["id"])
    otherline=add_sales_line(r,other,"0.75","12")
    a=create(r);assert post(r,a).status_code==201
    b=create(r,"OTHER-SHIP",sales_order_line_id=otherline["id"],quantity="0.5");assert post(r,b).status_code==201
    assert (source(r)["shipped_qty_base"],source(r)["remaining_qty_base"])==("0.75","0.5")
    assert (source(r,otherline["id"])["shipped_qty_base"],source(r,otherline["id"])["remaining_qty_base"])==("0.5","0.25")
    assert stock(r)=="0" and get_order(r,r["order"])["total_amount"]=="68.125"
    assert get_order(r,other)["total_amount"]=="9"


def test_concurrent_source_exhaustion_and_shared_stock_never_go_negative(shipping):
    r=shipping;a=create(r,"COMPETE-A",quantity="0.75");b=create(r,"COMPETE-B",quantity="0.75")
    bodies=[post_body(r,a),post_body(r,b)]
    with ThreadPoolExecutor(2) as pool:responses=list(pool.map(lambda pair:post(r,*pair),zip([a,b],bodies)))
    assert sorted(x.status_code for x in responses)==[201,409],[x.text for x in responses]
    assert stock(r)=="0.5" and source(r)["shipped_qty_base"]=="0.75"
    assert counts(r)["sales_shipment_entry"]==1


def test_concurrent_different_orders_compete_for_same_lot_without_pool_mixing(shipping):
    r=shipping;other=create_order(r,"SO-COMPETE",customer_id=r["customers"][1]["id"]);l=add_sales_line(r,other,"1.25","1")
    a=create(r,"STOCK-A",quantity="0.75");b=create(r,"STOCK-B",sales_order_line_id=l["id"],quantity="0.75")
    bodies=[post_body(r,a),post_body(r,b)]
    with ThreadPoolExecutor(2) as pool:responses=list(pool.map(lambda pair:post(r,*pair),zip([a,b],bodies)))
    assert sorted(x.status_code for x in responses)==[201,409]
    assert stock(r)=="0.5" and sorted([source(r)["shipped_qty_base"],source(r,l["id"])["shipped_qty_base"]])==["0","0.75"]


def test_repeated_original_submission_response_loss_and_new_runtime_replay_exactly_once(shipping):
    r=shipping;a=create(r);data=post_body(r,a)
    with ThreadPoolExecutor(2) as pool:responses=list(pool.map(lambda _:post(r,a,data),range(2)))
    assert sorted(x.status_code for x in responses)==[200,201]
    first=responses[0].json();before=snapshot(r["path"])
    assert post(r,a,data).status_code==200
    app=create_local_demo_app(r["path"],backend_port=PORT,frontend_port=UI_PORT)
    with client_for(app) as restarted:
        replay=restarted.post(URL+"/"+a["id"]+"/post",json=data)
        assert replay.status_code==200 and replay.json()["posting_id"]==first["posting_id"]
    with TestClient(create_app(r["path"])) as ordinary:
        assert not ordinary.get(URL+"/policy").json()["posting_allowed"]
        replay=ordinary.post(URL+"/"+a["id"]+"/post",json=data)
        assert replay.status_code==200 and replay.json()["posting_id"]==first["posting_id"]
    assert snapshot(r["path"])==before and stock(r)=="0.5" and counts(r)["sales_shipment_entry"]==1
    assert post(r,a,{**data,"request_key":str(uuid4())}).status_code==409
    assert post(r,a,{**data,"confirmation_token":"changed"}).status_code==409


def test_ordinary_runtime_flags_foreign_tokens_headers_and_expiry_do_not_authorize(shipping,monkeypatch):
    from backend.app import sales_shipment_authorization as auth
    r=shipping;a=create(r);data=post_body(r,a);before=snapshot(r["path"])
    with TestClient(create_app(r["path"])) as ordinary:
        assert ordinary.post(URL+"/"+a["id"]+"/post",json=data,headers={"X-Local-Demo":"true"}).status_code==403
    for headers in [{"Origin":"https://foreign.invalid"},{"Host":"foreign.invalid"}]:
        assert r["client"].post(URL+"/"+a["id"]+"/post",json=data,headers=headers).status_code==403
    assert post(r,a,{**data,"confirmation_token":"forged"}).status_code==403
    actual=auth.observed_time
    monkeypatch.setattr(auth,"observed_time",lambda:actual()+timedelta(minutes=11))
    assert post(r,a,data).status_code==409
    monkeypatch.setattr(auth,"observed_time",actual)
    app=create_local_demo_app(r["path"],backend_port=PORT,frontend_port=UI_PORT)
    with client_for(app) as other:assert other.post(URL+"/"+a["id"]+"/post",json=data).status_code==403
    assert snapshot(r["path"])==before and stock(r)=="1.25"


def test_stale_source_and_draft_review_reject_then_refresh_without_rewriting_source(shipping):
    r=shipping;c=r["client"];a=create(r);data=post_body(r,a);old_order=get_order(r,r["order"])
    change=c.put("/api/sales-orders/"+old_order["id"],json=header_edit(old_order,note="合法備註"))
    assert change.status_code==200
    before=snapshot(r["path"]);assert post(r,a,data).status_code==409 and snapshot(r["path"])==before
    s=source(r);response=c.put(URL+"/"+a["id"],json=edit(a,order_version=s["order_version"],line_version=s["line_version"]))
    assert response.status_code==200
    data=post_body(r,response.json());saved=c.put(URL+"/"+a["id"],json=edit(response.json(),note="變更出貨草稿"))
    assert saved.status_code==200
    assert post(r,saved.json(),data).status_code==409 and stock(r)=="1.25"


@pytest.mark.parametrize("stage",["entry","finalize"])
def test_injected_entry_or_late_finalize_failure_rolls_back_stock_state_versions_audit_key(shipping,stage):
    r=shipping;a=create(r);data=post_body(r,a)
    sql=("CREATE TRIGGER qa_shipment_failure BEFORE INSERT ON sales_shipment_entry BEGIN SELECT RAISE(ABORT,'injected shipment entry failure'); END"
        if stage=="entry" else "CREATE TRIGGER qa_shipment_failure BEFORE UPDATE ON sales_shipment_posting WHEN NEW.posting_state='POSTED' BEGIN SELECT RAISE(ABORT,'injected late shipment finalization failure'); END")
    with r["db"].write() as session:session.connection().exec_driver_sql(sql)
    before=snapshot(r["path"]);response=post(r,a,data)
    assert response.status_code==409,response.text
    assert snapshot(r["path"])==before and stock(r)=="1.25" and counts(r)["sales_shipment_entry"]==0
    with r["db"].write() as session:session.connection().exec_driver_sql("DROP TRIGGER qa_shipment_failure")
    assert post(r,a,data).status_code==201 and stock(r)=="0.5"


def test_posted_shipment_posting_entry_and_lineage_resist_api_sql_replace(shipping):
    r=shipping;a=create(r);response=post(r,a);assert response.status_code==201
    posted=response.json();before=snapshot(r["path"])
    assert r["client"].put(URL+"/"+a["id"],json=edit(posted,note="forbidden")).status_code==409
    assert r["client"].delete(URL+"/"+a["id"],params=dict(row_version=posted["row_version"])).status_code==409
    for table in TABLES:
        for action in ["UPDATE","DELETE","REPLACE"]:
            with pytest.raises(IntegrityError),r["db"].write() as session:
                con=session.connection();con.exec_driver_sql("PRAGMA recursive_triggers=OFF")
                columns=",".join(x[1] for x in con.exec_driver_sql("PRAGMA table_info("+table+")"))
                sql=("UPDATE "+table+" SET created_by=created_by" if action=="UPDATE" else "DELETE FROM "+table if action=="DELETE" else "INSERT OR REPLACE INTO "+table+" ("+columns+") SELECT "+columns+" FROM "+table+" LIMIT 1")
                con.exec_driver_sql(sql)
            assert snapshot(r["path"])==before
    assert stock(r)=="0.5"


def test_posted_source_identity_and_quantity_floor_protected_but_safe_draft_edits_work(shipping):
    r=shipping;c=r["client"];a=create(r);assert post(r,a).status_code==201
    order=get_order(r,r["order"]);line=order["lines"][0];otherline=order["lines"][1]
    for patch in [dict(ordered_qty="0.5"),dict(item_id=r["products"][1]["id"]),dict(unit_id=str(uuid4()))]:
        before=snapshot(r["path"])
        response=c.put("/api/sales-orders/"+order["id"]+"/lines/"+line["id"],json=line_edit(r,order,line,**patch))
        assert response.status_code in {409,422} and snapshot(r["path"])==before
    assert c.delete("/api/sales-orders/"+order["id"]+"/lines/"+line["id"],params=dict(row_version=line["row_version"],order_version=order["row_version"])).status_code==409
    assert c.put("/api/sales-orders/"+order["id"],json=header_edit(order,customer_id=r["customers"][1]["id"])).status_code==409
    allowed=c.put("/api/sales-orders/"+order["id"]+"/lines/"+line["id"],json=line_edit(r,order,line,ordered_qty="2",unit_price="44"))
    assert allowed.status_code==200,allowed.text
    assert (source(r)["shipped_qty_base"],source(r)["remaining_qty_base"])==("0.75","1.25")
    updated=get_order(r,order);assert c.put("/api/sales-orders/"+order["id"],json=header_edit(updated,note="合法後續備註")).status_code==200
    updated=get_order(r,order);new_other=next(l for l in updated["lines"] if l["id"]==otherline["id"])
    assert c.put("/api/sales-orders/"+order["id"]+"/lines/"+otherline["id"],json=line_edit(r,updated,new_other,unit_price="8")).status_code==200
    with pytest.raises(IntegrityError),r["db"].write() as session:session.connection().exec_driver_sql("UPDATE sales_order_line SET ordered_qty='0.5' WHERE id=?",(line["id"],))
    with pytest.raises(IntegrityError),r["db"].write() as session:session.connection().exec_driver_sql("UPDATE sales_order SET customer_id=? WHERE id=?",(r["customers"][1]["id"],order["id"]))
    assert current(r,a)["customer_id"]==r["customers"][0]["id"] and stock(r)=="0.5"


def test_exact_high_precision_and_unbounded_draft_are_not_silently_rounded(shipping):
    r=shipping;huge="9"*5000+".123456789012345678901234567890123456789"
    draft=create(r,"HUGE-DRAFT",quantity=huge);assert draft["quantity"]==huge
    quantity="0.000000000000000000000000000000000000001"
    a=create(r,"TINY-SHIP",quantity=quantity);response=post(r,a);assert response.status_code==201,response.text
    assert response.json()["movements"][0]["qty_base_delta"]=="-"+quantity
    assert Fraction(stock(r))==Fraction("1.25")-Fraction(quantity)
    assert source(r)["shipped_qty_base"]==quantity


def test_no_new_lot_or_position_including_genuine_untracked_product(shipping):
    r=shipping;receipt=receive_create(r,"UNTRACKED-STOCK",source_index=1,quantity="0.5",lot_code=None)
    assert receive_post(r,receipt).status_code==201
    position=next(p for p in r["client"].get("/api/stock-positions").json() if p["item_id"]==r["products"][1]["id"])
    assert position["lot_id"] is None
    initial=counts(r);a=create(r,"UNTRACKED-SHIP",sales_order_line_id=r["sales_lines"][1]["id"],position_id=position["id"],quantity="0.25")
    assert post(r,a).status_code==201 and stock(r,position)=="0.25"
    assert counts(r)["inventory_lot"]==initial["inventory_lot"] and counts(r)["stock_position"]==initial["stock_position"]


def test_default_sql_cannot_forge_posting_or_negative_entry_without_operation_grant(shipping):
    r=shipping;a=create(r);posted=post(r,a);assert posted.status_code==201
    b=create(r,"FORGED-TARGET",quantity="0.25");before=snapshot(r["path"])
    with r["db"].read() as session:
        con=session.connection()
        assert tuple(con.exec_driver_sql("SELECT erp_sales_shipment_authorized(),erp_sales_shipment_document(),erp_sales_shipment_digest(),erp_sales_shipment_snapshot(),erp_sales_shipment_time()").one())==(0,None,None,None,None)
    with pytest.raises(IntegrityError),r["db"].write() as session:
        con=session.connection()
        con.exec_driver_sql("UPDATE sales_shipment SET row_version=row_version+1 WHERE id=?",(b["id"],))
        columns=[x[1] for x in con.exec_driver_sql("PRAGMA table_info(sales_shipment_posting)")]
        row=list(con.exec_driver_sql("SELECT * FROM sales_shipment_posting LIMIT 1").one())
        values=dict(zip(columns,row));values.update(id=str(uuid4()),shipment_id=b["id"],idempotency_key=str(uuid4()),posting_state="PREPARING",source_version=1)
        con.exec_driver_sql("INSERT INTO sales_shipment_posting ("+",".join(columns)+") VALUES ("+",".join("?" for _ in columns)+")",tuple(values[c] for c in columns))
    assert snapshot(r["path"])==before


@pytest.mark.parametrize("delta",["0","0.75","-1"])
def test_authorized_entry_cannot_substitute_zero_positive_or_wrong_negative_leg(shipping,delta):
    from backend.app.sales_shipment_models import SalesShipmentEntry
    r=shipping;a=create(r);data=post_body(r,a);before=snapshot(r["path"])
    def alter(mapper,connection,target):target.qty_base_delta=delta
    event.listen(SalesShipmentEntry,"before_insert",alter)
    try:
        response=post(r,a,data)
        assert response.status_code==409,response.text
    finally:event.remove(SalesShipmentEntry,"before_insert",alter)
    assert snapshot(r["path"])==before and stock(r)=="1.25"


def test_full_real_chain_preserves_all_original_rows_mrp_raw_stock_and_cross_domain_keys(tmp_path):
    from backend.tests.seed_sales_shipments_browser import seed
    from backend.tests.test_mrp import preview
    path=artifact_parent(tmp_path)/"full-real-chain.sqlite"
    metadata=seed(path)
    app=create_local_demo_app(path,backend_port=PORT,frontend_port=UI_PORT)
    with client_for(app) as client:
        r=dict(metadata,client=client,app=app,db=app.state.db,path=path)
        raw_position=next(p for p in client.get("/api/stock-positions").json() if p["item_id"]==r["raws"][0]["id"])
        assert client.post(URL,json=body(r,"RAW-POSITION",position_id=raw_position["id"])).status_code==422
        resources=["purchase-orders","purchase-outstanding","purchase-receipts","supplier-returns","work-orders","boms","routings",
            "operation-definitions","material-issues","material-returns","production-completions","sales-orders"]
        projections={name:client.get("/api/"+name).json() for name in resources}
        old_mrp=preview(r);before=snapshot(path);initial=counts(r)
        document=create(r);request=post_body(r,document);result=post(r,document,request)
        assert result.status_code==201,result.text
        after=snapshot(path)
        assert all(after["rows"][t]==rows for t,rows in before["rows"].items() if t not in TABLES+["stock_create_receipt"])
        assert all(client.get("/api/"+name).json()==old for name,old in projections.items())
        new_mrp=preview(r)
        assert (new_mrp["orders"],new_mrp["materials"])==(old_mrp["orders"],old_mrp["materials"])
        assert stock(r)=="0.5" and stock(r,raw_position)=="3"
        assert counts(r)["inventory_lot"]==initial["inventory_lot"] and counts(r)["stock_position"]==initial["stock_position"]
        key_tables=["stock_posting","supplier_return_posting","material_issue_posting","material_return_posting","finished_goods_posting"]
        for index,table in enumerate(key_tables):
            with r["db"].read() as session:key=session.connection().exec_driver_sql("SELECT idempotency_key FROM "+table+" LIMIT 1").scalar_one()
            extra=create(r,"CROSS-KEY-"+str(index),quantity="0.01");unchanged=snapshot(path)
            rejected=post(r,extra,post_body(r,extra,request_key=key))
            assert rejected.status_code==409,(table,rejected.text)
            assert snapshot(path)==unchanged
        # Reverse direction: a genuine FG receipt cannot reuse the successful
        # shipment key. Its still-unused confirmed completion remains eligible.
        completion=r["completions"][2]
        other=client.post("/api/finished-goods-receipts",json=dict(request_key=str(uuid4()),document_no="REVERSE-CROSS-KEY",
            completion_id=completion["id"],completion_version=completion["row_version"],warehouse_id=r["warehouse"]["id"],
            lot_id=r["position"]["lot_id"],lot_code=r["position"]["lot_code"],business_date="2026-11-20",quantity="0.25",note=None))
        assert other.status_code==201,other.text
        reviewed=client.get("/api/finished-goods-receipts/"+other.json()["id"]+"/posting-review")
        assert reviewed.status_code==200,reviewed.text
        unchanged=snapshot(path)
        denied=client.post("/api/finished-goods-receipts/"+other.json()["id"]+"/post",json=dict(request_key=request["request_key"],
            row_version=other.json()["row_version"],confirmation_token=reviewed.json()["confirmation_token"]))
        assert denied.status_code==409,denied.text
        assert snapshot(path)==unchanged
