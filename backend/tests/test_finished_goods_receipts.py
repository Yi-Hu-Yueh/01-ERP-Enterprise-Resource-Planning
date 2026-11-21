"""Source-limited finished-goods posting in fresh isolated databases only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT
from backend.app.local_demo import create_local_demo_app
from backend.app.main import create_app
from backend.tests.test_completions import create as create_completion, confirm as confirm_completion
from backend.tests.test_foundation import resources, payload

URL = "/api/finished-goods-receipts"
TABLES = ["finished_goods_receipt", "finished_goods_posting", "finished_goods_entry"]
PORT = 8032
UI_PORT = 5195


def snapshot(path):
    """Release handles explicitly so SQLite backup/restart tests work on Windows."""
    with closing(sqlite3.connect(path)) as con:
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return dict(rows={name: con.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall() for name in names},
            objects=con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall())


def artifact_parent(tmp_path):
    parent = tmp_path.resolve()
    if parent.is_relative_to((ROOT / "artifacts").resolve()):
        return parent
    base = ROOT / "artifacts" / "round18-backend-tests"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="fg-receipt-", dir=base))


def client_for(app):
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))


def add_completion(r, number="COMP-QA-EXTRA", order=0, good="1.25", defective="0.25"):
    row = create_completion(r, number, order, good_qty_base=good, defective_qty_base=defective)
    response = confirm_completion(r, row)
    assert response.status_code == 201, response.text
    return response.json()


def seed_small(client):
    category, _ = resources(client)
    unit = client.post("/api/units", json=dict(code="BOOK", name="本")).json()
    products = []
    orders = []
    for index, tracking in enumerate([True, False]):
        product = client.post("/api/finished-goods", json={**payload(category, unit, "FG-QA-" + str(index)), "lot_tracking": tracking}).json()
        products.append(product)
        response = client.post("/api/work-orders", json=dict(request_key=str(uuid4()), document_no="WO-QA-" + str(index),
            product_id=product["id"], planned_qty_base="2.5" if index == 0 else "3.75",
            planned_start="2026-11-20", planned_finish="2026-11-21"))
        assert response.status_code == 201, response.text
        orders.append(response.json())
    warehouse = client.post("/api/warehouses", json=dict(code="WH-QA", name="成品倉", is_active=True, mrp_included=True)).json()
    other = client.post("/api/warehouses", json=dict(code="WH-QA-2", name="第二倉", is_active=True, mrp_included=False)).json()
    return dict(products=products, orders=orders, warehouse=warehouse, warehouse2=other, unit=unit)


@pytest.fixture
def receiving(tmp_path):
    path = artifact_parent(tmp_path) / "fg-receiving.sqlite"
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        r = dict(client=client, app=app, db=app.state.db, path=path, **seed_small(client))
        r["completions"] = [add_completion(r, "COMP-QA-A"), add_completion(r, "COMP-QA-B", order=1)]
        yield r


def source(r, completion=None):
    completion = completion or r["completions"][0]
    response = r["client"].get(URL + "/sources", params=dict(completion_id=completion["id"]))
    assert response.status_code == 200, response.text
    rows = response.json()
    return next(row for row in rows if row["completion_id"] == completion["id"])


def body(r, number="FGRCV-QA", source_index=0, **patch):
    completion = r["completions"][source_index]
    tracking = r["products"][source_index]["lot_tracking"]
    data = {**dict(request_key=str(uuid4()), document_no=number, completion_id=completion["id"],
        completion_version=completion["row_version"], warehouse_id=r["warehouse"]["id"], business_date="2026-11-20",
        quantity="1.25", lot_id=None, lot_code="LOT-FG-QA" if tracking else None, note="隔離成品入庫測試"), **patch}
    # Model an explicit existing-lot selection, rather than relying on an
    # undocumented automatic reuse of a typed "new" lot code.
    if tracking and "lot_id" not in patch:
        response = r["client"].get(URL + "/lots", params=dict(completion_id=completion["id"]))
        assert response.status_code == 200, response.text
        matching = next((lot for lot in response.json() if lot["lot_code"] == data["lot_code"]), None)
        if matching:
            data["lot_id"] = matching["id"]
    return data


def create(r, number="FGRCV-QA", source_index=0, **patch):
    response = r["client"].post(URL, json=body(r, number, source_index, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def current(r, document):
    response = r["client"].get(URL + "/" + document["id"])
    assert response.status_code == 200, response.text
    return response.json()


def edit(document, **patch):
    return {**{key: document[key] for key in ["document_no", "completion_id", "completion_version", "warehouse_id",
        "business_date", "quantity", "lot_id", "lot_code", "note", "row_version"]}, **patch}


def review(r, document):
    response = r["client"].get(URL + "/" + document["id"] + "/posting-review")
    assert response.status_code == 200, response.text
    return response.json()


def post_body(r, document, **patch):
    inspected = review(r, document)
    return {**dict(request_key=str(uuid4()), row_version=inspected["receipt"]["row_version"],
        confirmation_token=inspected["confirmation_token"]), **patch}


def post(r, document, data=None):
    return r["client"].post(URL + "/" + document["id"] + "/post", json=data if data is not None else post_body(r, document))


def counts(r):
    with r["db"].read() as session:
        return {name: session.connection().exec_driver_sql("SELECT count(*) FROM " + name).scalar()
                for name in TABLES + ["inventory_lot", "stock_position"]}


def positions(r, product=None):
    response = r["client"].get("/api/stock-positions")
    assert response.status_code == 200, response.text
    return [p for p in response.json() if p["item_id"] == (product or r["products"][0])["id"]]


def test_draft_crud_signed_precision_unique_number_and_no_inventory_creation(receiving):
    r = receiving; c = r["client"]; before_counts = counts(r)
    data = body(r, quantity="-0.123456789012345678901234567890123456789")
    response = c.post(URL, json=data); assert response.status_code == 201, response.text
    document = response.json()
    assert document["posting_state"] == "DRAFT" and document["posting_id"] is None
    assert document["quantity"] == data["quantity"] and document["source"]["unit_code"] == "BOOK"
    assert document["source"]["product_id"] == r["products"][0]["id"]
    assert source(r)["received_qty_base"] == "0" and source(r)["remaining_qty_base"] == "1.25"
    assert c.post(URL, json=data).status_code == 200
    assert c.post(URL, json={**data, "note": "different same key"}).status_code == 409
    assert c.post(URL, json={**data, "request_key": str(uuid4())}).status_code == 409
    assert c.get(URL + "?q=隔離").json()[0]["id"] == document["id"]
    assert c.get(URL + "?q=%").json() == []
    updated = c.put(URL + "/" + document["id"], json=edit(document, quantity="0")).json()
    assert updated["quantity"] == "0" and updated["row_version"] == document["row_version"] + 1
    assert updated["created_at"] == document["created_at"] and updated["created_by"] == document["created_by"]
    assert c.put(URL + "/" + document["id"], json=edit(document)).status_code == 409
    assert c.delete(URL + "/" + document["id"] + "?row_version=1").status_code == 409
    assert c.delete(URL + "/" + document["id"] + "?row_version=" + str(updated["row_version"])).status_code == 204
    assert c.post(URL, json=data).status_code == 410
    assert counts(r) == before_counts


def test_authoritative_review_is_readonly_then_real_good_only_inbound_and_audit(receiving):
    r = receiving; document = create(r); before = snapshot(r["path"])
    inspected = review(r, document)
    assert snapshot(r["path"]) == before
    assert (inspected["source"]["good_qty_base"], inspected["source"]["defective_qty_base"],
        inspected["source"]["received_qty_base"], inspected["source"]["remaining_qty_base"]) == ("1.25", "0.25", "0", "1.25")
    assert (inspected["current_stock_qty_base"], inspected["expected_stock_qty_base"], inspected["remaining_after_qty_base"]) == ("0", "1.25", "0")
    assert inspected["policy"]["posting_allowed"] and inspected["confirmation_token"]
    response = post(r, document); assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["posting_state"] == "POSTED" and saved["posting_id"]
    assert len(saved["movements"]) == 1
    movement = saved["movements"][0]
    assert movement["qty_base_delta"] == "1.25" and movement["receipt_id"] == document["id"]
    assert movement["item_id"] == r["products"][0]["id"] and movement["warehouse_id"] == r["warehouse"]["id"]
    assert movement["lot_code"] == "LOT-FG-QA" and movement["lot_id"]
    assert positions(r)[0]["on_hand_qty_base"] == "1.25"
    assert (source(r)["received_qty_base"], source(r)["remaining_qty_base"]) == ("1.25", "0")
    original = r["completions"][0]
    assert r["client"].get("/api/production-completions/" + original["id"]).json() == original
    assert r["client"].get("/api/work-orders/" + original["work_order_id"]).json()["approval_state"] == "DRAFT"
    with r["db"].read() as session:
        con = session.connection()
        lot = con.exec_driver_sql("SELECT origin_kind,supplier_id FROM inventory_lot WHERE id=?", (movement["lot_id"],)).one()
        assert tuple(lot) == ("MANUFACTURED", None)
        assert con.exec_driver_sql("SELECT typeof(qty_base_delta) FROM finished_goods_entry").scalar() == "text"
        assert con.exec_driver_sql("SELECT erp_decimal_sum(qty_base_delta) FROM physical_stock_ledger WHERE position_id=?", (movement["position_id"],)).scalar() == "1.25"


def test_partial_receipts_cumulate_to_exact_good_and_exhausted_source_rejects_more(receiving):
    r = receiving
    for index, quantity in enumerate(["0.75", "0.5"]):
        document = create(r, "PART-" + str(index), quantity=quantity)
        response = post(r, document); assert response.status_code == 201, response.text
    assert (source(r)["received_qty_base"], source(r)["remaining_qty_base"]) == ("1.25", "0")
    assert positions(r)[0]["on_hand_qty_base"] == "1.25"
    extra = create(r, "PART-EXCESS", quantity="0.0000000000000000001")
    before = snapshot(r["path"])
    response = r["client"].get(URL + "/" + extra["id"] + "/posting-review")
    assert response.status_code in {409, 422}
    assert post(r, extra, dict(request_key=str(uuid4()), row_version=1, confirmation_token="invalid")).status_code in {409, 422}
    assert snapshot(r["path"]) == before and counts(r)["finished_goods_entry"] == 2


def test_two_completion_sources_same_product_keep_independent_limits_and_reuse_lot(receiving):
    r = receiving; second = add_completion(r, "COMP-INDEPENDENT", good="0.5", defective="8")
    one = create(r, "INDEPENDENT-A"); assert post(r, one).status_code == 201
    r["completions"].append(second); r["products"].append(r["products"][0])
    two = create(r, "INDEPENDENT-B", source_index=2, quantity="0.5")
    assert source(r, second)["remaining_qty_base"] == "0.5"
    assert post(r, two).status_code == 201
    assert source(r)["received_qty_base"] == "1.25" and source(r, second)["received_qty_base"] == "0.5"
    assert positions(r)[0]["on_hand_qty_base"] == "1.75" and counts(r)["inventory_lot"] == 1


def test_confirmed_good_above_work_order_plan_is_actual_receiving_limit(receiving):
    r = receiving; completion = add_completion(r, "COMP-OVER-PLAN", good="4", defective="0.25")
    r["completions"].append(completion); r["products"].append(r["products"][0])
    document = create(r, "RECEIVE-REAL-GOOD", source_index=2, quantity="4")
    assert post(r, document).status_code == 201
    assert positions(r)[0]["on_hand_qty_base"] == "4" and source(r, completion)["remaining_qty_base"] == "0"
    assert r["client"].get("/api/work-orders/" + completion["work_order_id"]).json()["planned_qty_base"] == "2.5"


def test_zero_good_defective_only_source_never_authorizes_receiving(receiving):
    r = receiving; completion = add_completion(r, "COMP-DEFECT-ONLY", good="0", defective="2")
    r["completions"].append(completion); r["products"].append(r["products"][0])
    assert source(r, completion)["good_qty_base"] == "0"
    document = create(r, "DEFECT-NOT-STOCK", source_index=2, quantity="1")
    before = snapshot(r["path"])
    assert r["client"].get(URL + "/" + document["id"] + "/posting-review").status_code in {409, 422}
    assert post(r, document, dict(request_key=str(uuid4()), row_version=1, confirmation_token="invalid")).status_code in {409, 422}
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("quantity", ["-0.25", "0", "1.250000000000000000000000000001"])
def test_signed_zero_and_over_good_drafts_persist_but_post_rejects_without_mutations(receiving, quantity):
    r = receiving; document = create(r, quantity=quantity); before = snapshot(r["path"])
    assert current(r, document)["quantity"] == quantity
    assert r["client"].get(URL + "/" + document["id"] + "/posting-review").status_code in {409, 422}
    assert post(r, document, dict(request_key=str(uuid4()), row_version=1, confirmation_token="invalid")).status_code in {409, 422}
    assert snapshot(r["path"]) == before


def test_exact_tiny_and_large_quantities_have_no_rounding_scale_or_magnitude_cap(receiving):
    r = receiving
    for index, quantity in enumerate(["0.123456789012345678901234567890123456789", "9" * 5000 + ".125"]):
        completion = add_completion(r, "COMP-EXACT-" + str(index), good=quantity, defective="0")
        r["completions"].append(completion); r["products"].append(r["products"][0])
        document = create(r, "RECEIVE-EXACT-" + str(index), source_index=len(r["completions"])-1,
                          quantity=quantity, lot_code="EXACT-" + str(index))
        response = post(r, document); assert response.status_code == 201, response.text
        assert response.json()["movements"][0]["qty_base_delta"] == quantity
        assert source(r, completion)["received_qty_base"] == quantity and source(r, completion)["remaining_qty_base"] == "0"


def test_draft_source_missing_stale_substitution_float_and_invalid_date_reject(receiving):
    r = receiving; c = r["client"]; completion = create_completion(r, "UNCONFIRMED-SOURCE")
    data = body(r)
    for patch in [dict(completion_id=completion["id"], completion_version=completion["row_version"]),
        dict(completion_id=str(uuid4())), dict(completion_version=99), dict(product_id=r["products"][1]["id"]),
        dict(work_order_id=r["orders"][1]["id"]), dict(unit_id=r["unit"]["id"]), dict(quantity=1.25),
        dict(quantity="NaN"), dict(quantity="1e3"), dict(business_date="2026-02-30"), dict(warehouse_id=str(uuid4()))]:
        before = snapshot(r["path"])
        assert c.post(URL, json={**data, **patch}).status_code in {409, 422}
        assert snapshot(r["path"]) == before
    listed = c.get(URL + "/sources").json()
    assert completion["id"] not in [s["completion_id"] for s in listed]


def test_nontracked_finished_product_uses_no_lot_and_preserves_actual_unit(receiving):
    r = receiving; document = create(r, "NO-LOT", source_index=1)
    inspected = review(r, document); assert inspected["source"]["lot_tracking"] is False
    response = post(r, document); assert response.status_code == 201, response.text
    movement = response.json()["movements"][0]
    assert movement["lot_id"] is None and movement["lot_code"] is None
    assert positions(r, r["products"][1])[0]["on_hand_qty_base"] == "1.25" and counts(r)["inventory_lot"] == 0


def test_new_lot_draft_and_preview_create_nothing_existing_lot_and_warehouse_are_exact(receiving):
    r = receiving; one = create(r, "LOT-FIRST", quantity="0.75")
    assert counts(r)["inventory_lot"] == counts(r)["stock_position"] == 0
    review(r, one); assert counts(r)["inventory_lot"] == counts(r)["stock_position"] == 0
    posted = post(r, one).json(); movement = posted["movements"][0]
    lots = r["client"].get(URL + "/lots", params=dict(completion_id=r["completions"][0]["id"])).json()
    assert movement["lot_id"] in [lot["id"] for lot in lots]
    second = create(r, "LOT-REUSE", quantity="0.5", lot_id=movement["lot_id"], lot_code=movement["lot_code"], warehouse_id=r["warehouse2"]["id"])
    assert review(r, second)["current_stock_qty_base"] == "0"
    assert post(r, second).status_code == 201
    assert counts(r)["inventory_lot"] == 1 and len(positions(r)) == 2
    assert {p["warehouse_id"]:p["on_hand_qty_base"] for p in positions(r)} == {r["warehouse"]["id"]:"0.75", r["warehouse2"]["id"]:"0.5"}


def test_wrong_lot_id_code_or_other_product_lot_rejects_without_rewriting_metadata(receiving):
    r = receiving; c = r["client"]; first = create(r, "LOT-VALID", quantity="0.5")
    response = post(r, first); assert response.status_code == 201, response.text
    movement = response.json()["movements"][0]
    for patch in [dict(lot_id=str(uuid4()),lot_code="MISSING"), dict(lot_id=movement["lot_id"],lot_code="WRONG-CODE"),
                  dict(lot_id=movement["lot_id"],lot_code=movement["lot_code"],completion_id=r["completions"][1]["id"],
                       completion_version=r["completions"][1]["row_version"])]:
        before = snapshot(r["path"])
        assert c.post(URL, json=body(r, "INVALID-LOT", **patch)).status_code in {409, 422}
        assert snapshot(r["path"]) == before


def test_sql_source_authenticity_and_other_tracked_product_lot_are_protected(receiving):
    r=receiving;c=r["client"];unconfirmed=create_completion(r,"SQL-DRAFT-SOURCE")
    document=create(r,"SQL-SOURCE-GUARD",quantity="0.25");before=snapshot(r["path"])
    for sql,params in [
        ("UPDATE finished_goods_receipt SET completion_id=?,completion_version=?,row_version=row_version+1 WHERE id=?",
         (unconfirmed["id"],unconfirmed["row_version"],document["id"])),
        ("UPDATE finished_goods_receipt SET completion_version=completion_version+1,row_version=row_version+1 WHERE id=?",(document["id"],))]:
        with pytest.raises(IntegrityError),r["db"].write() as session:
            session.connection().exec_driver_sql(sql,params)
        assert snapshot(r["path"])==before
    response=post(r,document);assert response.status_code==201,response.text
    movement=response.json()["movements"][0]
    product=c.post("/api/finished-goods",json=dict(code="FG-OTHER-TRACKED",name="另一追批成品",is_active=True,
        category_id=r["products"][0]["category_id"],base_unit_id=r["unit"]["id"],lot_tracking=True)).json()
    order=c.post("/api/work-orders",json=dict(request_key=str(uuid4()),document_no="WO-OTHER-TRACKED",product_id=product["id"],
        planned_qty_base="1",planned_start="2026-11-20",planned_finish="2026-11-21")).json()
    r["products"].append(product);r["orders"].append(order)
    r["completions"].append(add_completion(r,"COMP-OTHER-TRACKED",order=2))
    before=snapshot(r["path"])
    response=c.post(URL,json=body(r,"WRONG-TRACKED-LOT",source_index=2,lot_id=movement["lot_id"],lot_code=movement["lot_code"]))
    assert response.status_code==422,response.text
    assert snapshot(r["path"])==before


def test_identical_successful_request_replays_once_conflicting_identity_rejects(receiving):
    r = receiving; one = create(r); data = post_body(r, one)
    response = post(r, one, data); assert response.status_code == 201, response.text
    before = snapshot(r["path"])
    replay = post(r, one, data)
    assert replay.status_code == 200 and replay.json()["posting_id"] == response.json()["posting_id"]
    assert snapshot(r["path"]) == before
    assert post(r, one, {**data,"row_version":99}).status_code == 409
    assert post(r, one, {**data,"confirmation_token":"changed"}).status_code == 409
    other = create(r, "KEY-CONFLICT", source_index=1)
    other_data = post_body(r, other, request_key=data["request_key"])
    assert post(r, other, other_data).status_code == 409
    assert counts(r)["finished_goods_entry"] == 1


def test_concurrent_original_request_posts_exactly_once(receiving):
    r = receiving; document = create(r); data = post_body(r, document)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _:post(r, document, data), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201], [x.text for x in responses]
    assert counts(r)["finished_goods_entry"] == 1 and positions(r)[0]["on_hand_qty_base"] == "1.25"


def test_competing_receipts_cannot_jointly_overconsume_one_source(receiving):
    r = receiving; documents = [create(r, "COMPETE-" + str(i), quantity="0.75") for i in range(2)]
    data = [post_body(r, document) for document in documents]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pair:post(r,*pair), zip(documents,data)))
    assert sorted(response.status_code for response in responses) in [[201,409],[201,422]], [x.text for x in responses]
    assert (source(r)["received_qty_base"],source(r)["remaining_qty_base"]) == ("0.75","0.5")
    assert positions(r)[0]["on_hand_qty_base"] == "0.75" and counts(r)["finished_goods_entry"] == 1


def test_concurrent_draft_updates_one_cas_winner(receiving):
    r = receiving; document = create(r)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda quantity:r["client"].put(URL + "/" + document["id"],json=edit(document,quantity=quantity)).status_code,["0.25","0.5"]))
    assert sorted(statuses) == [200,409]


def test_signed_review_rejects_stale_receipt_and_changed_source_received_pool(receiving):
    r = receiving; one = create(r,"STALE-ROW",quantity="0.5"); data = post_body(r,one)
    updated = r["client"].put(URL + "/" + one["id"],json=edit(one,note="changed note")).json()
    before = snapshot(r["path"])
    assert post(r,updated,{**data,"row_version":updated["row_version"]}).status_code == 409
    assert snapshot(r["path"]) == before
    one_data = post_body(r,updated)
    other = create(r,"CHANGED-POOL",quantity="0.25"); assert post(r,other).status_code == 201
    before = snapshot(r["path"])
    assert post(r,updated,one_data).status_code == 409
    assert snapshot(r["path"]) == before


def test_ordinary_runtime_headers_body_flags_and_forged_tokens_cannot_post(receiving):
    r = receiving; document = create(r); valid = post_body(r,document)
    with TestClient(create_app(r["path"])) as ordinary:
        before = snapshot(r["path"])
        assert not ordinary.get(URL + "/policy").json()["posting_allowed"]
        assert ordinary.get(URL + "/" + document["id"] + "/posting-review").json()["confirmation_token"] is None
        for suffix,headers,data in [("?authorized=true",{},valid),("",{"X-ERP-Posting":"true"},valid),
                                    ("",{},dict(valid,authorized=True))]:
            response = ordinary.post(URL + "/" + document["id"] + "/post" + suffix,headers=headers,json=data)
            assert response.status_code == (422 if "authorized" in data else 403),response.text
        assert snapshot(r["path"]) == before
    for token in [None,"forged",valid["confirmation_token"]+"tampered"]:
        before = snapshot(r["path"])
        assert post(r,document,{**valid,"confirmation_token":token}).status_code in {403,409,422}
        assert snapshot(r["path"]) == before
    for headers in [{"Origin":"https://foreign.invalid"},{"Host":"foreign.invalid"}]:
        assert r["client"].post(URL + "/" + document["id"] + "/post",json=valid,headers=headers).status_code == 403


@pytest.mark.parametrize("stage",["entry","finalize"])
def test_late_failure_rolls_back_lot_position_entry_state_audit_and_key(receiving,stage):
    r = receiving; document = create(r); data = post_body(r,document)
    with r["db"].write() as session:
        sql = ("CREATE TRIGGER qa_fg_failure BEFORE INSERT ON finished_goods_entry BEGIN SELECT RAISE(ABORT,'injected FG entry failure'); END"
               if stage == "entry" else
               "CREATE TRIGGER qa_fg_failure BEFORE UPDATE ON finished_goods_posting WHEN NEW.posting_state='POSTED' BEGIN SELECT RAISE(ABORT,'injected FG finalization failure'); END")
        session.connection().exec_driver_sql(sql)
    before = snapshot(r["path"])
    response = post(r,document,data)
    assert response.status_code == 409,response.text
    assert snapshot(r["path"]) == before
    assert counts(r)["inventory_lot"] == counts(r)["stock_position"] == counts(r)["finished_goods_entry"] == 0
    with r["db"].write() as session:
        session.connection().exec_driver_sql("DROP TRIGGER qa_fg_failure")
    assert post(r,document,data).status_code == 201


def test_posted_document_entry_lot_and_position_resist_api_sql_update_delete_replace(receiving):
    r = receiving; document = create(r); response = post(r,document); assert response.status_code == 201,response.text
    saved = response.json(); before = snapshot(r["path"])
    assert r["client"].put(URL + "/" + document["id"],json=edit(saved,note="forbidden")).status_code == 409
    assert r["client"].delete(URL + "/" + document["id"],params=dict(row_version=saved["row_version"])).status_code == 409
    for table in TABLES + ["inventory_lot","stock_position"]:
        for action in ["UPDATE","DELETE","REPLACE"]:
            with pytest.raises(IntegrityError), r["db"].write() as session:
                con = session.connection()
                sql = "UPDATE " + table + " SET created_by=created_by" if action == "UPDATE" else "DELETE FROM " + table
                if action == "REPLACE":
                    columns=",".join(row[1] for row in con.exec_driver_sql("PRAGMA table_info(" + table + ")"))
                    sql="INSERT OR REPLACE INTO " + table + " (" + columns + ") SELECT " + columns + " FROM " + table + " LIMIT 1"
                con.exec_driver_sql(sql)
            assert snapshot(r["path"]) == before


def test_default_sql_connection_cannot_create_manufactured_lot_or_forged_posting(receiving):
    from backend.app.db import DEMO_ACTOR_ID
    from backend.app.receiving_models import InventoryLot
    from backend.app.finished_goods_models import FinishedGoodsPosting
    r=receiving;document=create(r,quantity="0.25");response=post(r,document)
    assert response.status_code==201,response.text
    movement=response.json()["movements"][0]
    other=create(r,"FORGED-POSTING",quantity="0.25",lot_id=movement["lot_id"],lot_code=movement["lot_code"])
    before=snapshot(r["path"])
    with pytest.raises(IntegrityError),r["db"].write() as session:
        session.add(InventoryLot(item_id=r["products"][0]["id"],lot_code="FORGED-LOT",origin_kind="MANUFACTURED",
            supplier_id=None,created_by=DEMO_ACTOR_ID))
        session.flush()
    assert snapshot(r["path"])==before
    with r["db"].read() as session:
        original=session.get(FinishedGoodsPosting,response.json()["posting_id"])
        forged={column.name:getattr(original,column.name) for column in FinishedGoodsPosting.__table__.columns}
        assert tuple(session.connection().exec_driver_sql("SELECT erp_finished_goods_authorized(),erp_finished_goods_document(),erp_finished_goods_digest(),erp_finished_goods_snapshot(),erp_finished_goods_time()").one())==(0,None,None,None,None)
    forged.update(id=str(uuid4()),receipt_id=other["id"],idempotency_key=str(uuid4()),source_version=1,posting_state="PREPARING")
    with pytest.raises(IntegrityError),r["db"].write() as session:
        session.connection().exec_driver_sql("UPDATE finished_goods_receipt SET row_version=2 WHERE id=?",(other["id"],))
        session.add(FinishedGoodsPosting(**forged));session.flush()
    assert snapshot(r["path"])==before


@pytest.mark.parametrize("delta",["0","-0.25","2"])
def test_authorized_transaction_sql_guard_rejects_tampered_inbound_leg(receiving,delta):
    from backend.app.finished_goods_models import FinishedGoodsEntry
    r=receiving;document=create(r);data=post_body(r,document);before=snapshot(r["path"])
    def alter(mapper,connection,target):
        target.qty_base_delta=delta
    event.listen(FinishedGoodsEntry,"before_insert",alter)
    try:
        response=post(r,document,data)
        assert response.status_code==409,response.text
    finally:
        event.remove(FinishedGoodsEntry,"before_insert",alter)
    assert snapshot(r["path"])==before and counts(r)["finished_goods_entry"]==0


def test_fresh_runtime_preserves_posted_stock_draft_and_original_retry(receiving):
    r = receiving; document = create(r); data = post_body(r,document); response = post(r,document,data)
    assert response.status_code == 201,response.text
    draft = create(r,"PERSIST-DRAFT",source_index=1,quantity="-0.125")
    before = snapshot(r["path"])
    app = create_local_demo_app(r["path"],backend_port=PORT,frontend_port=UI_PORT)
    with client_for(app) as restarted:
        replay=restarted.post(URL + "/" + document["id"] + "/post",json=data)
        assert replay.status_code == 200 and replay.json()["posting_id"] == response.json()["posting_id"]
        assert restarted.get(URL + "/" + draft["id"]).json()["quantity"] == "-0.125"
        assert restarted.get(URL + "/" + document["id"]).json()["posting_state"] == "POSTED"
        with r["db"].read() as session:
            assert session.connection().exec_driver_sql("SELECT erp_decimal_sum(qty_base_delta) FROM physical_stock_ledger").scalar() == "1.25"
    assert snapshot(r["path"]) == before


def seed_finished_goods_receipt_qa_database(path):
    """Fresh real purchasing/issue/return chain plus confirmed output; no FG stock."""
    from backend.tests.test_completions import seed_completion_qa_database
    metadata = seed_completion_qa_database(path)
    app = create_local_demo_app(path,backend_port=PORT,frontend_port=UI_PORT)
    with client_for(app) as client:
        r=dict(metadata,client=client,app=app,db=app.state.db,path=path)
        metadata["completions"]=[add_completion(r,"COMP-TEST-001"),add_completion(r,"COMP-NONTRACK",order=1),
                                 add_completion(r,"COMP-PARTIAL-SOURCE")]
    return metadata


def test_real_chain_keeps_raw3_purchasing_issue_return_completion_and_mrp_unchanged(tmp_path):
    path=artifact_parent(tmp_path)/"full-chain.sqlite"
    metadata=seed_finished_goods_receipt_qa_database(path)
    app=create_local_demo_app(path,backend_port=PORT,frontend_port=UI_PORT)
    with client_for(app) as client:
        r=dict(metadata,client=client,app=app,db=app.state.db,path=path)
        resources=["work-orders","production-completions","boms","routings","operation-definitions","purchase-orders",
                   "purchase-outstanding","purchase-receipts","supplier-returns","material-issues","material-returns"]
        projections={name:client.get("/api/"+name).json() for name in resources}
        raw_positions=[p for p in client.get("/api/stock-positions").json() if p["item_id"] in {x["id"] for x in metadata["raws"]}]
        from backend.tests.test_mrp import preview
        mrp_before=preview(r); before=snapshot(path)
        document=create(r,"FGRCV-TEST-001",lot_code="LOT-FG-R18-001")
        request=post_body(r,document)
        result=post(r,document,request);assert result.status_code==201,result.text
        after=snapshot(path)
        mutable=set(TABLES+["stock_create_receipt","inventory_lot","stock_position"])
        assert all(after["rows"][table]==rows for table,rows in before["rows"].items() if table not in mutable)
        assert all(row in after["rows"]["inventory_lot"] for row in before["rows"]["inventory_lot"])
        assert all(row in after["rows"]["stock_position"] for row in before["rows"]["stock_position"])
        assert all(client.get("/api/"+name).json()==value for name,value in projections.items())
        current_raw=[p for p in client.get("/api/stock-positions").json() if p["item_id"] in {x["id"] for x in metadata["raws"]}]
        assert current_raw==raw_positions
        assert next(p for p in current_raw if p["item_id"]==metadata["raws"][0]["id"])["on_hand_qty_base"]=="3"
        mrp_after=preview(r)
        assert mrp_after["materials"]==mrp_before["materials"] and mrp_after["orders"]==mrp_before["orders"]
        assert positions(r)[0]["on_hand_qty_base"]=="1.25"
        # Existing stock-domain keys remain mutually exclusive in both directions.
        other=create(r,"FG-CROSS-DOMAIN",source_index=1,quantity="0.25")
        with r["db"].read() as session:
            old_key=session.connection().exec_driver_sql("SELECT idempotency_key FROM material_issue_posting LIMIT 1").scalar()
        unchanged=snapshot(path)
        assert post(r,other,post_body(r,other,request_key=old_key)).status_code==409
        assert snapshot(path)==unchanged
        from backend.tests.test_material_returns import header as return_header,add as return_add,post as return_post,post_data
        return_doc=return_header(r,"RETURN-CROSS-DOMAIN");return_add(r,return_doc,"0.25")
        unchanged=snapshot(path)
        denied=return_post(r,return_doc,post_data(r,return_doc,request_key=request["request_key"]))
        assert denied.status_code==409,denied.text
        assert snapshot(path)==unchanged
