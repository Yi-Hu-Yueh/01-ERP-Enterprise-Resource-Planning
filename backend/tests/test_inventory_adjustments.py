"""Independent adjustment business/SQL checks on fresh synthetic SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import timedelta
from fractions import Fraction
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT, make_engine
from backend.app.local_demo import create_local_demo_app
from backend.app.main import create_app
from backend.tests.seed_inventory_adjustments_browser import seed, PORT, UI_PORT
from backend.tests.test_finished_goods_receipts import snapshot
from backend.tests import test_stocktakes as stk

URL = "/api/inventory-adjustments"
NEW = {"inventory_adjustment", "inventory_adjustment_posting", "inventory_adjustment_entry"}


def client_for(app):
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))


def artifact_directory():
    parent = ROOT / "artifacts" / "round24-backend-tests"
    parent.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="adjustment-", dir=parent))


@pytest.fixture(scope="session")
def adjustment_sources():
    path = artifact_directory() / "fresh-sources.sqlite"
    return path, seed(path)


@pytest.fixture
def adjusting(adjustment_sources):
    source, metadata = adjustment_sources
    path = artifact_directory() / "adjustments.sqlite"
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(path)) as target:
        original.backup(target)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        yield dict(metadata, client=client, app=app, db=app.state.db, path=path)


def body(r, number="ADJ-QA", **patch):
    return dict(request_key=str(uuid4()), document_no=number, business_date="2026-11-21",
        position_id=r["raw_position"]["id"], direction="DECREASE", quantity="0.25",
        reason="本機展示庫存調整驗收，非實際損耗", note="隔離後端驗證") | patch


def create(r, number="ADJ-QA", **patch):
    response = r["client"].post(URL, json=body(r, number, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def current(r, row):
    response = r["client"].get(URL + "/" + row["id"])
    assert response.status_code == 200, response.text
    return response.json()


def edit(row, **patch):
    return {key: row[key] for key in ["document_no", "business_date", "position_id", "direction",
        "quantity", "reason", "note", "row_version"]} | patch


def review(r, row):
    response = r["client"].get(URL + "/" + row["id"] + "/posting-review")
    assert response.status_code == 200, response.text
    return response.json()


def post_body(r, row, **patch):
    result = review(r, row)
    return dict(request_key=str(uuid4()), row_version=result["adjustment"]["row_version"],
        confirmation_token=result["confirmation_token"] or "") | patch


def post(r, row, data=None):
    return r["client"].post(URL + "/" + row["id"] + "/post",
        json=data if data is not None else post_body(r, row))


def cancel_body(row, **patch):
    return dict(request_key=str(uuid4()), row_version=row["row_version"]) | patch


def cancel(r, row, data=None):
    return r["client"].post(URL + "/" + row["id"] + "/cancel",
        json=data if data is not None else cancel_body(current(r, row)))


def stock(r, position):
    return next(p for p in r["client"].get("/api/stock-positions").json() if p["id"] == position["id"])["on_hand_qty_base"]


def old_rows(r):
    return {name: rows for name, rows in snapshot(r["path"])["rows"].items()
        if name not in NEW | {"stock_create_receipt"}}


def assert_integrity(r):
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(session.connection().exec_driver_sql("PRAGMA foreign_key_check")) == []


@contextmanager
def raw_driver(r):
    engine = make_engine(r["path"])
    connection = engine.raw_connection()
    driver = connection.driver_connection
    driver.execute("PRAGMA recursive_triggers=OFF")
    try:
        yield driver
    finally:
        driver.rollback()
        connection.close()
        engine.dispose()


def test_draft_crud_retry_search_stale_versions_and_no_stock_or_stocktake_mutation(adjusting):
    r = adjusting; c = r["client"]; original = old_rows(r); data = body(r)
    response = c.post(URL, json=data); assert response.status_code == 201, response.text
    row = response.json()
    assert row["state"] == "DRAFT" and row["movements"] == [] and row["posting_id"] is None
    assert row["signed_qty_base_delta"] == "-0.25" and row["expected_stock_qty_base"] == "2.75"
    assert c.post(URL, json=data).status_code == 200
    assert c.post(URL, json=data | dict(reason="different")).status_code == 409
    assert c.post(URL, json=body(r)).status_code == 409
    changed = c.put(URL + "/" + row["id"], json=edit(row, note="修訂調整草稿"))
    assert changed.status_code == 200, changed.text
    assert changed.json()["row_version"] == 2
    assert c.get(URL, params=dict(q="修訂調整草稿")).json()[0]["id"] == row["id"]
    assert c.get(URL, params=dict(q="%_'")).json() == []
    assert c.put(URL + "/" + row["id"], json=edit(row)).status_code == 409
    assert c.delete(URL + "/" + row["id"], params=dict(row_version=1)).status_code == 409
    assert c.delete(URL + "/" + row["id"], params=dict(row_version=2)).status_code == 204
    assert c.post(URL, json=data).status_code == 410
    assert old_rows(r) == original
    assert (stock(r, r["raw_position"]), stock(r, r["finished_position"])) == ("3", "0.75")


@pytest.mark.parametrize("patch", [dict(document_no=" "), dict(position_id=str(uuid4())),
    dict(business_date="2026-02-30"), dict(direction="SIGNED"), dict(state="POSTED"),
    dict(item_id=str(uuid4())), dict(warehouse_id=str(uuid4())), dict(lot_id=str(uuid4())),
    dict(unit_id=str(uuid4())), dict(item_kind="RAW"), dict(qty_base_delta="99"), dict(lot_code="UNREGISTERED-LOT")])
def test_invalid_header_source_and_client_supplied_lineage_or_state_reject(adjusting, patch):
    r = adjusting; before = snapshot(r["path"])
    assert r["client"].post(URL, json=body(r, **patch)).status_code == 422
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("quantity", [0.25, "1e-3", "NaN", "Infinity", "+0.25", "1,000"])
def test_non_decimal_text_inputs_reject_without_partial_records(adjusting, quantity):
    r = adjusting; before = snapshot(r["path"])
    assert r["client"].post(URL, json=body(r, quantity=quantity)).status_code == 422
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("patch", [dict(quantity="0"), dict(quantity="-0.25"), dict(reason=""), dict(reason=" \t "),
    dict(reason="\u3000\u00a0"), dict(reason="\n\r\v\f")])
def test_incomplete_draft_review_is_readonly_unpostable_and_never_flips_direction(adjusting, patch):
    r = adjusting; row = create(r, **patch); before = snapshot(r["path"])
    checked = review(r, row)
    assert not checked["posting_allowed"] and checked["confirmation_token"] is None
    assert checked["adjustment"]["blockers"]
    if "quantity" in patch:
        assert checked["signed_qty_base_delta"] is None
        assert checked["expected_stock_qty_base"] is None
    result = post(r, row, dict(request_key=str(uuid4()), row_version=row["row_version"], confirmation_token=""))
    assert result.status_code in {409, 422}, result.text
    assert snapshot(r["path"]) == before
    assert stock(r, r["raw_position"]) == "3"


def test_source_selector_existing_zero_mixed_kinds_units_and_warehouses(adjusting):
    r = adjusting; before = snapshot(r["path"]); c = r["client"]
    sources = c.get(URL + "/sources").json()
    assert {s["position_id"] for s in sources} == {p["id"] for p in r["positions"]}
    assert {s["item_kind"] for s in sources} == {"RAW", "FINISHED"}
    assert len({s["unit_id"] for s in sources}) == 2
    zero = next(s for s in sources if s["position_id"] == r["zero_position"]["id"])
    assert zero["current_stock_qty_base"] == "0" and not zero["frozen"]
    assert {s["warehouse_id"] for s in c.get(URL + "/sources", params=dict(warehouse_id=r["warehouse2"]["id"])).json()} == {r["warehouse2"]["id"]}
    assert all(s["item_id"] == r["finished_position"]["item_id"] for s in c.get(URL + "/sources", params=dict(item_id=r["finished_position"]["item_id"])).json())
    assert c.get(URL + "/sources", params=dict(q="%'")).json() == []
    assert snapshot(r["path"]) == before


def test_real_negative_positive_entries_exact_results_and_old_business_history_preserved(adjusting):
    r = adjusting; original = old_rows(r); historical = r["stocktake"]
    raw = create(r, "ADJ-RAW-DECREASE"); checked = review(r, raw)
    assert (checked["current_stock_qty_base"], checked["signed_qty_base_delta"], checked["expected_stock_qty_base"]) == ("3", "-0.25", "2.75")
    assert old_rows(r) == original and stock(r, r["raw_position"]) == "3"
    result = post(r, raw); assert result.status_code == 201, result.text
    raw = result.json(); assert raw["state"] == "POSTED" and raw["posting_id"]
    assert len(raw["movements"]) == 1 and raw["movements"][0]["qty_base_delta"] == "-0.25"
    assert raw["movements"][0]["position_id"] == r["raw_position"]["id"]
    assert (raw["stock_before_qty_base"], raw["posted_stock_qty_base"]) == ("3", "2.75")
    fg = create(r, "ADJ-FINISHED-INCREASE", position_id=r["finished_position"]["id"], direction="INCREASE")
    checked = review(r, fg)
    assert (checked["current_stock_qty_base"], checked["signed_qty_base_delta"], checked["expected_stock_qty_base"]) == ("0.75", "0.25", "1")
    result = post(r, fg); assert result.status_code == 201, result.text
    assert result.json()["movements"][0]["qty_base_delta"] == "0.25"
    assert (stock(r, r["raw_position"]), stock(r, r["finished_position"]), stock(r, r["zero_position"])) == ("2.75", "1", "0")
    assert old_rows(r) == original
    saved = r["client"].get("/api/stocktakes/" + historical["id"]).json()
    assert saved["state"] == "CLOSED" and saved["movements"] == [] and not saved["freeze_active"]
    assert [(l["position_id"], l["book_qty"], l["counted_qty"], l["variance"]) for l in saved["lines"]] == [(l["position_id"], l["book_qty"], l["counted_qty"], l["variance"]) for l in historical["lines"]]
    from backend.tests.test_mrp import preview
    mrp = preview(r)
    material = next(m for m in mrp["materials"] if m["material"]["id"] == r["raw_position"]["item_id"])
    assert (material["gross_demand_qty_base"], material["physical_stock_qty_base"], material["shortage_qty_base"]) == ("11.5625", "2.75", "8.8125")
    inquiry = r["client"].get("/api/outstanding-sales").json()
    order = next(o for o in inquiry["orders"] if o["id"] == r["order"]["id"])
    assert order["total_amount"] == "68.125" and order["document_no"].endswith(" ")
    assert order["quantity_totals"][0]["outstanding_qty_base"] == "3"
    assert r["client"].get("/api/purchase-outstanding").json()["currency_totals"][0]["total_amount"] == "192.25"
    assert_integrity(r)


def test_second_independent_document_uses_new_stock_and_keeps_first_posted_before_after(adjusting):
    r = adjusting; a = create(r, "FIRST"); b = create(r, "SECOND", quantity="0.125")
    stale = post_body(r, b)
    assert post(r, a).status_code == 201
    before = snapshot(r["path"]); assert post(r, b, stale).status_code == 409
    assert snapshot(r["path"]) == before
    assert review(r, b)["current_stock_qty_base"] == "2.75"
    result = post(r, b); assert result.status_code == 201, result.text
    assert (result.json()["stock_before_qty_base"], result.json()["posted_stock_qty_base"], stock(r, r["raw_position"])) == ("2.75", "2.625", "2.625")
    first = current(r, a)
    assert (first["stock_before_qty_base"], first["posted_stock_qty_base"], first["expected_stock_qty_base"], first["source"]["current_stock_qty_base"]) == ("3", "2.75", "2.75", "2.625")


def test_insufficient_stock_rejects_exact_boundary_then_zero_balance_can_increase(adjusting):
    r = adjusting; too_much = create(r, "TOO-MUCH", quantity="3.000000000000000000000000000000000000001")
    before = snapshot(r["path"]); checked = review(r, too_much)
    assert not checked["posting_allowed"] and checked["confirmation_token"] is None
    assert post(r, too_much).status_code in {409, 422}
    assert snapshot(r["path"]) == before
    exact = create(r, "TO-ZERO", quantity="3"); result = post(r, exact)
    assert result.status_code == 201 and stock(r, r["raw_position"]) == "0"
    increase = create(r, "FROM-ZERO", direction="INCREASE", quantity="0.125")
    assert post(r, increase).status_code == 201 and stock(r, r["raw_position"]) == "0.125"


def test_registered_zero_lot_and_other_warehouse_are_independently_adjustable(adjusting):
    r = adjusting; original = old_rows(r)
    a = create(r, "ZERO-LOT", position_id=r["zero_position"]["id"], direction="INCREASE", quantity="0.125")
    b = create(r, "OTHER-WH", position_id=r["other_position"]["id"], direction="INCREASE", quantity="0.75")
    assert post(r, a).status_code == 201 and post(r, b).status_code == 201
    assert (stock(r, r["zero_position"]), stock(r, r["other_position"]), stock(r, r["raw_position"]), stock(r, r["finished_position"])) == ("0.125", "0.75", "3", "0.75")
    assert old_rows(r) == original


def test_durable_draft_cancel_retains_reason_and_never_mutates_stock(adjusting):
    r = adjusting; row = create(r); original = old_rows(r); data = cancel_body(row)
    result = cancel(r, row, data); assert result.status_code == 200, result.text
    saved = result.json(); assert saved["state"] == "CANCELLED" and saved["cancelled_at"] and saved["cancelled_by"]
    assert saved["reason"] == row["reason"] and saved["movements"] == [] and saved["posting_id"] is None
    assert cancel(r, row, data).json() == saved
    assert cancel(r, row, data | dict(row_version=999)).status_code == 409
    assert r["client"].put(URL + "/" + row["id"], json=edit(saved, reason="changed")).status_code == 409
    assert r["client"].delete(URL + "/" + row["id"], params=dict(row_version=saved["row_version"])).status_code == 409
    assert post(r, row, dict(request_key=str(uuid4()), row_version=saved["row_version"], confirmation_token="")).status_code in {409, 422}
    assert old_rows(r) == original and stock(r, r["raw_position"]) == "3"


@pytest.mark.parametrize("phase", ["ACTIVE_FROZEN", "REVIEW_READY"])
def test_both_active_stocktake_states_block_adjustment_api_and_new_source_preview(adjusting, phase):
    r = adjusting; row = create(r); original_review = post_body(r, row)
    take = stk.start(r, stk.create(r, "FROZEN-QA"))
    if phase == "REVIEW_READY": take = stk.count_all(r, take)
    assert take["state"] == phase
    before = snapshot(r["path"]); checked = review(r, row)
    assert not checked["posting_allowed"] and checked["confirmation_token"] is None
    assert checked["source"]["frozen"]
    assert "凍結" in checked["message"] or "凍結" in " ".join(checked["adjustment"]["blockers"])
    assert post(r, row, original_review).status_code == 409
    assert snapshot(r["path"]) == before and stk.current(r, take)["freeze_active"]
    assert stock(r, r["raw_position"]) == "3"


@pytest.mark.parametrize("release", ["cancel", "close"])
def test_released_freeze_allows_new_review_but_intervening_cycle_invalidates_old_token(adjusting, release):
    r = adjusting; row = create(r); stale = post_body(r, row); take = stk.start(r, stk.create(r, "CYCLE-QA"))
    if release == "cancel": assert stk.cancel(r, take).status_code == 200
    else:
        take = stk.count_all(r, take); assert stk.close(r, take).status_code == 201
    assert not stk.current(r, take)["freeze_active"]
    before = snapshot(r["path"]); assert post(r, row, stale).status_code == 409
    assert snapshot(r["path"]) == before and stock(r, r["raw_position"]) == "3"
    assert post(r, row).status_code == 201 and stock(r, r["raw_position"]) == "2.75"


def test_frozen_warehouse_does_not_block_another_warehouse_adjustment(adjusting):
    r = adjusting; take = stk.start(r, stk.create(r, "FIRST-WH"))
    row = create(r, position_id=r["other_position"]["id"], direction="INCREASE", quantity="0.125")
    assert post(r, row).status_code == 201
    assert stock(r, r["other_position"]) == "0.125" and stock(r, r["raw_position"]) == "3"
    assert stk.current(r, take)["freeze_active"]


def test_identical_post_replay_conflict_and_fresh_runtime_keep_one_entry(adjusting):
    r = adjusting; row = create(r); data = post_body(r, row)
    result = post(r, row, data); assert result.status_code == 201, result.text
    posted = result.json(); before = snapshot(r["path"])
    assert post(r, row, data).status_code == 200
    assert post(r, row, data | dict(request_key=str(uuid4()))).status_code == 409
    assert post(r, row, data | dict(confirmation_token="changed")).status_code == 409
    with client_for(create_local_demo_app(r["path"], backend_port=PORT, frontend_port=UI_PORT)) as client:
        replay = client.post(URL + "/" + row["id"] + "/post", json=data)
        assert replay.status_code == 200 and replay.json()["posting_id"] == posted["posting_id"]
    with TestClient(create_app(r["path"])) as client:
        assert client.post(URL + "/" + row["id"] + "/post", json=data).status_code == 200
    assert snapshot(r["path"]) == before and stock(r, r["raw_position"]) == "2.75"


def test_concurrent_same_request_commits_one_posting_and_entry(adjusting):
    r = adjusting; row = create(r); data = post_body(r, row)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: post(r, row, data), range(2)))
    assert sorted(response.status_code for response in results) == [200, 201]
    assert results[0].json()["posting_id"] == results[1].json()["posting_id"]
    assert len(current(r, row)["movements"]) == 1 and stock(r, r["raw_position"]) == "2.75"


def test_concurrent_negative_adjustments_cannot_jointly_overspend_the_position(adjusting):
    r = adjusting; a = create(r, "FIRST-NEGATIVE", quantity="2"); b = create(r, "SECOND-NEGATIVE", quantity="2")
    requests = [(row, post_body(r, row)) for row in [a, b]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pair: post(r, *pair), requests))
    assert sorted(response.status_code for response in responses) == [201, 409]
    rows = [current(r, row) for row in [a, b]]
    assert sorted(row["state"] for row in rows) == ["DRAFT", "POSTED"]
    assert sum(len(row["movements"]) for row in rows) == 1 and stock(r, r["raw_position"]) == "1"
    loser = next(row for row in rows if row["state"] == "DRAFT")
    assert not review(r, loser)["posting_allowed"] and post(r, loser).status_code in {409, 422}


def test_concurrent_start_and_adjustment_cannot_make_a_stale_frozen_snapshot(adjusting):
    r = adjusting; row = create(r); data = post_body(r, row); take = stk.create(r, "RACING-START")
    def activate(): return r["client"].post("/api/stocktakes/" + take["id"] + "/start", json=stk.start_body(take))
    with ThreadPoolExecutor(max_workers=2) as pool:
        posting = pool.submit(post, r, row, data); starting = pool.submit(activate)
        result, activated = posting.result(), starting.result()
    assert activated.status_code == 201, activated.text
    assert result.status_code in {201, 409}, result.text
    expected = "2.75" if result.status_code == 201 else "3"
    assert stk.line_for(activated.json(), r["raw_position"])["book_qty"] == expected
    assert stock(r, r["raw_position"]) == expected and activated.json()["freeze_active"]


@pytest.mark.parametrize("stage", ["after_entry", "posting_finish", "before_final"])
def test_injected_failures_roll_back_entry_posting_document_audit_and_allow_same_key_retry(adjusting, stage):
    r = adjusting; row = create(r); data = post_body(r, row); before = snapshot(r["path"]); reached = []
    def inject(connection, cursor, statement, parameters, context, executemany):
        lower = statement.lower()
        match = ((stage == "after_entry" and lower.startswith("insert into inventory_adjustment_entry"))
            or (stage == "posting_finish" and lower.startswith("update inventory_adjustment_posting set"))
            or (stage == "before_final" and lower.startswith("update inventory_adjustment set") and "posted_at" in lower))
        if match:
            reached.append(True)
            raise IntegrityError(statement, parameters, RuntimeError("injected adjustment " + stage))
    hook = "after_cursor_execute" if stage == "after_entry" else "before_cursor_execute"
    event.listen(r["db"].engine, hook, inject)
    try:
        response = post(r, row, data); assert response.status_code == 409, response.text
    finally: event.remove(r["db"].engine, hook, inject)
    assert reached and snapshot(r["path"]) == before
    assert current(r, row)["state"] == "DRAFT" and stock(r, r["raw_position"]) == "3"
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT erp_adjustment_authorized()").scalar() == 0
    assert post(r, row, data).status_code == 201 and stock(r, r["raw_position"]) == "2.75"


def test_stale_draft_version_or_other_document_confirmation_rejects_without_partial_state(adjusting):
    r = adjusting; a = create(r, "FIRST"); b = create(r, "SECOND"); old = post_body(r, a)
    changed = r["client"].put(URL + "/" + a["id"], json=edit(a, note="source review is stale"))
    assert changed.status_code == 200
    before = snapshot(r["path"])
    assert post(r, changed.json(), old).status_code == 409
    assert post(r, b, old | dict(row_version=b["row_version"], request_key=str(uuid4()))).status_code in {403, 409}
    assert snapshot(r["path"]) == before


def test_changed_source_version_invalidates_preview_without_changing_stock(adjusting):
    r = adjusting; row = create(r); data = post_body(r, row)
    warehouse = r["client"].get("/api/warehouses/" + row["warehouse_id"]).json()
    changed = r["client"].put("/api/warehouses/" + warehouse["id"],
        json={key: warehouse[key] for key in ["code", "name", "address", "mrp_included", "is_active", "row_version"]}
        | dict(name="已更新來源名稱的隔離倉庫"))
    assert changed.status_code == 200, changed.text
    before = snapshot(r["path"])
    assert post(r, row, data).status_code == 409
    assert snapshot(r["path"]) == before and stock(r, r["raw_position"]) == "3"
    assert post(r, row).status_code == 201


def test_wrong_runtime_host_origin_forgery_expired_and_future_signatures_reject(adjusting, monkeypatch):
    from backend.app import adjustment_authorization as auth
    r = adjusting; row = create(r); data = post_body(r, row); before = snapshot(r["path"])
    with TestClient(create_app(r["path"])) as ordinary:
        assert not ordinary.get(URL + "/policy").json()["posting_allowed"]
        assert ordinary.post(URL + "/" + row["id"] + "/post", json=data, headers={"X-Local-Demo": "true"}).status_code == 403
    for headers in [{"Host": "foreign.invalid"}, {"Origin": "https://foreign.invalid"}]:
        assert r["client"].post(URL + "/" + row["id"] + "/post", json=data, headers=headers).status_code == 403
    assert post(r, row, data | dict(confirmation_token="forged")).status_code == 403
    actual = auth.observed_time
    for shift in [timedelta(minutes=11), timedelta(minutes=-1)]:
        monkeypatch.setattr(auth, "observed_time", lambda shift=shift: actual() + shift)
        assert post(r, row, data).status_code == 409
    monkeypatch.setattr(auth, "observed_time", actual)
    with client_for(create_local_demo_app(r["path"], backend_port=PORT, frontend_port=UI_PORT)) as other:
        assert other.post(URL + "/" + row["id"] + "/post", json=data).status_code == 403
    assert snapshot(r["path"]) == before


def test_private_grant_is_bound_to_one_physical_connection_and_clears_afterward(adjusting, monkeypatch):
    from backend.app import inventory_adjustments as api
    r = adjusting; row = create(r); actual = api.verified_confirmation; checked = []
    @contextmanager
    def verify(session, parent, token, current_snapshot, fail):
        with actual(session, parent, token, current_snapshot, fail) as grant:
            assert session.connection().exec_driver_sql("SELECT erp_adjustment_authorized()").scalar() == 1
            with r["db"].read() as other:
                assert other.connection().exec_driver_sql("SELECT erp_adjustment_authorized()").scalar() == 0
                checked.append(True)
            yield grant
    monkeypatch.setattr(api, "verified_confirmation", verify)
    assert post(r, row).status_code == 201 and checked == [True]
    with r["db"].read() as session:
        assert tuple(session.connection().exec_driver_sql("SELECT erp_adjustment_authorized(),erp_adjustment_document(),erp_adjustment_digest(),erp_adjustment_snapshot(),erp_adjustment_time()").one()) == (0, None, None, None, None)


@pytest.mark.parametrize("delta", ["0", "0.25", "-0.5"])
def test_even_authorized_post_cannot_insert_wrong_zero_sign_or_quantity(adjusting, delta):
    from backend.app.adjustment_models import InventoryAdjustmentEntry
    r = adjusting; row = create(r); data = post_body(r, row); before = snapshot(r["path"])
    def alter(mapper, connection, target): target.qty_base_delta = delta
    event.listen(InventoryAdjustmentEntry, "before_insert", alter)
    try:
        response = post(r, row, data); assert response.status_code == 409, response.text
    finally: event.remove(InventoryAdjustmentEntry, "before_insert", alter)
    assert snapshot(r["path"]) == before and stock(r, r["raw_position"]) == "3"


@pytest.mark.parametrize("field", ["position_id", "leg_no"])
def test_even_authorized_post_cannot_move_another_real_position_or_create_another_leg(adjusting, field):
    from backend.app.adjustment_models import InventoryAdjustmentEntry
    r = adjusting; row = create(r); data = post_body(r, row); before = snapshot(r["path"])
    def alter(mapper, connection, target):
        setattr(target, field, r["finished_position"]["id"] if field == "position_id" else 2)
    event.listen(InventoryAdjustmentEntry, "before_insert", alter)
    try:
        response = post(r, row, data); assert response.status_code == 409, response.text
    finally: event.remove(InventoryAdjustmentEntry, "before_insert", alter)
    assert snapshot(r["path"]) == before
    assert (stock(r, r["raw_position"]), stock(r, r["finished_position"])) == ("3", "0.75")


@pytest.mark.parametrize("state", ["ACTIVE_FROZEN", "REVIEW_READY"])
def test_freeze_is_rechecked_inside_real_signed_grant_and_sql_not_only_http(adjusting, monkeypatch, state):
    from backend.app import inventory_adjustments as api
    r = adjusting; row = create(r); data = post_body(r, row); take = stk.create(r, "GRANTED-FREEZE")
    actual = api.verified_confirmation; captured = []
    @contextmanager
    def verify(session, parent, token, current_snapshot, fail):
        with actual(session, parent, token, current_snapshot, fail) as grant:
            # Simulate a buggy internal caller activating a legitimate stocktake
            # after validation but before ledger writes, on the very same writer.
            from backend.app.stocktake_authorization import edit_grant
            from backend.app.stocktake_models import Stocktake, StocktakeLine
            from backend.app.models import Item, now_utc
            from backend.app.receiving_models import StockPosition
            from backend.app.stock_ledger import on_hand
            from backend.app.db import DEMO_ACTOR_ID
            from sqlalchemy import select
            doc = session.get(Stocktake, take["id"])
            with edit_grant(session, doc.id, "START"):
                doc.state, doc.freeze_active = "STARTING", True
                doc.snapshot_at, doc.started_by = now_utc(), DEMO_ACTOR_ID
                doc.start_key, doc.start_fingerprint = str(uuid4()), "a" * 64
                doc.row_version += 1; session.flush()
                for position in session.scalars(select(StockPosition).where(StockPosition.warehouse_id == doc.warehouse_id)):
                    item = session.get(Item, position.item_id)
                    session.add(StocktakeLine(stocktake_id=doc.id, position_id=position.id, item_id=item.id,
                        item_kind=item.item_kind, unit_id=item.base_unit_id, warehouse_id=position.warehouse_id,
                        lot_id=position.lot_id, book_qty=on_hand(session, position.id), created_by=DEMO_ACTOR_ID))
                session.flush(); doc.state = "ACTIVE_FROZEN"; session.flush()
            if state == "REVIEW_READY":
                with edit_grant(session, doc.id, "COUNT"):
                    lines = list(session.scalars(select(StocktakeLine).where(StocktakeLine.stocktake_id == doc.id)))
                    for line in lines:
                        line.counted_qty, line.counted_at, line.counted_by = line.book_qty, now_utc(), DEMO_ACTOR_ID
                        line.row_version += 1
                    session.flush(); doc.state = "REVIEW_READY"; doc.row_version += 1; session.flush()
            captured.append(True)
            yield grant
    monkeypatch.setattr(api, "verified_confirmation", verify)
    before = snapshot(r["path"]); result = post(r, row, data)
    assert result.status_code == 409, result.text
    assert captured and snapshot(r["path"]) == before


@pytest.mark.parametrize("state", ["ACTIVE_FROZEN", "REVIEW_READY"])
def test_shared_flush_boundary_rejects_frozen_adjustment_before_any_insert_sql(adjusting, state):
    from backend.app.adjustment_models import InventoryAdjustmentEntry
    from backend.app.db import DEMO_ACTOR_ID
    r = adjusting; take = stk.start(r, stk.create(r, "COMMON-FROZEN"))
    if state == "REVIEW_READY": take = stk.count_all(r, take)
    before = snapshot(r["path"]); inserts = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith("insert into inventory_adjustment_entry"):
            inserts.append(statement)
    event.listen(r["db"].engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(IntegrityError, match="凍結|frozen"), r["db"].write() as session:
            session.add(InventoryAdjustmentEntry(posting_id=str(uuid4()), adjustment_id=str(uuid4()),
                position_id=r["raw_position"]["id"], qty_base_delta="-0.25", created_by=DEMO_ACTOR_ID))
            session.flush()
    finally: event.remove(r["db"].engine, "before_cursor_execute", observe)
    assert inserts == [] and snapshot(r["path"]) == before


def test_direct_sql_entry_and_posting_parent_mutations_are_frozen_independently(adjusting):
    r = adjusting; row = create(r); assert post(r, row).status_code == 201
    stk.start(r, stk.create(r, "SQL-FROZEN")); before = snapshot(r["path"])
    with raw_driver(r) as con:
        for table in ["inventory_adjustment_entry", "inventory_adjustment_posting"]:
            for sql in [f"UPDATE {table} SET created_by=created_by", f"DELETE FROM {table}",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} LIMIT 1"]:
                with pytest.raises(sqlite3.DatabaseError, match="(?i)frozen|凍結"):
                    con.execute(sql)
                con.rollback()
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("phase", ["POSTED", "CANCELLED"])
def test_terminal_api_sql_update_delete_replace_and_lot_reassignment_reject(adjusting, phase):
    r = adjusting; row = create(r)
    result = post(r, row) if phase == "POSTED" else cancel(r, row)
    assert result.status_code in {200, 201}, result.text
    saved = result.json(); before = snapshot(r["path"])
    assert r["client"].put(URL + "/" + row["id"], json=edit(saved, reason="rewrite")).status_code == 409
    assert r["client"].delete(URL + "/" + row["id"], params=dict(row_version=saved["row_version"])).status_code == 409
    tables = ["inventory_adjustment"] + (["inventory_adjustment_posting", "inventory_adjustment_entry"] if phase == "POSTED" else [])
    with raw_driver(r) as con:
        for table in tables:
            for action in ["UPDATE", "DELETE", "REPLACE"]:
                sql = (f"UPDATE {table} SET created_by=created_by" if action == "UPDATE" else
                    f"DELETE FROM {table}" if action == "DELETE" else
                    f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} LIMIT 1")
                with pytest.raises(sqlite3.DatabaseError): con.execute(sql)
                con.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            con.execute("UPDATE inventory_adjustment SET lot_id=? WHERE id=?", (r["zero_position"]["lot_id"], row["id"]))
        con.rollback()
    assert snapshot(r["path"]) == before


def test_default_sql_cannot_forge_posted_document_posting_or_entry(adjusting):
    r = adjusting; posted = create(r, "ORIGINAL"); assert post(r, posted).status_code == 201
    draft = create(r, "FORGED"); before = snapshot(r["path"])
    with raw_driver(r) as con:
        assert con.execute("SELECT erp_adjustment_authorized()").fetchone()[0] == 0
        sqls = [
            ("UPDATE inventory_adjustment SET state='POSTED',row_version=row_version+1,posted_at=created_at,posted_by=created_by WHERE id=?", (draft["id"],)),
            ("INSERT OR REPLACE INTO inventory_adjustment_posting SELECT * FROM inventory_adjustment_posting LIMIT 1", ()),
            ("INSERT OR REPLACE INTO inventory_adjustment_entry SELECT * FROM inventory_adjustment_entry LIMIT 1", ()),
        ]
        for sql, parameters in sqls:
            with pytest.raises(sqlite3.DatabaseError): con.execute(sql, parameters)
            con.rollback()
    assert snapshot(r["path"]) == before


def test_posted_zero_position_source_dimensions_cannot_be_rewritten(adjusting):
    r = adjusting; row = create(r, position_id=r["zero_position"]["id"], direction="INCREASE", quantity="0.125")
    assert post(r, row).status_code == 201
    before = snapshot(r["path"]); p = r["zero_position"]
    attempts = [
        ("UPDATE stock_position SET warehouse_id=? WHERE id=?", (r["warehouse2"]["id"], p["id"])),
        ("UPDATE stock_position SET lot_id=? WHERE id=?", (r["raw_position"]["lot_id"], p["id"])),
        ("UPDATE stock_position SET rowid=rowid+10000 WHERE id=?", (p["id"],)),
        ("INSERT OR REPLACE INTO stock_position SELECT * FROM stock_position WHERE id=?", (p["id"],)),
        ("UPDATE inventory_lot SET lot_code='FORGED-LOT' WHERE id=?", (p["lot_id"],)),
        ("UPDATE item SET base_unit_id=? WHERE id=?", (r["products"][0]["base_unit_id"], p["item_id"])),
        ("UPDATE item SET item_kind='FINISHED' WHERE id=?", (p["item_id"],)),
    ]
    with raw_driver(r) as con:
        for sql, parameters in attempts:
            with pytest.raises(sqlite3.DatabaseError): con.execute(sql, parameters)
            con.rollback()
    assert snapshot(r["path"]) == before


def test_cross_domain_key_reuse_rejects_in_both_directions(adjusting):
    r = adjusting; row = create(r)
    with r["db"].read() as session:
        old_keys = [session.connection().exec_driver_sql("SELECT idempotency_key FROM " + table + " LIMIT 1").scalar_one()
            for table in ["stock_posting", "supplier_return_posting", "material_issue_posting",
                "material_return_posting", "finished_goods_posting", "sales_shipment_posting",
                "customer_return_finalization", "stocktake_posting"]]
    for old_key in old_keys:
        before = snapshot(r["path"])
        assert post(r, row, post_body(r, row, request_key=old_key)).status_code == 409
        assert snapshot(r["path"]) == before
    data = post_body(r, row); assert post(r, row, data).status_code == 201
    endpoint, ordinary = stk.prepare_writer(r, "purchase")
    before = snapshot(r["path"])
    response = r["client"].post(endpoint, json=ordinary | dict(request_key=data["request_key"]))
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("kind", ["supplier", "issue", "material_return", "finished", "shipment", "customer_return"])
def test_adjustment_posting_key_is_rejected_by_each_other_ordinary_writer(adjusting, kind):
    r = adjusting; row = create(r); data = post_body(r, row)
    assert post(r, row, data).status_code == 201
    endpoint, ordinary = stk.prepare_writer(r, kind)
    before = snapshot(r["path"])
    response = r["client"].post(endpoint, json=ordinary | dict(request_key=data["request_key"]))
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before


def test_tiny_long_and_5000_digit_magnitudes_post_without_rounding_or_cap(adjusting):
    r = adjusting; original = old_rows(r)
    huge = "9" * 5000 + ".123456789012345678901234567890123456789"
    a = create(r, "HUGE-POSITIVE", position_id=r["zero_position"]["id"], direction="INCREASE", quantity=huge)
    result = post(r, a); assert result.status_code == 201, result.text
    assert result.json()["movements"][0]["qty_base_delta"] == huge and stock(r, r["zero_position"]) == huge
    tiny = "0.000000000000000000000000000000000000001"
    b = create(r, "TINY-NEGATIVE", quantity=tiny); result = post(r, b)
    assert result.status_code == 201 and result.json()["movements"][0]["qty_base_delta"] == "-" + tiny
    assert Fraction(stock(r, r["raw_position"])) == Fraction("3") - Fraction(tiny)
    assert old_rows(r) == original
    with r["db"].read() as session:
        assert set(session.connection().exec_driver_sql("SELECT typeof(qty_base_delta) FROM inventory_adjustment_entry").scalars()) == {"text"}
    assert_integrity(r)
