"""Draft manufacturing orders, real SQLite source holds and non-posting invariants."""
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from backend.app.db import DEMO_ACTOR_ID, migrate
from backend.app.main import create_app
from backend.tests.test_foundation import setup, resources, payload
from backend.tests.test_supplier_offers import snapshot
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry, query
from backend.tests.test_bom import header as bom_header, add as bom_add, edit_header as bom_edit
from backend.tests.test_routing import header as routing_header, add as routing_add, header_edit as routing_edit

URL = "/api/work-orders"


@pytest.fixture
def work(setup):
    client, db, path = setup
    category, sheet = resources(client)
    book = client.post("/api/units", json={"code": "BOOK", "name": "本"}).json()
    products = [client.post("/api/finished-goods", json=payload(category, book, code)).json()
                for code in ["FG-001", "FG-BOM-002"]]
    raw = client.post("/api/raw-materials", json=payload(category, sheet, "RAW-ONLY")).json()
    center = client.post("/api/work-centers", json={"code": "CENTER", "name": "共用中心", "is_active": True}).json()
    operation = client.post("/api/operation-definitions", json={"code": "OP", "name": "可重用工法", "is_active": True}).json()
    r = dict(client=client, db=db, path=path, products=products, raws=[raw], centers=[center], operations=[operation])
    boms = [bom_header(r, i, revision="BOM-A", output_basis_qty="1") for i in range(2)]
    for b in boms:
        bom_add(r, b, qty="2.75")
    routings = [routing_header(r, i, revision="ROUTE-A") for i in range(2)]
    for route in routings:
        routing_add(r, route, "10", setup_minutes="1.25", run_minutes_per_base="0.125")
    r["boms"] = [client.get("/api/boms/"+b["id"]).json() for b in boms]
    r["routings"] = [client.get("/api/routings/"+h["id"]).json() for h in routings]
    return r


def data(r, number="WO-A", product=0, bom=None, routing=None, **patch):
    b = r["boms"][bom] if bom is not None else None
    route = r["routings"][routing] if routing is not None else None
    return {**dict(request_key=str(uuid4()), document_no=number, product_id=r["products"][product]["id"],
                   planned_qty_base="12.5", planned_start="2026-11-20", planned_finish="2026-11-25", note="草稿計畫",
                   bom_id=b["id"] if b else None, bom_version=b["row_version"] if b else None,
                   routing_id=route["id"] if route else None, routing_version=route["row_version"] if route else None), **patch}


def create(r, number="WO-A", product=0, bom=None, routing=None, **patch):
    response = r["client"].post(URL, json=data(r, number, product, bom, routing, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def get(r, row):
    response = r["client"].get(URL+"/"+row["id"])
    assert response.status_code == 200, response.text
    return response.json()


def edit(row, **patch):
    return {**{key: row[key] for key in ["document_no", "product_id", "planned_qty_base", "planned_start", "planned_finish", "note", "bom_id", "routing_id", "row_version"]},
            "bom_version": row["bom"]["row_version"] if row["bom"] else None,
            "routing_version": row["routing"]["row_version"] if row["routing"] else None, **patch}


def test_independent_orders_same_and_other_products_crud_search_audit_and_reopen(work):
    r = work; client = r["client"]
    a = create(r, "WO-ONE", bom=0, routing=0)
    b = create(r, "WO-TWO", planned_qty_base="7")
    other = create(r, "WO-OTHER", 1, 1, 1)
    assert len({a["id"], b["id"], other["id"]}) == 3
    assert str(UUID(a["id"])) == a["id"] and a["id"] != a["document_no"]
    assert a["product_code"] == "FG-001" and a["product_base_unit_code"] == "BOOK"
    assert a["approval_state"] == b["approval_state"] == other["approval_state"] == "DRAFT"
    assert a["bom"]["release_state"] == a["routing"]["release_state"] == "DRAFT"
    changed = client.put(URL+"/"+a["id"], json=edit(a, planned_qty_base="-0.125", note="修改後備註"))
    assert changed.status_code == 200, changed.text
    updated = changed.json()
    assert updated["row_version"] == 2 and updated["planned_qty_base"] == "-0.125"
    assert (updated["id"], updated["created_at"], updated["created_by"]) == (a["id"], a["created_at"], DEMO_ACTOR_ID)
    assert get(r, b) == b and get(r, other) == other
    assert len(client.get(URL, params={"q": "FG-001"}).json()) == 2
    assert len(client.get(URL, params={"q": "WO-ONE"}).json()) == 1
    assert client.get(URL, params={"q": "%"}).json() == []
    assert client.delete(URL+"/"+b["id"], params={"row_version": b["row_version"]}).status_code == 204
    assert client.get(URL+"/"+b["id"]).status_code == 404
    migrate(r["path"])
    with TestClient(create_app(r["path"])) as reopened:
        assert reopened.get(URL+"/"+a["id"]).json() == updated
        assert reopened.get(URL+"/"+other["id"]).json() == other


@pytest.mark.parametrize("value", ["0", "-2.75", "0."+"0"*1000+"1", "9"*5000+"."+"7"*500])
def test_exact_signed_zero_and_long_quantity_text_without_release_limits(work, value):
    r = work; row = create(r, planned_qty_base=value)
    assert row["planned_qty_base"] == get(r, row)["planned_qty_base"] == value
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT typeof(planned_qty_base),planned_qty_base FROM work_order").first() == ("text", value)


@pytest.mark.parametrize("bom,routing", [(None, None), (0, None), (None, 0), (0, 0)])
def test_explicit_optional_draft_source_combinations_without_automatic_choice(work, bom, routing):
    r = work; row = create(r, bom=bom, routing=routing)
    assert row["bom_id"] == (r["boms"][bom]["id"] if bom is not None else None)
    assert row["routing_id"] == (r["routings"][routing]["id"] if routing is not None else None)
    assert bool(row["bom"]) == (bom is not None) and bool(row["routing"]) == (routing is not None)
    if bom is not None:
        assert row["bom"]["revision"] == "BOM-A" and row["bom"]["row_version"] == r["boms"][bom]["row_version"]
    if routing is not None:
        assert row["routing"]["revision"] == "ROUTE-A" and row["routing"]["row_version"] == r["routings"][routing]["row_version"]


def test_product_retarget_requires_explicit_consistent_source_choices(work):
    r = work; c = r["client"]; row = create(r, bom=0, routing=0)
    before = snapshot(r["path"])
    assert c.put(URL+"/"+row["id"], json=edit(row, product_id=r["products"][1]["id"])).status_code == 422
    assert get(r, row) == row and snapshot(r["path"]) == before
    response = c.put(URL+"/"+row["id"], json=edit(row, product_id=r["products"][1]["id"],
        bom_id=None, bom_version=None, routing_id=None, routing_version=None))
    assert response.status_code == 200, response.text
    moved = response.json()
    assert moved["bom_id"] is moved["routing_id"] is moved["bom"] is moved["routing"] is None
    response = c.put(URL+"/"+row["id"], json=edit(moved, bom_id=r["boms"][1]["id"], bom_version=r["boms"][1]["row_version"],
        routing_id=r["routings"][1]["id"], routing_version=r["routings"][1]["row_version"]))
    assert response.status_code == 200, response.text
    assert response.json()["bom"]["product_id"] == response.json()["routing"]["product_id"] == moved["product_id"]


@pytest.mark.parametrize("patch", [
    {"planned_qty_base": 1.25}, {"planned_qty_base": "NaN"}, {"planned_qty_base": "1e3"},
    {"planned_qty_base": "1,2"}, {"document_no": " "}, {"planned_start": "2026-02-30"},
    {"planned_finish": "2026-11-25T10:30"}, {"approval_state": "APPROVED"},
    {"execution_state": "PLANNED"}, {"released_at": "2026-11-20T00:00:00Z"},
    {"product_description_snapshot": "未發放不得建立快照"}, {"created_by": str(uuid4())}])
def test_invalid_format_or_unapproved_workflow_payload_does_not_write(work, patch):
    r = work; before = snapshot(r["path"])
    assert r["client"].post(URL, json=data(r, **patch)).status_code == 422
    assert snapshot(r["path"]) == before


def test_date_syntax_past_future_and_reversed_drafts_are_persistent_not_scheduled(work):
    r = work
    row = create(r, planned_start="9999-12-31", planned_finish="0001-01-01")
    assert row["planned_start"] == "9999-12-31" and row["planned_finish"] == "0001-01-01"
    assert row["date_order_warning"] and row["approval_state"] == "DRAFT"
    response = r["client"].put(URL+"/"+row["id"], json=edit(row, planned_start="2020-01-01", planned_finish="2020-01-02"))
    assert response.status_code == 200, response.text
    assert not response.json()["date_order_warning"] and response.json()["approval_state"] == "DRAFT"
    assert get(r, row) == response.json()


def test_raw_unknown_and_cross_product_sources_rejected_atomically(work):
    r = work; c = r["client"]
    patches = [
        {"product_id": r["raws"][0]["id"]}, {"product_id": str(uuid4())},
        {"bom_id": r["boms"][1]["id"], "bom_version": r["boms"][1]["row_version"]},
        {"routing_id": r["routings"][1]["id"], "routing_version": r["routings"][1]["row_version"]},
        {"bom_id": str(uuid4()), "bom_version": 1}, {"routing_id": str(uuid4()), "routing_version": 1},
        {"bom_id": r["boms"][0]["id"]}, {"routing_id": r["routings"][0]["id"]},
        {"bom_version": 1}, {"routing_version": 1}]
    for patch in patches:
        before = snapshot(r["path"])
        assert c.post(URL, json=data(r, **patch)).status_code == 422
        assert snapshot(r["path"]) == before


def test_stale_source_selection_and_order_cas_preserve_prior_valid_record(work):
    r = work; c = r["client"]; body = data(r, bom=0, routing=0)
    b = r["boms"][0]
    changed = c.put("/api/boms/"+b["id"], json=bom_edit(b, revision="BOM-EDITED"))
    assert changed.status_code == 200, changed.text
    before = snapshot(r["path"])
    assert c.post(URL, json=body).status_code == 409
    assert snapshot(r["path"]) == before
    body["bom_version"] = changed.json()["row_version"]
    created = c.post(URL, json=body); assert created.status_code == 201, created.text
    row = created.json()
    response = c.put(URL+"/"+row["id"], json=edit(row, note="第二版")); assert response.status_code == 200
    before = snapshot(r["path"])
    assert c.put(URL+"/"+row["id"], json=edit(row, note="過時版本")).status_code == 409
    assert c.delete(URL+"/"+row["id"], params={"row_version": 1}).status_code == 409
    assert get(r, row) == response.json() and snapshot(r["path"]) == before


def test_referenced_source_headers_and_children_are_held_until_explicit_unlink(work):
    r = work; c = r["client"]; row = create(r, bom=0, routing=0)
    b, route = r["boms"][0], r["routings"][0]
    component, step = b["components"][0], route["steps"][0]
    before = snapshot(r["path"])
    assert c.put("/api/boms/"+b["id"], json=bom_edit(b, revision="BLOCKED")).status_code == 409
    assert c.put("/api/routings/"+route["id"], json=routing_edit(route, revision="BLOCKED")).status_code == 409
    bad = [
        ("UPDATE bom SET revision='BLOCKED' WHERE id=?", (b["id"],)),
        ("UPDATE bom_component SET usage_qty_base='999' WHERE id=?", (component["id"],)),
        ("DELETE FROM bom_component WHERE id=?", (component["id"],)),
        ("UPDATE routing SET revision='BLOCKED' WHERE id=?", (route["id"],)),
        ("UPDATE routing_step SET sequence_no=999 WHERE id=?", (step["id"],)),
        ("DELETE FROM routing_step WHERE id=?", (step["id"],)),
        ("INSERT OR REPLACE INTO bom_component(id,created_at,created_by,updated_at,row_version,bom_id,line_no,material_id,usage_qty_base,purpose_note) SELECT id,created_at,created_by,updated_at,row_version,?,99,material_id,usage_qty_base,purpose_note FROM bom_component WHERE id=?", (r["boms"][1]["id"], component["id"])),
        ("INSERT OR REPLACE INTO routing_step(id,created_at,created_by,updated_at,row_version,routing_id,sequence_no,operation_id,work_center_id,setup_minutes,run_minutes_per_base) SELECT id,created_at,created_by,updated_at,row_version,?,99,operation_id,work_center_id,setup_minutes,run_minutes_per_base FROM routing_step WHERE id=?", (r["routings"][1]["id"], step["id"])),
        ("INSERT OR REPLACE INTO bom_component(rowid,id,created_at,created_by,updated_at,row_version,bom_id,line_no,material_id,usage_qty_base,purpose_note) SELECT rowid,?,created_at,created_by,updated_at,row_version,?,99,material_id,usage_qty_base,purpose_note FROM bom_component WHERE id=?", (str(uuid4()), r["boms"][1]["id"], component["id"])),
        ("INSERT OR REPLACE INTO routing_step(rowid,id,created_at,created_by,updated_at,row_version,routing_id,sequence_no,operation_id,work_center_id,setup_minutes,run_minutes_per_base) SELECT rowid,?,created_at,created_by,updated_at,row_version,?,99,operation_id,work_center_id,setup_minutes,run_minutes_per_base FROM routing_step WHERE id=?", (str(uuid4()), r["routings"][1]["id"], step["id"])),
        ("UPDATE OR REPLACE bom_component SET rowid=(SELECT rowid FROM bom_component WHERE id=?) WHERE id=?", (component["id"], r["boms"][1]["components"][0]["id"])),
        ("UPDATE OR REPLACE routing_step SET rowid=(SELECT rowid FROM routing_step WHERE id=?) WHERE id=?", (step["id"], r["routings"][1]["steps"][0]["id"])),
        ("DELETE FROM bom WHERE id=?", (b["id"],)),
        ("DELETE FROM routing WHERE id=?", (route["id"],))]
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT rowid FROM bom_component WHERE id=?", (component["id"],)).scalar() > 0
        assert session.connection().exec_driver_sql("SELECT rowid FROM routing_step WHERE id=?", (step["id"],)).scalar() > 0
    for sql, args in bad:
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:
                session.connection().exec_driver_sql("PRAGMA recursive_triggers=OFF")
                session.connection().exec_driver_sql(sql, args)
        assert snapshot(r["path"]) == before
    response = c.put(URL+"/"+row["id"], json=edit(row, bom_id=None, bom_version=None, routing_id=None, routing_version=None))
    assert response.status_code == 200, response.text
    assert c.put("/api/boms/"+b["id"], json=bom_edit(b, revision="NOW-EDITABLE")).status_code == 200
    assert c.put("/api/routings/"+route["id"], json=routing_edit(route, revision="NOW-EDITABLE")).status_code == 200


def test_direct_sql_order_kind_source_date_decimal_and_creation_audit_guards(work):
    r = work; row = create(r)
    bad = [
        ("UPDATE work_order SET product_id=? WHERE id=?", (r["raws"][0]["id"], row["id"])),
        ("UPDATE work_order SET bom_id=? WHERE id=?", (r["boms"][1]["id"], row["id"])),
        ("UPDATE work_order SET routing_id=? WHERE id=?", (r["routings"][1]["id"], row["id"])),
        ("UPDATE work_order SET planned_qty_base='1.00' WHERE id=?", (row["id"],)),
        ("UPDATE work_order SET planned_start='2026-02-30' WHERE id=?", (row["id"],)),
        ("UPDATE work_order SET approval_state='APPROVED' WHERE id=?", (row["id"],)),
        ("UPDATE work_order SET id=? WHERE id=?", (str(uuid4()), row["id"])),
        ("UPDATE work_order SET created_at='2020-01-01' WHERE id=?", (row["id"],)),
        ("UPDATE item SET code='CHANGED' WHERE id=?", (row["product_id"],))]
    for sql, args in bad:
        before = snapshot(r["path"])
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:
                session.connection().exec_driver_sql(sql, args)
        assert snapshot(r["path"]) == before


def test_order_only_product_basis_and_replace_creation_identity_cannot_be_rewritten(work):
    r = work; c = r["client"]
    category = c.get("/api/categories").json()[0]
    book = next(u for u in c.get("/api/units").json() if u["code"] == "BOOK")
    product = c.post("/api/finished-goods", json=payload(category, book, "WORK-ORDER-ONLY")).json()
    row = create(r, product_id=product["id"])
    victim = create(r, "WO-HELD-SOURCES", bom=0, routing=0)
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT rowid FROM work_order WHERE id=?", (row["id"],)).scalar() > 0
        assert session.connection().exec_driver_sql("SELECT rowid FROM work_order WHERE id=?", (victim["id"],)).scalar() > 0
    bad = [
        ("UPDATE item SET code='CHANGED' WHERE id=?", (product["id"],)),
        ("UPDATE item SET item_kind='RAW' WHERE id=?", (product["id"],)),
        ("UPDATE item SET base_unit_id=? WHERE id=?", (r["raws"][0]["base_unit_id"], product["id"])),
        ("INSERT OR REPLACE INTO work_order(id,created_at,created_by,updated_at,row_version,document_no,product_id,planned_qty_base,planned_start,planned_finish,note,bom_id,routing_id,approval_state) SELECT ?,created_at,created_by,updated_at,1,document_no,product_id,planned_qty_base,planned_start,planned_finish,note,bom_id,routing_id,approval_state FROM work_order WHERE id=?", (str(uuid4()), row["id"])),
        ("INSERT OR REPLACE INTO work_order(id,created_at,created_by,updated_at,row_version,document_no,product_id,planned_qty_base,planned_start,planned_finish,note,bom_id,routing_id,approval_state) SELECT id,created_at,created_by,updated_at,1,'CHANGED-NUMBER',product_id,planned_qty_base,planned_start,planned_finish,note,bom_id,routing_id,approval_state FROM work_order WHERE id=?", (row["id"],)),
        ("UPDATE OR REPLACE work_order SET document_no=? WHERE id=?", (victim["document_no"], row["id"])),
        ("INSERT OR REPLACE INTO work_order(rowid,id,created_at,created_by,updated_at,row_version,document_no,product_id,planned_qty_base,planned_start,planned_finish,note,bom_id,routing_id,approval_state) SELECT rowid,?,created_at,created_by,updated_at,1,'ROWID-REPLACEMENT',product_id,planned_qty_base,planned_start,planned_finish,note,bom_id,routing_id,approval_state FROM work_order WHERE id=?", (str(uuid4()), row["id"])),
        ("UPDATE OR REPLACE work_order SET rowid=(SELECT rowid FROM work_order WHERE id=?) WHERE id=?", (victim["id"], row["id"]))]
    for sql, args in bad:
        before = snapshot(r["path"])
        with pytest.raises(IntegrityError):
            with r["db"].write() as session:
                session.connection().exec_driver_sql("PRAGMA recursive_triggers=OFF")
                session.connection().exec_driver_sql(sql, args)
        assert snapshot(r["path"]) == before


    assert c.put("/api/boms/"+r["boms"][0]["id"], json=bom_edit(r["boms"][0], revision="STILL-HELD")).status_code == 409
    assert c.put("/api/routings/"+r["routings"][0]["id"], json=routing_edit(r["routings"][0], revision="STILL-HELD")).status_code == 409


def test_actual_incoming_fk_blocks_order_deletion_and_failed_writes_rollback(work):
    r = work; c = r["client"]; row = create(r)
    with r["db"].write() as session:
        session.connection().exec_driver_sql("CREATE TABLE qa_work_order_reference (order_id TEXT REFERENCES work_order(id) ON DELETE RESTRICT)")
        session.connection().exec_driver_sql("INSERT INTO qa_work_order_reference VALUES (?)", (row["id"],))
    before = snapshot(r["path"])
    assert c.delete(URL+"/"+row["id"], params={"row_version": 1}).status_code == 409
    with pytest.raises(IntegrityError):
        with r["db"].write() as session:
            session.connection().exec_driver_sql("DELETE FROM work_order WHERE id=?", (row["id"],))
    assert snapshot(r["path"]) == before


def test_duplicate_request_replay_document_uniqueness_concurrency_and_tombstone(work):
    r = work; c = r["client"]; body = data(r, bom=0, routing=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: c.post(URL, json=body).status_code, range(2))) == [200, 201]
    row = c.get(URL).json()[0]
    assert c.post(URL, json={**body, "request_key": str(uuid4())}).status_code == 409
    assert c.post(URL, json={**body, "note": "同請求識別不同內容"}).status_code == 409
    edit_body = edit(row)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda qty: c.put(URL+"/"+row["id"], json={**edit_body, "planned_qty_base": qty}).status_code,
                               ["2", "3"])) == [200, 409]
    saved = get(r, row)
    assert c.delete(URL+"/"+row["id"], params={"row_version": saved["row_version"]}).status_code == 204
    assert c.post(URL, json=body).status_code == 410


def test_injected_insert_update_delete_failures_leave_no_partial_rows_or_receipts(work):
    r = work; c = r["client"]; row = create(r, "WO-SAFE")
    with r["db"].write() as session:
        con = session.connection()
        con.exec_driver_sql("CREATE TRIGGER qa_wo_insert AFTER INSERT ON work_order WHEN NEW.document_no='FAIL' BEGIN SELECT RAISE(ABORT,'work order forced insert rollback'); END")
        con.exec_driver_sql("CREATE TRIGGER qa_wo_update AFTER UPDATE ON work_order WHEN NEW.planned_qty_base='999' BEGIN SELECT RAISE(ABORT,'work order forced update rollback'); END")
        con.exec_driver_sql("CREATE TRIGGER qa_wo_delete AFTER DELETE ON work_order BEGIN SELECT RAISE(ABORT,'work order forced delete rollback'); END")
    for action in [lambda: c.post(URL, json=data(r, "FAIL", bom=0, routing=0)),
                   lambda: c.put(URL+"/"+row["id"], json=edit(row, planned_qty_base="999")),
                   lambda: c.delete(URL+"/"+row["id"], params={"row_version": 1})]:
        before = snapshot(r["path"])
        assert action().status_code == 409
        assert snapshot(r["path"]) == before


def accepted_sources(r):
    """Build accepted-style manufacturing sources in an already isolated purchase fixture."""
    c = r["client"]
    category = c.get("/api/categories").json()[0]
    book = c.post("/api/units", json={"code": "UNIT-BOOK", "name": "本"}).json()
    products = [c.post("/api/finished-goods", json=payload(category, book, code)).json()
                for code in ["FG-001", "FG-BOM-002"]]
    centers = [c.post("/api/work-centers", json={"code": "WC-TEST-001", "name": "共用工作中心", "is_active": True}).json()]
    operations = [c.post("/api/operation-definitions", json={"code": code, "name": name, "is_active": True}).json()
                  for code, name in [("OP-R13-CUT", "裁紙"), ("OP-R13-PRINT", "印刷"), ("OP-R13-BIND", "裝訂")]]
    r.update(products=products, centers=centers, operations=operations)
    a, b = bom_header(r, revision="BOM-TEST-001", output_basis_qty="1"), bom_header(r, 1, revision="BOM-TEST-002", output_basis_qty="1")
    bom_add(r, a, qty="2.75"); bom_add(r, a, 1, "0.5"); bom_add(r, b, qty="1.25")
    first, other = routing_header(r, revision="ROUTE-TEST-001"), routing_header(r, 1, revision="ROUTE-OTHER")
    routing_add(r, first, "10", setup_minutes="1.25", run_minutes_per_base="0.125")
    routing_add(r, first, "20", operation=1); routing_add(r, first, "30", operation=2)
    routing_add(r, other, "10")
    r["boms"] = [c.get("/api/boms/"+h["id"]).json() for h in [a, b]]
    r["routings"] = [c.get("/api/routings/"+h["id"]).json() for h in [first, other]]
    return r


def test_work_order_crud_never_mutates_existing_boms_routes_purchase_returns_or_stock(inquiry):
    r = accepted_sources(inquiry); c = r["client"]
    before = snapshot(r["path"])
    boms, routings = c.get("/api/boms").json(), c.get("/api/routings").json()
    stock, original_query = c.get("/api/stock-positions").json(), query(r)
    row = create(r, bom=0, routing=0)
    updated = c.put(URL+"/"+row["id"], json=edit(row, planned_qty_base="0", planned_start="2026-12-20", planned_finish="2026-01-01"))
    assert updated.status_code == 200, updated.text
    other = create(r, "WO-OTHER", 1, 1, 1, planned_qty_base="-1.125")
    assert c.delete(URL+"/"+other["id"], params={"row_version": 1}).status_code == 204
    after = snapshot(r["path"])
    assert all(after["rows"][table] == rows for table, rows in before["rows"].items()
               if table not in {"work_order", "stock_create_receipt"})
    assert c.get("/api/boms").json() == boms and c.get("/api/routings").json() == routings
    assert c.get("/api/stock-positions").json() == stock and query(r) == original_query


def test_late_work_order_ddl_failure_rolls_back_every_new_table_index_and_source_hold(tmp_path, monkeypatch):
    from importlib import import_module
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from backend.app.db import ROOT, make_engine
    from backend.tests.test_master_data import legacy_database

    path = tmp_path / "old-routing-schema.sqlite"
    legacy_database(path)
    engine = make_engine(path)
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    try:
        with engine.connect().execution_options(erp_write=True) as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0008_manufacturing_routing")
    finally:
        engine.dispose()
    before = snapshot(path)
    assert before["rows"]["item"] and "work_order" not in before["rows"]
    migration = import_module("backend.migrations.versions.0009_draft_work_orders")
    execute = migration.op.execute
    reached = {}

    def fail_late(statement, *args, **kwargs):
        if "CREATE TRIGGER work_order_routing_step_hold_update" in str(statement):
            names = set(migration.op.get_bind().exec_driver_sql("SELECT name FROM sqlite_master").scalars())
            reached.update(table="work_order" in names, index="ix_work_order_product_id" in names,
                           prior_source_hold="work_order_bom_component_hold_update" in names)
            raise RuntimeError("injected late work-order source-hold DDL failure")
        return execute(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(migration.op, "execute", fail_late)
        with pytest.raises(RuntimeError, match="injected late work-order source-hold DDL failure"):
            migrate(path)
    assert reached == {"table": True, "index": True, "prior_source_hold": True}
    assert snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0008_manufacturing_routing"
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='work_order'").fetchone() is None

    # Keep this historical 0009 fault-injection test pinned to its own revision.
    def upgrade_work_order_only():
        with engine.connect().execution_options(erp_write=True) as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0009_draft_work_orders")
    upgrade_work_order_only()
    upgrade_work_order_only()
    engine.dispose()
    after = snapshot(path)
    assert all(after["rows"][table] == rows for table, rows in before["rows"].items())
    assert all(obj in after["objects"] for obj in before["objects"])
    assert set(after["rows"]) - set(before["rows"]) == {"work_order"}
    assert after["rows"]["work_order"] == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0009_draft_work_orders"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

