"""Real persisted inbound movements; isolated SQLite/API tests, never Owner data."""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from backend.app.db import DEMO_ACTOR_ID, ROOT, make_engine, migrate
from backend.app.main import create_app
from backend.app.decimal_text import exact_difference
from backend.tests.test_foundation import setup
from backend.tests.test_purchasing import po, create_header, add_line, get, header_edit, line_edit

URL = "/api/purchase-receipts"
TABLES = ["inventory_lot", "stock_document", "stock_document_line", "stock_position", "stock_posting", "stock_entry", "stock_create_receipt"]


@pytest.fixture
def receiving(po):
    # One real tracked item, one deliberately nontracked item, two warehouses.
    raw = po["raws"][1]
    body = {key: raw[key] for key in ["code", "name", "specification", "category_id", "base_unit_id", "is_active", "row_version"]}
    response = po["client"].put("/api/raw-materials/"+raw["id"], json={**body, "lot_tracking": False})
    assert response.status_code == 200, response.text
    po["warehouse2"] = po["client"].post("/api/warehouses", json={"code": "QA-W2", "name": "第二倉", "is_active": True, "mrp_included": False}).json()
    parent = create_header(po)
    po["sources"] = [add_line(po, parent), add_line(po, parent, "20", "8.75", item=1)]
    po["order"] = get(po, parent)
    assert po["order"]["total_amount"] == "300"
    return po


def read(r, doc):
    response = r["client"].get(URL+"/"+doc["id"])
    assert response.status_code == 200, response.text
    return response.json()


def header(r, number="RC-A", **patch):
    data = {"request_key": str(uuid4()), "document_no": number, "purchase_order_id": r["order"]["id"],
            "business_date": "2026-11-19", "note": "原始草稿", **patch}
    response = r["client"].post(URL, json=data)
    assert response.status_code == 201, response.text
    return response.json()


def line_body(r, doc, quantity="6", source=0, **patch):
    return {"request_key": str(uuid4()), "document_version": read(r, doc)["row_version"],
            "purchase_order_line_id": r["sources"][source]["id"], "quantity": quantity,
            "warehouse_id": r["warehouse"]["id"], "lot_code": "REAL-LOT-A" if source == 0 else None, **patch}


def line(r, doc, quantity="6", source=0, **patch):
    response = r["client"].post(URL+"/"+doc["id"]+"/lines", json=line_body(r, doc, quantity, source, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def post(r, doc, body=None):
    return r["client"].post(URL+"/"+doc["id"]+"/post", json=body or {"request_key": str(uuid4()), "row_version": read(r, doc)["row_version"]})


def counts(r):
    with r["db"].read() as session:
        return {t: session.connection().exec_driver_sql("SELECT count(*) FROM "+t).scalar() for t in TABLES}


def positions(r):
    return r["client"].get("/api/stock-positions").json()


def test_real_6_plus_4_multi_line_draft_zero_restart_and_order_unchanged(receiving):
    r = receiving
    original = get(r, r["order"])
    a = header(r)
    one = line(r, a)
    line(r, a, "2", source=1, warehouse_id=r["warehouse2"]["id"])
    assert counts(r)["inventory_lot"] == counts(r)["stock_entry"] == counts(r)["stock_position"] == 0
    assert read(r, a)["lines"][0]["received_qty_base"] == "0"
    result = post(r, a)
    assert result.status_code == 201, result.text
    assert result.json()["posting_state"] == "POSTED" and len(result.json()["movements"]) == 2
    b = header(r, "RC-B")
    line(r, b, "4")
    assert post(r, b).status_code == 201
    pending = header(r, "RC-PENDING")
    line(r, pending, "999")
    stock = positions(r)
    assert sorted(p["on_hand_qty_base"] for p in stock) == ["10", "2"]
    assert len([p for p in stock if p["lot_id"] is None]) == 1
    assert counts(r)["inventory_lot"] == 1 and counts(r)["stock_entry"] == 3
    assert read(r, a)["lines"][0]["received_qty_base"] == "10"
    assert read(r, a)["lines"][0]["remaining_qty_base"] == "0"
    assert r["client"].get(URL+"/source-orders/"+original["id"]).json()["lines"][0]["received_qty_base"] == "10"
    assert r["client"].get(URL+"/"+a["id"]+"/lines/"+one["id"]).status_code == 200
    assert r["client"].get(URL+"/"+b["id"]+"/lines/"+one["id"]).status_code == 404
    assert get(r, r["order"]) == original
    saved = [read(r, doc) for doc in [a, b, pending]]
    migrate(r["path"])
    with TestClient(create_app(r["path"])) as client:
        assert [client.get(URL+"/"+doc["id"]).json() for doc in [a, b, pending]] == saved
        assert client.get("/api/stock-positions").json() == stock
        assert client.get("/api/purchase-orders/"+original["id"]).json() == original
    with r["db"].read() as session:
        c = session.connection()
        assert c.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(c.exec_driver_sql("PRAGMA foreign_key_check")) == []
        assert set(c.exec_driver_sql("SELECT typeof(quantity),typeof(base_factor_snapshot) FROM stock_document_line")) == {("text", "text")}
        assert set(c.exec_driver_sql("SELECT typeof(qty_base_delta) FROM stock_entry")) == {("text",)}


def test_existing_stock_lot_item_warehouse_isolation_overreceipt_and_zero(receiving):
    r = receiving
    for index, qty in enumerate(["2", "6", "4", "0"]):
        doc = header(r, "RC-"+str(index)); line(r, doc, qty)
        assert post(r, doc).status_code == 201
    assert positions(r)[0]["on_hand_qty_base"] == "12"
    assert read(r, doc)["lines"][0]["remaining_qty_base"] == "-2"  # no invented cap
    for index, kwargs in enumerate([{"warehouse_id": r["warehouse2"]["id"]}, {"lot_code": "REAL-LOT-B"}, {"source": 1}]):
        other = header(r, "OTHER-"+str(index)); line(r, other, "0.125", **kwargs)
        assert post(r, other).status_code == 201
    stock = positions(r)
    assert len(stock) == 4 and sorted(p["on_hand_qty_base"] for p in stock) == ["0.125", "0.125", "0.125", "12"]
    assert get(r, r["order"])["total_amount"] == "300"


def test_request_key_create_post_retries_double_click_and_concurrent_post(receiving):
    r = receiving
    data = {"request_key": str(uuid4()), "document_no": "RETRY", "purchase_order_id": r["order"]["id"], "business_date": "2026-11-19"}
    responses = [r["client"].post(URL, json=data) for _ in range(2)]
    assert [x.status_code for x in responses] == [201, 200]
    a = responses[0].json()
    assert r["client"].post(URL, json={**data, "document_no": "CHANGED"}).status_code == 409
    body = line_body(r, a)
    assert r["client"].post(URL+"/"+a["id"]+"/lines", json=body).status_code == 201
    assert r["client"].post(URL+"/"+a["id"]+"/lines", json=body).status_code == 200
    assert r["client"].post(URL+"/"+a["id"]+"/lines", json={**body, "quantity": "8"}).status_code == 409
    body = {"request_key": str(uuid4()), "row_version": read(r, a)["row_version"]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: post(r, a, body), range(4)))
    assert sorted(x.status_code for x in results) == [200, 200, 200, 201]
    assert len({x.json()["posting_id"] for x in results}) == 1
    assert post(r, a, body).status_code == 200
    assert post(r, a, {**body, "row_version": body["row_version"]+1}).status_code == 409
    assert post(r, a, {**body, "request_key": str(uuid4())}).status_code == 200
    b = header(r, "RC-B"); line(r, b, "4")
    assert post(r, b, {**body, "row_version": read(r, b)["row_version"]}).status_code == 409
    assert positions(r)[0]["on_hand_qty_base"] == "6" and counts(r)["stock_entry"] == 1


def test_concurrent_distinct_receipts_share_one_nullable_position(receiving):
    r = receiving
    docs = [header(r, "PARALLEL-"+str(i)) for i in range(4)]
    for doc in docs:
        line(r, doc, "0.125", source=1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda doc: post(r, doc), docs))
    assert all(x.status_code == 201 for x in responses), [x.text for x in responses]
    assert len(positions(r)) == 1 and positions(r)[0]["on_hand_qty_base"] == "0.5"
    assert positions(r)[0]["lot_id"] is None and counts(r)["inventory_lot"] == 0
    assert counts(r)["stock_entry"] == 4


@pytest.mark.parametrize("failure", ["second_entry", "finalize"])
def test_injected_failure_rolls_back_all_writes_and_retry_is_safe(receiving, failure):
    r = receiving
    a = header(r); line(r, a); line(r, a, "3", lot_code="SECOND-REAL-LOT")
    before, size = read(r, a), counts(r)
    with r["db"].write() as session:
        if failure == "second_entry":
            session.connection().exec_driver_sql("CREATE TRIGGER injected_failure BEFORE INSERT ON stock_entry WHEN (SELECT count(*) FROM stock_entry)>0 BEGIN SELECT RAISE(ABORT,'forced stock rollback'); END")
        else:
            session.connection().exec_driver_sql("CREATE TRIGGER injected_failure BEFORE UPDATE OF posting_state ON stock_posting BEGIN SELECT RAISE(ABORT,'forced stock finalize rollback'); END")
    result = post(r, a)
    assert result.status_code == 409, result.text
    assert read(r, a) == before and counts(r) == size and positions(r) == []
    with r["db"].write() as session:
        session.connection().exec_driver_sql("DROP TRIGGER injected_failure")
    assert post(r, a).status_code == 201
    assert sorted(p["on_hand_qty_base"] for p in positions(r)) == ["3", "6"]


def test_immutable_posted_history_and_direct_sql_guards(receiving):
    r = receiving
    a = header(r); l = line(r, a); assert post(r, a).status_code == 201
    frozen, stock = read(r, a), positions(r)
    hb = {k: frozen[k] for k in ["document_no", "purchase_order_id", "business_date", "external_reference", "note", "row_version"]}
    assert r["client"].put(URL+"/"+a["id"], json={**hb, "note": "rewrite"}).status_code == 409
    assert r["client"].delete(URL+"/"+a["id"], params={"row_version": frozen["row_version"]}).status_code == 409
    assert r["client"].post(URL+"/"+a["id"]+"/lines", json=line_body(r, a)).status_code == 409
    lb = {k: frozen["lines"][0][k] for k in ["purchase_order_line_id", "quantity", "warehouse_id", "lot_code", "supplier_lot_code", "manufactured_date", "expiry_date", "note", "row_version"]}
    assert r["client"].put(URL+"/"+a["id"]+"/lines/"+l["id"], json={**lb, "document_version": frozen["row_version"]}).status_code == 409
    assert r["client"].delete(URL+"/"+a["id"]+"/lines/"+l["id"], params={"row_version": l["row_version"], "document_version": frozen["row_version"]}).status_code == 409
    for table in ["stock_document", "stock_document_line", "stock_entry", "stock_posting", "stock_position", "inventory_lot"]:
        for command in ["DELETE FROM "+table, "UPDATE "+table+" SET id=id"]:
            with pytest.raises(IntegrityError):
                with r["db"].write() as session:
                    session.connection().exec_driver_sql(command)
    for column, value in [("lot_tracking", 0), ("item_kind", "FINISHED")]:
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:
                session.connection().exec_driver_sql("UPDATE item SET "+column+"=? WHERE id=?", (value, r["raws"][0]["id"]))
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:
            c=session.connection()
            columns=[row[1] for row in c.exec_driver_sql("PRAGMA table_info(stock_document_line)")]
            expressions=["?" if name=="id" else "line_no+1" if name=="line_no" else name for name in columns]
            c.exec_driver_sql("INSERT INTO stock_document_line("+",".join(columns)+") SELECT "+",".join(expressions)+" FROM stock_document_line WHERE id=?",(str(uuid4()),l["id"]))
    assert read(r, a) == frozen and positions(r) == stock


@pytest.mark.parametrize("patch", [{"quantity": 1.25}, {"quantity": "NaN"}, {"quantity": "Infinity"}, {"quantity": "1e3"},
    {"quantity": True}, {"warehouse_id": str(uuid4())}, {"purchase_order_line_id": str(uuid4())},
    {"lot_id": str(uuid4())}, {"item_id": str(uuid4())}, {"quantity_base": "9"}, {"manufactured_date": "2026-02-30"}])
def test_invalid_lines_have_no_side_effects(receiving, patch):
    r = receiving; a = header(r); before = read(r, a); size = counts(r)
    result = r["client"].post(URL+"/"+a["id"]+"/lines", json=line_body(r, a, **patch))
    assert result.status_code == 422, result.text
    assert read(r, a) == before and counts(r) == size


def test_lot_policy_missing_nontracked_conflicting_provenance_and_same_transaction(receiving):
    r = receiving; a = header(r); l = line(r, a, lot_code=None)
    assert post(r, a).status_code == 422 and positions(r) == []
    assert r["client"].post(URL+"/"+a["id"]+"/lines", json=line_body(r, a, source=1, lot_code="FAKE")).status_code == 422
    assert r["client"].post(URL+"/"+a["id"]+"/lines", json=line_body(r, a, source=1, expiry_date="2027-01-01")).status_code == 422
    b = header(r, "RC-B"); line(r, b, supplier_lot_code="SUP-REAL"); assert post(r, b).status_code == 201
    assert r["client"].post(URL+"/"+a["id"]+"/lines", json=line_body(r, a, supplier_lot_code="DIFFERENT")).status_code == 422
    c = header(r, "RC-C"); line(r, c, lot_code="NEW-REAL", supplier_lot_code="ONE"); line(r, c, lot_code="NEW-REAL", supplier_lot_code="TWO")
    size = counts(r); assert post(r, c).status_code == 422; assert counts(r) == size
    assert len(positions(r)) == 1 and positions(r)[0]["on_hand_qty_base"] == "6"


def test_negative_draft_direction_empty_receipt_version_and_source_checks(receiving):
    r = receiving; a = header(r)
    assert post(r, a).status_code == 422
    l = line(r, a, "-3.25")
    assert read(r, a)["lines"][0]["quantity"] == "-3.25"
    result = post(r, a); assert result.status_code == 422 and "負接受量" in result.text
    assert positions(r) == []
    b = header(r, "RC-B"); line(r, b)
    assert post(r, b, {"request_key": str(uuid4()), "row_version": 1}).status_code == 409
    original = get(r, r["order"])
    assert r["client"].put("/api/purchase-orders/"+original["id"], json=header_edit(original, note="explicit source edit")).status_code == 200
    assert post(r, b).status_code == 409
    assert positions(r) == []
    other = create_header(r, "OTHER-PO"); otherline = add_line(r, other)
    assert r["client"].post(URL+"/"+b["id"]+"/lines", json=line_body(r, b, purchase_order_line_id=otherline["id"])).status_code == 422


def test_draft_edit_delete_search_stale_writers_and_create_tombstone(receiving):
    r = receiving; a = header(r); body = line_body(r, a); response = r["client"].post(URL+"/"+a["id"]+"/lines", json=body); l=response.json()
    parent = read(r, a)
    lb = {k: l[k] for k in ["purchase_order_line_id", "quantity", "warehouse_id", "lot_code", "row_version"]}
    target = URL+"/"+a["id"]+"/lines/"+l["id"]
    result = r["client"].put(target, json={**lb, "quantity": "4.125", "document_version": parent["row_version"]})
    assert result.status_code == 200, result.text
    assert r["client"].put(target, json={**lb, "quantity": "7", "document_version": parent["row_version"]}).status_code == 409
    assert r["client"].get(URL, params={"q": "RC-A"}).json()[0]["id"] == a["id"]
    assert r["client"].get(URL, params={"q": "%"}).json() == []
    assert r["client"].delete(URL+"/"+a["id"], params={"row_version": read(r,a)["row_version"]}).status_code == 409
    assert r["client"].delete(target, params={"row_version": 2, "document_version": read(r,a)["row_version"]}).status_code == 204
    assert r["client"].post(URL+"/"+a["id"]+"/lines", json=body).status_code == 410
    assert r["client"].delete(URL+"/"+a["id"], params={"row_version": read(r,a)["row_version"]}).status_code == 204
    assert counts(r)["stock_entry"] == 0


@pytest.mark.parametrize("quantity", ["0.0000000000000000000000000000000000000000000000000000001", "1234567890123456789012345678901234567890.12345678901234567890123456789", "9"*5000])
def test_exact_quantity_without_rounding_or_scale_limit(receiving, quantity):
    r = receiving; a = header(r); line(r, a, quantity, source=1)
    assert post(r, a).status_code == 201
    assert positions(r)[0]["on_hand_qty_base"] == quantity
    b = header(r, "RC-B"); line(r, b, "0.125", source=1); assert post(r,b).status_code == 201
    from backend.app.decimal_text import exact_sum
    assert positions(r)[0]["on_hand_qty_base"] == exact_sum([quantity,"0.125"])
    assert exact_difference(quantity,quantity) == "0"
    assert get(r,r["order"])["total_amount"] == "300"


def test_nullable_unique_fk_and_incomplete_post_cannot_count_or_finalize(receiving):
    r = receiving; a=header(r); line(r,a,source=1); assert post(r,a).status_code == 201
    position=positions(r)[0]
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:
            session.connection().exec_driver_sql("INSERT INTO stock_position(id,created_at,created_by,item_id,warehouse_id,lot_id) SELECT ?,created_at,created_by,item_id,warehouse_id,NULL FROM stock_position WHERE id=?", (str(uuid4()),position["id"]))
    b=header(r,"INCOMPLETE"); line(r,b,source=1); current=read(r,b)
    posting_id=str(uuid4())
    with r["db"].write() as session:
        c=session.connection(); c.exec_driver_sql("UPDATE stock_document SET row_version=row_version+1 WHERE id=?",(b["id"],))
        c.exec_driver_sql("INSERT INTO stock_posting(id,created_at,created_by,stock_document_id,idempotency_key,request_fingerprint,source_version,posting_state,posted_at,posted_by) VALUES(?,?,?,?,?,?,?,'PREPARING',?,?)",(posting_id,current["created_at"],DEMO_ACTOR_ID,b["id"],str(uuid4()),"direct incomplete probe",current["row_version"],current["created_at"],DEMO_ACTOR_ID))
    assert read(r,b)["posting_state"] == "INCOMPLETE" and positions(r)[0]["on_hand_qty_base"] == "6"
    assert read(r,b)["lines"][0]["received_qty_base"] == "6"
    assert post(r,b).status_code == 409
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:
            session.connection().exec_driver_sql("UPDATE stock_posting SET posting_state='POSTED' WHERE id=?",(posting_id,))


def test_additive_0004_upgrade_repeat_and_ddl_failure_rollback(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from backend.tests.test_supplier_offers import snapshot
    from alembic.operations import Operations
    def legacy(path):
        config=Config(str(ROOT/"backend"/"alembic.ini")); config.set_main_option("script_location",str(ROOT/"backend"/"migrations"))
        engine=make_engine(path)
        with engine.connect().execution_options(erp_write=True) as c:
            config.attributes["connection"]=c; command.upgrade(config,"0004_purchase_orders")
        engine.dispose()
    path=tmp_path/"legacy.sqlite"; legacy(path); before=snapshot(path); migrate(path); migrate(path); after=snapshot(path)
    assert all(after["rows"][t]==rows for t,rows in before["rows"].items())
    assert all(obj in after["objects"] for obj in before["objects"])
    assert all(after["rows"][t]==[] for t in TABLES)
    failed=tmp_path/"failed.sqlite"; legacy(failed); before=snapshot(failed)
    original=Operations.create_table
    def injected(self,name,*args,**kwargs):
        if name=="stock_entry":
            raise RuntimeError("forced receiving migration failure")
        return original(self,name,*args,**kwargs)
    monkeypatch.setattr(Operations,"create_table",injected)
    with pytest.raises(RuntimeError,match="forced receiving migration failure"):
        migrate(failed)
    assert snapshot(failed)==before
