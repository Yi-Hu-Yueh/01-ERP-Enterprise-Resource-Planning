"""Independent return/restock business outcomes on fresh isolated SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from fractions import Fraction
from pathlib import Path
import tempfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT
from backend.app.local_demo import create_local_demo_app
from backend.app.main import create_app
from backend.tests.test_finished_goods_receipts import snapshot, create as receive_create, post as receive_post
from backend.tests.test_sales import get as get_order, header_edit, line_edit
from backend.tests.test_sales_shipments import seed_fast, create as ship_create, post as ship_post, source as sales_source

URL = "/api/customer-returns"
PORT, UI_PORT = 8038, 5201
TABLES = ["customer_return", "customer_return_finalization", "customer_return_entry"]


def client_for(app):
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))


def artifact_parent(tmp_path):
    if tmp_path.resolve().is_relative_to(ROOT / "artifacts"):
        return tmp_path
    parent = ROOT / "artifacts" / "round21-backend-tests"
    parent.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="customer-return-", dir=parent))


@pytest.fixture
def returning(tmp_path):
    path = artifact_parent(tmp_path) / "customer-returns.sqlite"
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        r = dict(seed_fast(client), app=app, db=app.state.db, path=path)
        shipment = ship_create(r, "SHIP-RETURN-SOURCE", quantity="0.75")
        response = ship_post(r, shipment)
        assert response.status_code == 201, response.text
        r["shipment"] = response.json()
        assert stock(r) == "0.5"
        yield r


def source(r, shipment_id=None):
    response = r["client"].get(URL + "/sources", params=dict(shipment_id=shipment_id or r["shipment"]["id"]))
    assert response.status_code == 200, response.text
    return next(s for s in response.json() if s["shipment_id"] == (shipment_id or r["shipment"]["id"]))


def body(r, number="CRET-QA", **patch):
    shipment = r["shipment"]
    return {**dict(request_key=str(uuid4()), document_no=number, shipment_id=shipment["id"],
        shipment_version=shipment["row_version"], business_date="2026-11-20", quantity="0.25",
        restock_eligible=True, reason="真實退貨 QA", note="獨立客戶退貨測試"), **patch}


def create(r, number="CRET-QA", **patch):
    response = r["client"].post(URL, json=body(r, number, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def current(r, document):
    response = r["client"].get(URL + "/" + document["id"])
    assert response.status_code == 200, response.text
    return response.json()


def edit(row, **patch):
    keys = ["document_no", "shipment_id", "shipment_version", "business_date", "quantity", "restock_eligible", "reason", "note", "row_version"]
    return {**{key: row[key] for key in keys}, **patch}


def review(r, document):
    response = r["client"].get(URL + "/" + document["id"] + "/finalization-review")
    assert response.status_code == 200, response.text
    return response.json()


def final_body(r, document, **patch):
    inspected = review(r, document)
    return {**dict(request_key=str(uuid4()), row_version=inspected["return_document"]["row_version"],
        confirmation_token=inspected["confirmation_token"]), **patch}


def finalize(r, document, data=None):
    return r["client"].post(URL + "/" + document["id"] + "/finalize", json=data if data is not None else final_body(r, document))


def stock(r, position=None):
    position = position or r["position"]
    return next(p for p in r["client"].get("/api/stock-positions").json() if p["id"] == position["id"])["on_hand_qty_base"]


def counts(r):
    with r["db"].read() as session:
        return {table: session.connection().exec_driver_sql("SELECT COUNT(*) FROM " + table).scalar()
            for table in TABLES + ["inventory_lot", "stock_position"]}


def historical_projection(value):
    """Exclude only live on-hand readback; preserve all source/event fields."""
    if isinstance(value, list):
        return [historical_projection(row) for row in value]
    return {key: item for key, item in value.items() if key != "current_stock_qty_base"}


def test_partial_025_05_restock_real_entries_cap_and_original_gross_sales_unchanged(returning):
    r = returning; c = r["client"]; original_order = get_order(r, r["order"])
    original_shipment = c.get("/api/sales-shipments/" + r["shipment"]["id"]).json(); initial = counts(r)
    a = create(r); before = snapshot(r["path"]); inspected = review(r, a)
    assert snapshot(r["path"]) == before
    assert (inspected["source"]["original_shipped_qty_base"], inspected["source"]["finalized_returned_qty_base"], inspected["source"]["remaining_returnable_qty_base"]) == ("0.75", "0", "0.75")
    assert (inspected["current_stock_qty_base"], inspected["expected_stock_qty_base"], inspected["remaining_after_qty_base"]) == ("0.5", "0.75", "0.5")
    first = finalize(r, a); assert first.status_code == 201, first.text
    saved = first.json(); assert saved["finalization_state"] == "POSTED" and stock(r) == "0.75"
    assert len(saved["movements"]) == 1 and saved["movements"][0]["qty_base_delta"] == "0.25"
    assert saved["movements"][0]["position_id"] == r["position"]["id"]
    for key in ["shipment_id", "sales_order_id", "sales_order_line_id", "customer_id", "product_id", "unit_id", "position_id", "warehouse_id", "lot_id"]:
        assert saved[key] == (r["shipment"]["id"] if key == "shipment_id" else r["shipment"][key])
    b = create(r, "CRET-QA-B", quantity="0.5"); assert finalize(r, b).status_code == 201
    assert stock(r) == "1.25" and (source(r)["finalized_returned_qty_base"], source(r)["remaining_returnable_qty_base"]) == ("0.75", "0")
    extra = create(r, "CRET-EXCESS", quantity="0.0001"); before = snapshot(r["path"])
    assert c.get(URL + "/" + extra["id"] + "/finalization-review").status_code == 409
    assert finalize(r, extra, dict(request_key=str(uuid4()), row_version=1, confirmation_token="forged")).status_code == 409
    assert snapshot(r["path"]) == before and get_order(r, r["order"]) == original_order
    assert historical_projection(c.get("/api/sales-shipments/" + r["shipment"]["id"]).json()) == historical_projection(original_shipment)
    assert (sales_source(r)["shipped_qty_base"], sales_source(r)["remaining_qty_base"]) == ("0.75", "0.5")
    assert counts(r)["inventory_lot"] == initial["inventory_lot"] and counts(r)["stock_position"] == initial["stock_position"]
    with r["db"].read() as session:
        con = session.connection()
        assert sorted(con.exec_driver_sql("SELECT qty_base_delta,work_order_id FROM physical_stock_ledger WHERE domain='CUSTOMER_RETURN'").all()) == [("0.25", None), ("0.5", None)]
        assert con.exec_driver_sql("SELECT typeof(qty_base_delta) FROM customer_return_entry LIMIT 1").scalar() == "text"


def test_record_only_consumes_allowance_but_never_increases_stock_or_entries(returning):
    r = returning; initial = counts(r); a = create(r, restock_eligible=False)
    inspected = review(r, a)
    assert (inspected["current_stock_qty_base"], inspected["expected_stock_qty_base"], inspected["remaining_after_qty_base"]) == ("0.5", "0.5", "0.5")
    response = finalize(r, a); assert response.status_code == 201, response.text
    result = response.json(); assert result["finalization_state"] == "RECORDED_NO_RESTOCK" and result["movements"] == []
    assert stock(r) == "0.5" and counts(r)["customer_return_entry"] == 0
    assert counts(r)["inventory_lot"] == initial["inventory_lot"] and counts(r)["stock_position"] == initial["stock_position"]
    assert (source(r)["finalized_returned_qty_base"], source(r)["remaining_returnable_qty_base"]) == ("0.25", "0.5")
    b = create(r, "MIXED-RESTOCK", quantity="0.5"); assert finalize(r, b).status_code == 201
    assert stock(r) == "1" and counts(r)["customer_return_entry"] == 1
    assert (source(r)["finalized_returned_qty_base"], source(r)["remaining_returnable_qty_base"]) == ("0.75", "0")


def test_draft_crud_durable_create_search_stale_delete_and_no_inventory_mutation(returning):
    r = returning; c = r["client"]; initial = counts(r); data = body(r); a = c.post(URL, json=data).json()
    assert a["finalization_state"] == "DRAFT" and a["movements"] == [] and stock(r) == "0.5"
    assert c.post(URL, json=data).status_code == 200
    assert c.post(URL, json={**data, "quantity": "0.5"}).status_code == 409
    assert c.post(URL, json=body(r)).status_code == 409
    changed = c.put(URL + "/" + a["id"], json=edit(a, quantity="-0.125", restock_eligible=False, reason="草稿修訂"))
    assert changed.status_code == 200, changed.text
    assert changed.json()["quantity"] == "-0.125"
    assert len(c.get(URL, params=dict(q="草稿修訂")).json()) == 1
    assert c.get(URL, params=dict(q="%")).json() == []
    assert c.put(URL + "/" + a["id"], json=edit(a, quantity="0.5")).status_code == 409
    assert c.delete(URL + "/" + a["id"], params=dict(row_version=1)).status_code == 409
    assert c.delete(URL + "/" + a["id"], params=dict(row_version=2)).status_code == 204
    assert c.post(URL, json=data).status_code == 410
    assert counts(r) == initial and stock(r) == "0.5" and source(r)["finalized_returned_qty_base"] == "0"


@pytest.mark.parametrize("quantity", ["0", "-0.25", "-0.000000000000000000000000000000000000001"])
def test_signed_zero_drafts_never_finalize_as_an_extra_shipment(returning, quantity):
    r = returning; a = create(r, quantity=quantity); before = snapshot(r["path"])
    assert r["client"].get(URL + "/" + a["id"] + "/finalization-review").status_code == 422
    assert finalize(r, a, dict(request_key=str(uuid4()), row_version=1, confirmation_token="forged")).status_code == 422
    assert snapshot(r["path"]) == before and stock(r) == "0.5"


def test_nonexistent_unposted_wrong_versions_and_client_identity_overrides_reject(returning):
    r = returning; c = r["client"]; draft = ship_create(r, "UNPOSTED-SOURCE", quantity="0.1")
    patches = [dict(shipment_id=str(uuid4())), dict(shipment_id=draft["id"], shipment_version=draft["row_version"]),
        dict(shipment_version=r["shipment"]["row_version"] + 1), dict(customer_id=r["customers"][1]["id"]),
        dict(product_id=r["products"][1]["id"]), dict(lot_id=str(uuid4())), dict(position_id=str(uuid4())),
        dict(unit_id=str(uuid4())), dict(sales_order_line_id=str(uuid4())), dict(document_no=" "),
        dict(business_date="2026-02-30"), dict(quantity=0.25), dict(quantity="NaN"), dict(quantity="1e3"),
        dict(restock_eligible="true"), dict(restock_eligible=1)]
    for index, patch in enumerate(patches):
        before = snapshot(r["path"])
        response = c.post(URL, json=body(r, "INVALID-" + str(index), **patch))
        assert response.status_code in {409, 422}, (patch, response.text)
        assert snapshot(r["path"]) == before
    missing = body(r, "MISSING-CHOICE"); del missing["restock_eligible"]
    assert c.post(URL, json=missing).status_code == 422
    assert [s["shipment_id"] for s in c.get(URL + "/sources").json()] == [r["shipment"]["id"]]


def test_separate_posted_shipments_keep_independent_allowances_on_the_same_lot(returning):
    r = returning; extra = ship_create(r, "SHIP-RETURN-OTHER", quantity="0.5")
    second = ship_post(r, extra); assert second.status_code == 201, second.text
    assert stock(r) == "0"
    a = create(r, quantity="0.75", restock_eligible=False); assert finalize(r, a).status_code == 201
    b = create(r, "SECOND-SOURCE-RETURN", shipment_id=second.json()["id"], shipment_version=second.json()["row_version"], quantity="0.5")
    assert finalize(r, b).status_code == 201 and stock(r) == "0.5"
    assert (source(r)["finalized_returned_qty_base"], source(r)["remaining_returnable_qty_base"]) == ("0.75", "0")
    other = source(r, second.json()["id"])
    assert (other["original_shipped_qty_base"], other["finalized_returned_qty_base"], other["remaining_returnable_qty_base"]) == ("0.5", "0.5", "0")


def test_mixed_concurrent_finalizations_cannot_jointly_exceed_original_shipment(returning):
    r = returning; a = create(r, "RACE-RESTOCK", quantity="0.5"); b = create(r, "RACE-NO-STOCK", quantity="0.5", restock_eligible=False)
    bodies = [final_body(r, a), final_body(r, b)]
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda pair: finalize(r, *pair), zip([a, b], bodies)))
    assert sorted(x.status_code for x in responses) == [201, 409], [x.text for x in responses]
    assert (source(r)["finalized_returned_qty_base"], source(r)["remaining_returnable_qty_base"]) == ("0.5", "0.25")
    winner = next(x.json() for x in responses if x.status_code == 201)
    restocked = winner["finalization_state"] == "POSTED"
    assert stock(r) == ("1" if restocked else "0.5") and counts(r)["customer_return_entry"] == int(restocked)


@pytest.mark.parametrize("restock", [True, False])
def test_original_duplicate_replay_survives_new_runtime_and_ordinary_readback(returning, restock):
    r = returning; a = create(r, restock_eligible=restock); data = final_body(r, a)
    with ThreadPoolExecutor(2) as pool: responses = list(pool.map(lambda _: finalize(r, a, data), range(2)))
    assert sorted(x.status_code for x in responses) == [200, 201]
    saved = responses[0].json(); before = snapshot(r["path"])
    assert finalize(r, a, data).status_code == 200
    with client_for(create_local_demo_app(r["path"], backend_port=PORT, frontend_port=UI_PORT)) as restarted:
        replay = restarted.post(URL + "/" + a["id"] + "/finalize", json=data)
        assert replay.status_code == 200 and replay.json()["finalization_id"] == saved["finalization_id"]
    with TestClient(create_app(r["path"])) as ordinary:
        replay = ordinary.post(URL + "/" + a["id"] + "/finalize", json=data)
        assert replay.status_code == 200 and replay.json()["finalization_id"] == saved["finalization_id"]
    assert snapshot(r["path"]) == before and counts(r)["customer_return_entry"] == int(restock)
    assert finalize(r, a, {**data, "request_key": str(uuid4())}).status_code == 409
    assert finalize(r, a, {**data, "confirmation_token": "changed"}).status_code == 409
    other = create(r, "OTHER-KEY-DOC", quantity="0.1")
    assert finalize(r, other, final_body(r, other, request_key=data["request_key"])).status_code == 409


def test_ordinary_runtime_flags_headers_foreign_tokens_expiry_and_future_reviews_reject(returning, monkeypatch):
    from backend.app import customer_return_authorization as auth
    r = returning; a = create(r); data = final_body(r, a); before = snapshot(r["path"])
    with TestClient(create_app(r["path"])) as ordinary:
        assert not ordinary.get(URL + "/policy").json()["posting_allowed"]
        assert ordinary.post(URL + "/" + a["id"] + "/finalize", json=data, headers={"X-Local-Demo": "true"}).status_code == 403
    for headers in [{"Origin": "https://foreign.invalid"}, {"Host": "foreign.invalid"}]:
        assert r["client"].post(URL + "/" + a["id"] + "/finalize", json=data, headers=headers).status_code == 403
    assert finalize(r, a, {**data, "confirmation_token": "forged"}).status_code == 403
    actual = auth.observed_time
    for delta in [timedelta(minutes=11), timedelta(minutes=-1)]:
        monkeypatch.setattr(auth, "observed_time", lambda delta=delta: actual() + delta)
        assert finalize(r, a, data).status_code == 409
    monkeypatch.setattr(auth, "observed_time", actual)
    with client_for(create_local_demo_app(r["path"], backend_port=PORT, frontend_port=UI_PORT)) as other:
        assert other.post(URL + "/" + a["id"] + "/finalize", json=data).status_code == 403
    assert snapshot(r["path"]) == before and stock(r) == "0.5"


def test_review_becomes_stale_after_draft_edit_or_another_finalized_return(returning):
    r = returning; c = r["client"]; a = create(r); data = final_body(r, a)
    updated = c.put(URL + "/" + a["id"], json=edit(a, restock_eligible=False, reason="改為不回庫"))
    assert updated.status_code == 200
    before = snapshot(r["path"]); assert finalize(r, updated.json(), data).status_code == 409
    assert snapshot(r["path"]) == before
    a = updated.json(); data = final_body(r, a)
    b = create(r, "OTHER-RETURN", quantity="0.1", restock_eligible=False); assert finalize(r, b).status_code == 201
    before = snapshot(r["path"]); assert finalize(r, a, data).status_code == 409
    assert snapshot(r["path"]) == before and stock(r) == "0.5"
    assert finalize(r, a).status_code == 201


@pytest.mark.parametrize("stage", ["entry", "restock-finalize", "record-finalize"])
def test_injected_entry_and_late_finalization_failure_rolls_back_every_write(returning, stage):
    r = returning; a = create(r, restock_eligible=stage != "record-finalize"); data = final_body(r, a)
    sql = ("CREATE TRIGGER qa_customer_return_failure BEFORE INSERT ON customer_return_entry BEGIN SELECT RAISE(ABORT,'injected return entry failure'); END"
        if stage == "entry" else "CREATE TRIGGER qa_customer_return_failure BEFORE UPDATE ON customer_return_finalization WHEN NEW.finalization_state<>'PREPARING' BEGIN SELECT RAISE(ABORT,'injected late return finalization failure'); END")
    with r["db"].write() as session: session.connection().exec_driver_sql(sql)
    before = snapshot(r["path"]); response = finalize(r, a, data)
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before and stock(r) == "0.5" and counts(r)["customer_return_finalization"] == 0
    with r["db"].write() as session: session.connection().exec_driver_sql("DROP TRIGGER qa_customer_return_failure")
    assert finalize(r, a, data).status_code == 201


@pytest.mark.parametrize("restock", [True, False])
def test_finalized_api_sql_update_delete_replace_immutability_and_source_preservation(returning, restock):
    r = returning; a = create(r, restock_eligible=restock); response = finalize(r, a); assert response.status_code == 201
    saved = response.json(); before = snapshot(r["path"])
    assert r["client"].put(URL + "/" + a["id"], json=edit(saved, note="forbidden")).status_code == 409
    assert r["client"].delete(URL + "/" + a["id"], params=dict(row_version=saved["row_version"])).status_code == 409
    for table in TABLES[:2] + ([TABLES[2]] if restock else []):
        for action in ["UPDATE", "DELETE", "REPLACE"]:
            with pytest.raises(IntegrityError), r["db"].write() as session:
                con = session.connection(); con.exec_driver_sql("PRAGMA recursive_triggers=OFF")
                columns = ",".join(x[1] for x in con.exec_driver_sql("PRAGMA table_info(" + table + ")"))
                sql = ("UPDATE " + table + " SET created_by=created_by" if action == "UPDATE" else "DELETE FROM " + table if action == "DELETE" else "INSERT OR REPLACE INTO " + table + " (" + columns + ") SELECT " + columns + " FROM " + table + " LIMIT 1")
                con.exec_driver_sql(sql)
            assert snapshot(r["path"]) == before
    assert r["client"].delete("/api/sales-shipments/" + r["shipment"]["id"], params=dict(row_version=r["shipment"]["row_version"])).status_code == 409
    assert source(r)["finalized_returned_qty_base"] == "0.25"


def test_safe_original_sales_edits_do_not_erase_gross_shipped_or_returned_history(returning):
    r = returning; c = r["client"]; a = create(r); assert finalize(r, a).status_code == 201
    order = get_order(r, r["order"]); line = order["lines"][0]
    response = c.put("/api/sales-orders/" + order["id"] + "/lines/" + line["id"], json=line_edit(r, order, line, ordered_qty="2", unit_price="44"))
    assert response.status_code == 200, response.text
    updated = get_order(r, order); assert c.put("/api/sales-orders/" + order["id"], json=header_edit(updated, note="合法後續備註")).status_code == 200
    assert (sales_source(r)["shipped_qty_base"], sales_source(r)["remaining_qty_base"]) == ("0.75", "1.25")
    assert (source(r)["finalized_returned_qty_base"], source(r)["remaining_returnable_qty_base"]) == ("0.25", "0.5")
    b = create(r, "AFTER-SAFE-EDITS", quantity="0.5", restock_eligible=False)
    assert finalize(r, b).status_code == 201 and stock(r) == "0.75"


def test_exact_tiny_positive_and_5000_digit_draft_without_rounding_or_cap(returning):
    r = returning; huge = "9" * 5000 + ".123456789012345678901234567890123456789"
    assert create(r, "HUGE-RETURN-DRAFT", quantity=huge)["quantity"] == huge
    tiny = "0.000000000000000000000000000000000000001"
    a = create(r, "TINY-RETURN", quantity=tiny); response = finalize(r, a); assert response.status_code == 201, response.text
    assert response.json()["movements"][0]["qty_base_delta"] == tiny
    assert Fraction(stock(r)) == Fraction("0.5") + Fraction(tiny)
    assert source(r)["finalized_returned_qty_base"] == tiny


@pytest.mark.parametrize("delta", ["0", "-0.25", "0.5"])
def test_authorized_stock_entry_rejects_zero_negative_or_wrong_positive_quantity(returning, delta):
    from backend.app.customer_return_models import CustomerReturnEntry
    r = returning; a = create(r); data = final_body(r, a); before = snapshot(r["path"])
    def alter(mapper, connection, target): target.qty_base_delta = delta
    event.listen(CustomerReturnEntry, "before_insert", alter)
    try:
        response = finalize(r, a, data); assert response.status_code == 409, response.text
    finally: event.remove(CustomerReturnEntry, "before_insert", alter)
    assert snapshot(r["path"]) == before and stock(r) == "0.5"


def test_operation_grant_is_denied_on_another_physical_connection(returning, monkeypatch):
    from backend.app import customer_returns as api
    r = returning; a = create(r); actual = api.verified_confirmation; checked = []
    @contextmanager
    def verify(session, row, token, current_snapshot, fail):
        with actual(session, row, token, current_snapshot, fail) as grant:
            assert session.connection().exec_driver_sql("SELECT erp_customer_return_authorized()").scalar() == 1
            with r["db"].read() as other:
                assert other.connection().exec_driver_sql("SELECT erp_customer_return_authorized()").scalar() == 0
                checked.append(True)
            yield grant
    monkeypatch.setattr(api, "verified_confirmation", verify)
    assert finalize(r, a).status_code == 201 and checked == [True]


def test_default_sql_cannot_forge_a_finalization_without_the_dedicated_grant(returning):
    r = returning; a = create(r); assert finalize(r, a).status_code == 201
    b = create(r, "FORGED-RETURN", quantity="0.1"); before = snapshot(r["path"])
    with r["db"].read() as session:
        assert tuple(session.connection().exec_driver_sql("SELECT erp_customer_return_authorized(),erp_customer_return_document(),erp_customer_return_digest(),erp_customer_return_snapshot(),erp_customer_return_time()").one()) == (0, None, None, None, None)
    with pytest.raises(IntegrityError), r["db"].write() as session:
        con = session.connection(); con.exec_driver_sql("UPDATE customer_return SET row_version=row_version+1 WHERE id=?", (b["id"],))
        columns = [x[1] for x in con.exec_driver_sql("PRAGMA table_info(customer_return_finalization)")]
        values = dict(zip(columns, con.exec_driver_sql("SELECT * FROM customer_return_finalization LIMIT 1").one()))
        values.update(id=str(uuid4()), return_id=b["id"], idempotency_key=str(uuid4()), finalization_state="PREPARING", source_version=1)
        con.exec_driver_sql("INSERT INTO customer_return_finalization (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", tuple(values[k] for k in columns))
    assert snapshot(r["path"]) == before


def test_full_real_chain_preserves_old_records_mrp_raw_stock_and_six_domain_keys(tmp_path):
    from backend.tests.seed_customer_returns_browser import seed
    from backend.tests.test_mrp import preview
    path = artifact_parent(tmp_path) / "return-full-chain.sqlite"; metadata = seed(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        r = dict(metadata, client=client, app=app, db=app.state.db, path=path)
        resources = ["purchase-orders", "purchase-outstanding", "purchase-receipts", "supplier-returns", "work-orders", "boms", "routings", "operation-definitions", "material-issues", "material-returns", "production-completions", "sales-orders", "sales-shipments", "finished-goods-receipts"]
        projections = {name: client.get("/api/" + name).json() for name in resources}; old_mrp = preview(r)
        document = create(r); request = final_body(r, document); before = snapshot(path)
        result = finalize(r, document, request); assert result.status_code == 201, result.text
        after = snapshot(path)
        assert all(after["rows"][t] == rows for t, rows in before["rows"].items() if t not in TABLES + ["stock_create_receipt"])
        for name, old in projections.items():
            actual = client.get("/api/" + name).json()
            if name in {"sales-shipments", "finished-goods-receipts"}:
                # Their live on-hand readback must reflect the genuine restock;
                # every immutable document/source/movement field stays equal.
                assert historical_projection(actual) == historical_projection(old)
            else:
                assert actual == old
        new_mrp = preview(r); assert (new_mrp["orders"], new_mrp["materials"]) == (old_mrp["orders"], old_mrp["materials"])
        raw_position = next(p for p in client.get("/api/stock-positions").json() if p["item_id"] == r["raws"][0]["id"])
        assert stock(r) == "0.75" and stock(r, raw_position) == "3"
        tables = ["stock_posting", "supplier_return_posting", "material_issue_posting", "material_return_posting", "finished_goods_posting", "sales_shipment_posting"]
        for index, table in enumerate(tables):
            with r["db"].read() as session: key = session.connection().exec_driver_sql("SELECT idempotency_key FROM " + table + " LIMIT 1").scalar_one()
            extra = create(r, "CROSS-RETURN-KEY-" + str(index), quantity="0.01"); unchanged = snapshot(path)
            rejected = finalize(r, extra, final_body(r, extra, request_key=key))
            assert rejected.status_code == 409, (table, rejected.text)
            assert snapshot(path) == unchanged
        completion = r["completions"][2]
        other = client.post("/api/finished-goods-receipts", json=dict(request_key=str(uuid4()), document_no="REVERSE-RETURN-KEY", completion_id=completion["id"], completion_version=completion["row_version"], warehouse_id=r["warehouse"]["id"], lot_id=r["position"]["lot_id"], lot_code=r["position"]["lot_code"], business_date="2026-11-20", quantity="0.25", note=None))
        assert other.status_code == 201, other.text
        reviewed = client.get("/api/finished-goods-receipts/" + other.json()["id"] + "/posting-review"); assert reviewed.status_code == 200
        unchanged = snapshot(path)
        denied = client.post("/api/finished-goods-receipts/" + other.json()["id"] + "/post", json=dict(request_key=request["request_key"], row_version=other.json()["row_version"], confirmation_token=reviewed.json()["confirmation_token"]))
        assert denied.status_code == 409, denied.text
        assert snapshot(path) == unchanged
