from backend.tests.finished_goods_migration_assertions import assert_finished_goods_upgrade_objects
"""Supplier returns against genuine immutable receipts; every database is isolated."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.db import DEMO_ACTOR_ID, ROOT, make_engine, migrate
from backend.app.decimal_text import exact_difference, exact_sum
from backend.app.main import create_app
from backend.tests.test_foundation import setup
from backend.tests.test_purchasing import po, create_header, add_line, get
from backend.tests.test_receiving import header as receipt_header, line as receipt_line, post as receipt_post
from backend.tests.test_supplier_offers import snapshot

URL = "/api/supplier-returns"
NEW = ["supplier_return", "supplier_return_line", "supplier_return_posting", "supplier_return_entry"]
FROZEN = ["purchase_order", "purchase_order_line", "stock_document", "stock_document_line", "stock_posting", "stock_entry", "stock_position", "inventory_lot"]


@pytest.fixture
def returns(po):
    r = po
    raw = r["raws"][1]
    body = {k: raw[k] for k in ["code", "name", "specification", "category_id", "base_unit_id", "is_active", "row_version"]}
    response = r["client"].put("/api/raw-materials/"+raw["id"], json={**body, "lot_tracking": False})
    assert response.status_code == 200, response.text
    r["warehouse2"] = r["client"].post("/api/warehouses", json={"code": "QA-W2", "name": "另一倉", "is_active": True, "mrp_included": False}).json()
    a, b = create_header(r, "PO-TEST-001"), create_header(r, "PO-TEST-002")
    r["sources"] = [add_line(r, a, "12", "13"), add_line(r, b, "5", "7.25", item=1)]
    r["order"], r["other_order"] = get(r, a), get(r, b)
    assert r["order"]["total_amount"] == "156" and r["other_order"]["total_amount"] == "36.25"
    r["receipts"], r["origins"] = [], []
    for no, qty in [("RCV-TEST-001", "6"), ("RCV-TEST-002", "4")]:
        doc = receipt_header(r, no)
        row = receipt_line(r, doc, qty, lot_code="LOT-R8-001", supplier_lot_code="SUPPLIER-REAL-LOT")
        result = receipt_post(r, doc)
        assert result.status_code == 201, result.text
        r["receipts"].append(result.json()); r["origins"].append(row)
    r["frozen"] = frozen(r)
    r["original_orders"] = [get(r, a), get(r, b)]
    assert balance(r) == "10"
    return r


def frozen(r):
    with r["db"].read() as session:
        return {t: list(session.connection().exec_driver_sql("SELECT * FROM "+t+" ORDER BY 1")) for t in FROZEN}


def counts(r):
    with r["db"].read() as session:
        return {t: session.connection().exec_driver_sql("SELECT count(*) FROM "+t).scalar() for t in NEW}


def stock(r):
    response = r["client"].get("/api/stock-positions")
    assert response.status_code == 200, response.text
    return response.json()


def balance(r):
    return next(p["on_hand_qty_base"] for p in stock(r) if p["item_id"] == r["raws"][0]["id"] and p["warehouse_id"] == r["warehouse"]["id"] and p["lot_code"] == "LOT-R8-001")


def read(r, doc):
    response = r["client"].get(URL+"/"+doc["id"])
    assert response.status_code == 200, response.text
    return response.json()


def header_body(r, no="RTN-A", **patch):
    return {"request_key": str(uuid4()), "document_no": no, "supplier_id": r["suppliers"][0]["id"],
            "business_date": "2026-11-19", "reason_code": None, "external_reference": None, "note": "原始退貨草稿", **patch}


def header(r, no="RTN-A", **patch):
    response = r["client"].post(URL, json=header_body(r, no, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def line_body(r, doc, qty="3", origin=0, **patch):
    return {"request_key": str(uuid4()), "origin_line_id": r["origins"][origin]["id"], "quantity": qty,
            "note": None, "document_version": read(r, doc)["row_version"], **patch}


def line(r, doc, qty="3", origin=0, **patch):
    response = r["client"].post(URL+"/"+doc["id"]+"/lines", json=line_body(r, doc, qty, origin, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def post(r, doc, body=None):
    return r["client"].post(URL+"/"+doc["id"]+"/post", json=body or {"request_key": str(uuid4()), "row_version": read(r, doc)["row_version"]})


def edit_header(row, **patch):
    return {**{k: row[k] for k in ["document_no", "supplier_id", "business_date", "reason_code", "external_reference", "note", "row_version"]}, **patch}


def edit_line(r, doc, row, **patch):
    return {**{k: row[k] for k in ["origin_line_id", "quantity", "note", "row_version"]}, "document_version": read(r, doc)["row_version"], **patch}


def assert_originals(r):
    assert frozen(r) == r["frozen"]
    assert [get(r, r["order"]), get(r, r["other_order"])] == r["original_orders"]
    context = r["client"].get("/api/purchase-receipts/source-orders/"+r["order"]["id"]).json()["lines"][0]
    assert (context["ordered_qty_base"], context["received_qty_base"], context["remaining_qty_base"]) == ("12", "10", "2")


def test_real_return_3_draft_zero_posted_7_gross_unchanged_and_restart(returns):
    r = returns; a = header(r); row = line(r, a)
    assert balance(r) == "10" and counts(r)["supplier_return_entry"] == 0
    assert (row["source_quantity_base"], row["returned_qty_base"], row["returnable_qty_base"]) == ("6", "0", "6")
    response = post(r, a)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["posting_state"] == "POSTED" and len(saved["movements"]) == 1
    assert saved["movements"][0]["qty_base_delta"] == "-3"
    assert saved["movements"][0]["origin_line_id"] == r["origins"][0]["id"]
    assert balance(r) == "7"
    assert (saved["lines"][0]["returned_qty_base"], saved["lines"][0]["returnable_qty_base"]) == ("3", "3")
    assert_originals(r)
    migrate(r["path"])
    with TestClient(create_app(r["path"])) as reopened:
        assert reopened.get(URL+"/"+a["id"]).json() == saved
        assert reopened.get("/api/stock-positions").json() == stock(r)
    assert_originals(r)
    with r["db"].read() as session:
        c = session.connection()
        assert c.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(c.exec_driver_sql("PRAGMA foreign_key_check")) == []
        assert set(c.exec_driver_sql("SELECT typeof(quantity) FROM supplier_return_line")) == {("text",)}
        assert set(c.exec_driver_sql("SELECT typeof(qty_base_delta) FROM supplier_return_entry")) == {("text",)}
        assert c.exec_driver_sql("SELECT count(*) FROM stock_posting").scalar() == 2
        assert sorted(c.exec_driver_sql("SELECT qty_base_delta FROM stock_entry").scalars()) == ["4", "6"]


def test_partial_multiple_documents_receipts_and_origin_remaining_limit(returns):
    r = returns
    for no, qty, origin in [("RTN-1", "3", 0), ("RTN-2", "2.5", 0), ("RTN-3", "1.25", 1)]:
        a = header(r, no); line(r, a, qty, origin); response = post(r, a)
        assert response.status_code == 201, response.text
    assert balance(r) == "3.25"
    context = r["client"].get(URL+"/source-receipts/"+r["receipts"][0]["id"]).json()
    assert context["lines"][0]["returned_qty_base"] == "5.5" and context["lines"][0]["returnable_qty_base"] == "0.5"
    a = header(r, "TOO-MUCH"); line(r, a, "0.500000000000000000000000000000000001")
    before, previous = counts(r), read(r,a)
    assert post(r,a).status_code in [409,422]
    assert balance(r) == "3.25" and counts(r) == before and read(r,a) == previous
    assert_originals(r)


def test_duplicate_origin_lines_in_one_draft_are_aggregated_before_posting(returns):
    r=returns; a=header(r); line(r,a,"3"); line(r,a,"4")
    before=counts(r); response=post(r,a)
    assert response.status_code in [409,422], response.text
    assert counts(r)==before and balance(r)=="10"
    assert read(r,a)["posting_state"] == "DRAFT"
    b=header(r,"VALID-SAME-ORIGIN"); line(r,b,"2.75"); line(r,b,"3.25")
    assert post(r,b).status_code == 201
    assert balance(r)=="4" and len(read(r,b)["movements"])==2
    assert_originals(r)


def test_request_retries_global_keys_duplicate_numbers_and_concurrent_post(returns):
    r=returns; data=header_body(r)
    responses=[r["client"].post(URL,json=data) for _ in range(2)]
    assert [x.status_code for x in responses] == [201,200]
    a=responses[0].json()
    assert r["client"].post(URL,json={**data,"note":"changed"}).status_code == 409
    assert r["client"].post(URL,json=header_body(r)).status_code == 409
    body=line_body(r,a); target=URL+"/"+a["id"]+"/lines"
    assert r["client"].post(target,json=body).status_code == 201
    assert r["client"].post(target,json=body).status_code == 200
    assert r["client"].post(target,json={**body,"quantity":"4"}).status_code == 409
    key={"request_key":str(uuid4()),"row_version":read(r,a)["row_version"]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:post(r,a,key),range(4)))
    assert sorted(x.status_code for x in results)==[200,200,200,201], [x.text for x in results]
    assert len({x.json()["posting_id"] for x in results})==1
    assert post(r,a,key).status_code==200 and balance(r)=="7"
    assert post(r,a,{**key,"request_key":str(uuid4())}).status_code==200
    b=header(r,"SECOND"); line(r,b,"1")
    assert post(r,b,{**key,"row_version":read(r,b)["row_version"]}).status_code==409
    assert counts(r)["supplier_return_entry"]==1
    assert_originals(r)


def test_concurrent_competing_returns_serialize_source_and_position_checks(returns):
    r=returns; docs=[header(r,"COMPETE-"+str(i)) for i in range(2)]
    for doc in docs:line(r,doc,"4")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda doc:post(r,doc),docs))
    assert sorted(x.status_code for x in responses) in [[201,409],[201,422]], [x.text for x in responses]
    assert balance(r)=="6" and counts(r)["supplier_return_entry"]==1
    assert sorted(read(r,doc)["posting_state"] for doc in docs)==["DRAFT","POSTED"]
    assert_originals(r)


def test_concurrent_distinct_origins_share_one_real_position_without_double_deduction(returns):
    r=returns;docs=[header(r,"DISTINCT-"+str(i)) for i in range(2)]
    line(r,docs[0],"6",origin=0);line(r,docs[1],"4",origin=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda doc:post(r,doc),docs))
    assert [x.status_code for x in responses]==[201,201], [x.text for x in responses]
    assert balance(r)=="0" and len(stock(r))==1 and counts(r)["supplier_return_entry"]==2
    assert_originals(r)


def test_material_warehouse_lot_isolation_and_nontracked_origin_without_fake_lot(returns):
    r=returns
    another=create_header(r,"PO-OTHER-LOT")
    source=add_line(r,another,"10","13")
    tracked=receipt_header(r,"OTHER-POSITIONS",purchase_order_id=another["id"])
    lot_b=receipt_line(r,tracked,"1",purchase_order_line_id=source["id"],lot_code="LOT-OTHER",supplier_lot_code="REAL-OTHER")
    warehouse_b=receipt_line(r,tracked,"2",purchase_order_line_id=source["id"],warehouse_id=r["warehouse2"]["id"],lot_code="LOT-R8-001",supplier_lot_code="SUPPLIER-REAL-LOT")
    assert receipt_post(r,tracked).status_code==201
    untracked=receipt_header(r,"NONTRACKED-ORIGIN",purchase_order_id=r["other_order"]["id"])
    non=receipt_line(r,untracked,"2",source=1,warehouse_id=r["warehouse2"]["id"])
    assert receipt_post(r,untracked).status_code==201
    before=frozen(r);a=header(r)
    for origin,qty in [(r["origins"][0],"3"),(lot_b,"0.5"),(warehouse_b,"1"),(non,"0.125")]:line(r,a,qty,origin_line_id=origin["id"])
    result=post(r,a);assert result.status_code==201,result.text
    assert len(result.json()["movements"])==4 and frozen(r)==before
    values={(p["item_id"],p["warehouse_id"],p["lot_code"]):p["on_hand_qty_base"] for p in stock(r)}
    assert values[(r["raws"][0]["id"],r["warehouse"]["id"],"LOT-R8-001")]=="7"
    assert values[(r["raws"][0]["id"],r["warehouse"]["id"],"LOT-OTHER")]=="0.5"
    assert values[(r["raws"][0]["id"],r["warehouse2"]["id"],"LOT-R8-001")]=="1"
    assert values[(r["raws"][1]["id"],r["warehouse2"]["id"],None)]=="1.875"
    assert next(p for p in stock(r) if p["item_id"]==r["raws"][1]["id"])["lot_id"] is None
    assert [get(r,r["order"]),get(r,r["other_order"])]==r["original_orders"]


def test_concurrent_duplicate_document_numbers_are_database_unique(returns):
    r=returns
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:r["client"].post(URL,json=header_body(r)),range(2)))
    assert sorted(x.status_code for x in results)==[201,409]
    assert counts(r)["supplier_return"]==1 and balance(r)=="10"


@pytest.mark.parametrize("stage",["second_entry","finalize"])
def test_injected_failure_rolls_back_all_return_state_and_deductions(returns,stage):
    r=returns; a=header(r); line(r,a,"2"); line(r,a,"1",origin=1)
    before, state, positions = counts(r),read(r,a),stock(r)
    with r["db"].write() as session:
        if stage=="second_entry":
            session.connection().exec_driver_sql("CREATE TRIGGER injected_return_failure BEFORE INSERT ON supplier_return_entry WHEN (SELECT count(*) FROM supplier_return_entry)>0 BEGIN SELECT RAISE(ABORT,'forced return movement rollback'); END")
        else:
            session.connection().exec_driver_sql("CREATE TRIGGER injected_return_failure BEFORE UPDATE OF posting_state ON supplier_return_posting BEGIN SELECT RAISE(ABORT,'forced return finalize rollback'); END")
    result=post(r,a)
    assert result.status_code==409, result.text
    assert counts(r)==before and read(r,a)==state and stock(r)==positions
    assert_originals(r)
    with r["db"].write() as session:session.connection().exec_driver_sql("DROP TRIGGER injected_return_failure")
    assert post(r,a).status_code==201 and balance(r)=="7"


@pytest.mark.parametrize("patch",[{"supplier_id":str(uuid4())},{"business_date":"2026-02-30"},{"business_date":0},
    {"document_no":" "},{"approval_state":"APPROVED"},{"posting_state":"POSTED"},{"id":str(uuid4())}])
def test_invalid_headers_and_system_overrides_leave_stock_unchanged(returns,patch):
    r=returns
    assert r["client"].post(URL,json=header_body(r,**patch)).status_code==422
    assert counts(r)=={t:0 for t in NEW} and balance(r)=="10"
    assert_originals(r)


@pytest.mark.parametrize("patch",[{"origin_line_id":str(uuid4())},{"quantity":0.1},{"quantity":"NaN"},{"quantity":"1e2"},
    {"quantity":True},{"quantity":"1,000"},{"warehouse_id":str(uuid4())},{"lot_id":str(uuid4())},
    {"item_id":str(uuid4())},{"position_id":str(uuid4())},{"origin_line_version":999}])
def test_invalid_source_quantity_or_editable_provenance_rejected(returns,patch):
    r=returns;a=header(r);before=counts(r);state=read(r,a)
    response=r["client"].post(URL+"/"+a["id"]+"/lines",json=line_body(r,a,**patch))
    assert response.status_code==422,response.text
    assert counts(r)==before and read(r,a)==state and balance(r)=="10"


def test_source_eligibility_cross_supplier_and_stale_versions(returns):
    r=returns
    draft=receipt_header(r,"DRAFT-SOURCE");draftline=receipt_line(r,draft,"1",lot_code="LOT-R8-001",supplier_lot_code="SUPPLIER-REAL-LOT")
    a=header(r)
    assert r["client"].post(URL+"/"+a["id"]+"/lines",json=line_body(r,a,origin_line_id=draftline["id"])).status_code in [409,422]
    wrong=header(r,"WRONG-SUPPLIER",supplier_id=r["suppliers"][1]["id"])
    assert r["client"].post(URL+"/"+wrong["id"]+"/lines",json=line_body(r,wrong)).status_code in [409,422]
    assert r["client"].get(URL+"/source-receipts",params={"supplier_id":r["suppliers"][1]["id"]}).json()==[]
    eligible=r["client"].get(URL+"/source-receipts",params={"supplier_id":r["suppliers"][0]["id"]}).json()
    assert {x["document_no"] for x in eligible}=={"RCV-TEST-001","RCV-TEST-002"}
    row=line(r,a)
    assert r["client"].put(URL+"/"+a["id"],json=edit_header(read(r,a),supplier_id=r["suppliers"][1]["id"])).status_code==409
    assert post(r,a,{"request_key":str(uuid4()),"row_version":1}).status_code==409
    target=URL+"/"+a["id"]+"/lines/"+row["id"]
    old=edit_line(r,a,row,quantity="2")
    assert r["client"].put(target,json=old).status_code==200
    assert r["client"].put(target,json=old).status_code==409
    assert balance(r)=="10"


def test_draft_edit_delete_tombstones_search_and_cross_parent_reads(returns):
    r=returns;a=header(r);b=header(r,"RTN-B");body=line_body(r,a)
    response=r["client"].post(URL+"/"+a["id"]+"/lines",json=body);assert response.status_code==201
    row=response.json();state=read(r,a)
    assert r["client"].put(URL+"/"+a["id"],json=edit_header(state,note="changed")).status_code==200
    assert r["client"].get(URL+"/"+b["id"]+"/lines/"+row["id"]).status_code==404
    assert len(r["client"].get(URL,params={"q":"RTN-A"}).json())==1
    assert r["client"].get(URL,params={"q":"%"}).json()==[]
    assert r["client"].delete(URL+"/"+a["id"],params={"row_version":read(r,a)["row_version"]}).status_code==409
    assert r["client"].delete(URL+"/"+a["id"]+"/lines/"+row["id"],params={"row_version":row["row_version"],"document_version":read(r,a)["row_version"]}).status_code==204
    assert r["client"].post(URL+"/"+a["id"]+"/lines",json=body).status_code==410
    assert r["client"].delete(URL+"/"+a["id"],params={"row_version":read(r,a)["row_version"]}).status_code==204
    assert balance(r)=="10"


@pytest.mark.parametrize("quantity",["0","-1","-0.0000000000000000000001"])
def test_nonpositive_draft_evidence_never_posts_as_outbound(returns,quantity):
    r=returns;a=header(r);line(r,a,quantity)
    before=counts(r);response=post(r,a)
    assert response.status_code in [409,422],response.text
    assert balance(r)=="10" and counts(r)==before


@pytest.mark.parametrize("quantity",["0.00000000000000000000000000000000000000000000000000001", "1234567890123456789012345678901234567890.12345678901234567890123456789", "9"*5000])
def test_exact_decimal_without_rounding_scale_or_magnitude_caps(returns,quantity):
    r=returns
    doc=receipt_header(r,"PRECISION-ORIGIN")
    origin=receipt_line(r,doc,quantity,lot_code="PRECISION-REAL-LOT")
    assert receipt_post(r,doc).status_code==201
    a=header(r);line(r,a,quantity,origin_line_id=origin["id"])
    result=post(r,a);assert result.status_code==201,result.text
    assert result.json()["movements"][0]["qty_base_delta"]=="-"+quantity
    assert next(p for p in stock(r) if p["lot_code"]=="PRECISION-REAL-LOT")["on_hand_qty_base"]=="0"
    assert balance(r)=="10" and get(r,r["order"])["total_amount"]=="156"


def test_immutable_posted_return_and_direct_sql_history_guards(returns):
    r=returns;a=header(r);row=line(r,a);assert post(r,a).status_code==201
    saved=read(r,a);before=stock(r)
    assert r["client"].put(URL+"/"+a["id"],json=edit_header(saved,note="rewrite")).status_code==409
    assert r["client"].delete(URL+"/"+a["id"],params={"row_version":saved["row_version"]}).status_code==409
    assert r["client"].post(URL+"/"+a["id"]+"/lines",json=line_body(r,a)).status_code==409
    assert r["client"].put(URL+"/"+a["id"]+"/lines/"+row["id"],json=edit_line(r,a,saved["lines"][0],quantity="1")).status_code==409
    for table in NEW:
        for command in ["DELETE FROM "+table,"UPDATE "+table+" SET id=id"]:
            with pytest.raises(IntegrityError):
                with r["db"].write() as session:session.connection().exec_driver_sql(command)
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:
            c=session.connection();columns=[x[1] for x in c.exec_driver_sql("PRAGMA table_info(supplier_return_line)")]
            values=["?" if k=="id" else "line_no+1" if k=="line_no" else k for k in columns]
            c.exec_driver_sql("INSERT INTO supplier_return_line("+",".join(columns)+") SELECT "+",".join(values)+" FROM supplier_return_line WHERE id=?",(str(uuid4()),row["id"]))
    assert read(r,a)==saved and stock(r)==before
    assert_originals(r)


def test_create_and_posting_keys_are_domain_separated_from_receipts(returns):
    r=returns
    key=str(uuid4());body=header_body(r,request_key=key);a=r["client"].post(URL,json=body).json()
    payload={"request_key":key,"document_no":"NEW-RECEIPT","purchase_order_id":r["order"]["id"],"business_date":"2026-11-19"}
    assert r["client"].post("/api/purchase-receipts",json=payload).status_code==409
    line(r,a);post_key=str(uuid4());assert post(r,a,{"request_key":post_key,"row_version":read(r,a)["row_version"]}).status_code==201
    inbound=receipt_header(r,"LATER-INBOUND");receipt_line(r,inbound,"1",lot_code="LOT-R8-001",supplier_lot_code="SUPPLIER-REAL-LOT")
    assert receipt_post(r,inbound,{"request_key":post_key,"row_version":r["client"].get("/api/purchase-receipts/"+inbound["id"]).json()["row_version"]}).status_code==409
    inbound_key=r["receipts"][0]["posting_id"]
    with r["db"].read() as session:
        inbound_key=session.connection().exec_driver_sql("SELECT idempotency_key FROM stock_posting WHERE id=?",(inbound_key,)).scalar()
    b=header(r,"NEXT-RETURN");line(r,b,"1")
    assert post(r,b,{"request_key":inbound_key,"row_version":read(r,b)["row_version"]}).status_code==409
    assert balance(r)=="7"


def test_malformed_prior_ledger_and_insufficient_physical_stock_fail_closed(returns):
    r=returns;a=header(r);line(r,a,"6");before=counts(r)
    # Deliberate corruption only in this disposable database, simulating an unsupported external writer.
    with r["db"].write() as session:
        c=session.connection();c.exec_driver_sql("DROP TRIGGER stock_entry_immutable_update")
        c.exec_driver_sql("UPDATE stock_entry SET qty_base_delta='1' WHERE document_line_id=?",(r["origins"][0]["id"],))
    assert balance(r)=="5"
    response=post(r,a);assert response.status_code in [409,422],response.text
    assert counts(r)==before and balance(r)=="5" and read(r,a)["posting_state"]=="DRAFT"


def direct_prepare(session,doc,key):
    c=session.connection();posting=str(uuid4())
    c.exec_driver_sql("UPDATE supplier_return SET row_version=row_version+1 WHERE id=?",(doc["id"],))
    c.exec_driver_sql("INSERT INTO supplier_return_posting(id,created_at,created_by,supplier_return_id,idempotency_key,request_fingerprint,source_version,posting_state,posted_at,posted_by) VALUES(?,?,?,?,?,?,?,'PREPARING',?,?)",(posting,doc["created_at"],DEMO_ACTOR_ID,doc["id"],key,"direct isolated SQL probe",doc["row_version"],doc["created_at"],DEMO_ACTOR_ID))
    return posting


def test_valid_origin_cannot_overdraw_corrupted_shared_physical_stock_api_or_sql(returns):
    r=returns;a=header(r);line(r,a,"6");doc=read(r,a);before=counts(r)
    # Preserve the selected +6 source leg. Simulate an unsupported external withdrawal
    # ONLY by corrupting the other origin in this disposable QA database.
    with r["db"].write() as session:
        c=session.connection();c.exec_driver_sql("DROP TRIGGER stock_entry_immutable_update")
        c.exec_driver_sql("UPDATE stock_entry SET qty_base_delta='-1' WHERE document_line_id=?",(r["origins"][1]["id"],))
    assert balance(r)=="5"
    assert read(r,a)["lines"][0]["source_quantity_base"]=="6"
    assert read(r,a)["lines"][0]["returnable_qty_base"]=="6"
    faulted=read(r,a)
    response=post(r,a)
    assert response.status_code in [409,422],response.text
    assert "庫存" in response.json()["message"]
    with pytest.raises(IntegrityError,match="supplier_return physical stock insufficient"):
        with r["db"].write() as session:direct_prepare(session,doc,str(uuid4()))
    assert counts(r)==before and read(r,a)==faulted and balance(r)=="5"


def test_inbound_and_return_posting_race_share_one_exact_authoritative_balance(returns):
    r=returns;a=header(r);line(r,a,"3")
    incoming=receipt_header(r,"RACE-NEW-INBOUND")
    receipt_line(r,incoming,"2",lot_code="LOT-R8-001",supplier_lot_code="SUPPLIER-REAL-LOT")
    return_key={"request_key":str(uuid4()),"row_version":read(r,a)["row_version"]}
    incoming_version=r["client"].get("/api/purchase-receipts/"+incoming["id"]).json()["row_version"]
    incoming_key={"request_key":str(uuid4()),"row_version":incoming_version}
    actions=[lambda:post(r,a,return_key),lambda:receipt_post(r,incoming,incoming_key)]
    with ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(lambda action:action(),actions))
    assert [response.status_code for response in responses]==[201,201],[response.text for response in responses]
    assert balance(r)=="9" and len(stock(r))==1
    assert counts(r)["supplier_return_entry"]==1
    saved=read(r,a)
    assert saved["movements"][0]["qty_base_delta"]=="-3"
    assert (saved["lines"][0]["returned_qty_base"],saved["lines"][0]["returnable_qty_base"])==("3","3")
    context=r["client"].get("/api/purchase-receipts/source-orders/"+r["order"]["id"]).json()["lines"][0]
    assert (context["ordered_qty_base"],context["received_qty_base"],context["remaining_qty_base"])==("12","12","0")
    assert [get(r,r["order"]),get(r,r["other_order"])]==r["original_orders"]
    with r["db"].read() as session:
        for table,original in r["frozen"].items():
            ids=[row[0] for row in original]
            if ids:
                rows=list(session.connection().exec_driver_sql("SELECT * FROM "+table+" WHERE id IN ("+",".join("?" for _ in ids)+") ORDER BY 1",tuple(ids)))
                assert rows==original
    assert post(r,a,return_key).status_code==200 and receipt_post(r,incoming,incoming_key).status_code==200
    assert balance(r)=="9"


def test_direct_sql_incomplete_wrong_direction_and_global_key_guards(returns):
    r=returns;a=header(r);row=line(r,a);doc=read(r,a)
    with r["db"].read() as session:
        inbound_key=session.connection().exec_driver_sql("SELECT idempotency_key FROM stock_posting LIMIT 1").scalar()
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:direct_prepare(session,doc,inbound_key)
    assert read(r,a)==doc
    with r["db"].write() as session:posting=direct_prepare(session,doc,str(uuid4()))
    assert read(r,a)["posting_state"]=="INCOMPLETE" and balance(r)=="10"
    assert read(r,a)["lines"][0]["returned_qty_base"]=="0"
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:
            session.connection().exec_driver_sql("INSERT INTO supplier_return_entry(id,created_at,created_by,posting_id,return_line_id,position_id,leg_no,qty_base_delta) VALUES(?,?,?,?,?,?,1,'3')",(str(uuid4()),doc["created_at"],DEMO_ACTOR_ID,posting,row["id"],stock(r)[0]["id"]))
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:session.connection().exec_driver_sql("UPDATE supplier_return_posting SET posting_state='POSTED' WHERE id=?",(posting,))
    assert post(r,a).status_code==409 and counts(r)["supplier_return_entry"]==0 and balance(r)=="10"


def legacy_0005(path, r):
    """Build the real old schema and populate accepted inbound rows in legal posting order."""
    from alembic import command
    from alembic.config import Config
    config=Config(str(ROOT/"backend/alembic.ini"));config.set_main_option("script_location",str(ROOT/"backend/migrations"))
    engine=make_engine(path)
    with engine.connect().execution_options(erp_write=True) as connection:
        config.attributes["connection"]=connection;command.upgrade(config,"0005_purchase_receiving")
    with sqlite3.connect(r["path"]) as source:
        with engine.connect().execution_options(erp_write=True) as dest:
            tables=["actor","currency","unit","item_category","customer","supplier","item","warehouse","work_center","supplier_offer","purchase_order","purchase_order_line","purchase_create_receipt","inventory_lot","stock_document","stock_document_line","stock_position","stock_create_receipt"]
            for table in tables:
                columns=[x[1] for x in source.execute("PRAGMA table_info("+table+")")]
                rows=source.execute("SELECT * FROM "+table).fetchall()
                if rows:dest.exec_driver_sql("INSERT INTO "+table+"("+",".join(columns)+") VALUES("+",".join("?" for _ in columns)+")",rows)
            cols=[x[1] for x in source.execute("PRAGMA table_info(stock_posting)")]
            for posting in source.execute("SELECT * FROM stock_posting"):
                row=list(posting);row[cols.index("posting_state")]="PREPARING"
                dest.exec_driver_sql("INSERT INTO stock_posting("+",".join(cols)+") VALUES("+",".join("?" for _ in cols)+")",tuple(row))
                entries=source.execute("SELECT * FROM stock_entry WHERE posting_id=?",(posting[cols.index("id")],)).fetchall()
                entry_cols=[x[1] for x in source.execute("PRAGMA table_info(stock_entry)")]
                dest.exec_driver_sql("INSERT INTO stock_entry("+",".join(entry_cols)+") VALUES("+",".join("?" for _ in entry_cols)+")",entries)
                dest.exec_driver_sql("UPDATE stock_posting SET posting_state='POSTED' WHERE id=?",(posting[cols.index("id")],))
            dest.commit()
    engine.dispose()


def test_real_0005_migration_preserves_posted_receipts_repeat_and_failure_rollback(returns,tmp_path,monkeypatch):
    r=returns;path=tmp_path/"old-posted.sqlite";legacy_0005(path,r);before=snapshot(path)
    migrate(path);migrate(path);after=snapshot(path)
    assert all(after["rows"][t]==rows for t,rows in before["rows"].items())
    assert_finished_goods_upgrade_objects(before,after)
    assert all(after["rows"][t]==[] for t in NEW)
    with TestClient(create_app(path)) as client:
        assert client.get("/api/stock-positions").json()[0]["on_hand_qty_base"]=="10"
        assert sorted(x["total_amount"] for x in client.get("/api/purchase-orders").json())==["156","36.25"]
    failed=tmp_path/"failed-upgrade.sqlite";legacy_0005(failed,r);old=snapshot(failed)
    from alembic.operations import Operations
    original=Operations.create_table
    def injected(self,name,*args,**kwargs):
        if name=="supplier_return_entry":raise RuntimeError("forced supplier-return DDL rollback")
        return original(self,name,*args,**kwargs)
    monkeypatch.setattr(Operations,"create_table",injected)
    with pytest.raises(RuntimeError,match="forced supplier-return DDL rollback"):migrate(failed)
    assert snapshot(failed)==old
    with sqlite3.connect(failed) as c:assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0]=="0005_purchase_receiving"
