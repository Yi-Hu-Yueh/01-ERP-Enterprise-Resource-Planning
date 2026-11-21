"""Independent whole-warehouse business and bypass checks on synthetic SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import timedelta
from fractions import Fraction
import json
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT, DEMO_ACTOR_ID
from backend.app.local_demo import create_local_demo_app
from backend.app.main import create_app
from backend.app.stock_ledger import register_exact_sql
from backend.tests.seed_stocktakes_browser import seed, PORT, UI_PORT
from backend.tests.test_finished_goods_receipts import snapshot

URL = "/api/stocktakes"
NEW = ["stocktake", "stocktake_line", "stocktake_exception", "stocktake_posting", "stocktake_entry"]
WRITERS = ["stock_entry", "supplier_return_entry", "material_issue_entry", "material_return_entry",
    "finished_goods_entry", "sales_shipment_entry", "customer_return_entry"]


def client_for(app):
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))


def artifact_directory():
    parent = ROOT / "artifacts" / "round23-backend-tests"
    parent.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="stocktake-", dir=parent))


@pytest.fixture(scope="session")
def stocktake_sources():
    path = artifact_directory() / "fresh-sources.sqlite"
    metadata = seed(path)
    return path, metadata


@pytest.fixture
def counting(stocktake_sources):
    source, metadata = stocktake_sources
    path = artifact_directory() / "stocktake.sqlite"
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(path)) as copy:
        original.backup(copy)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        yield dict(metadata, client=client, app=app, db=app.state.db, path=path)


def body(r, number="STK-QA", **patch):
    return dict(request_key=str(uuid4()), document_no=number, warehouse_id=r["warehouse"]["id"],
        business_date="2026-11-21", note="獨立盤點驗證") | patch


def create(r, number="STK-QA", **patch):
    response = r["client"].post(URL, json=body(r, number, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def current(r, document):
    response = r["client"].get(URL + "/" + document["id"])
    assert response.status_code == 200, response.text
    return response.json()


def start_body(document, **patch):
    return dict(request_key=str(uuid4()), row_version=document["row_version"]) | patch


def start(r, document=None, data=None):
    document = document or create(r)
    response = r["client"].post(URL + "/" + document["id"] + "/start", json=data or start_body(document))
    assert response.status_code in {200, 201}, response.text
    return response.json()


def line_for(document, position):
    return next(line for line in document["lines"] if line["position_id"] == position["id"])


def count_body(document, line, quantity, **patch):
    return dict(row_version=line["row_version"], stocktake_version=document["row_version"],
        counted_qty=quantity, note="實際盤數") | patch


def save_count(r, document, position, quantity, **patch):
    document = current(r, document); line = line_for(document, position)
    response = r["client"].put(URL + "/" + document["id"] + "/lines/" + line["id"],
        json=count_body(document, line, quantity, **patch))
    assert response.status_code == 200, response.text
    return current(r, document)


def count_all(r, document, overrides=None):
    overrides = overrides or {}
    for line in current(r, document)["lines"]:
        document = save_count(r, document, dict(id=line["position_id"]), overrides.get(line["position_id"], line["book_qty"]))
    return current(r, document)


def closing_body(r, document, **patch):
    response = r["client"].get(URL + "/" + document["id"] + "/closing-review")
    assert response.status_code == 200, response.text
    reviewed = response.json()
    return dict(request_key=str(uuid4()), row_version=reviewed["stocktake"]["row_version"],
        confirmation_token=reviewed["confirmation_token"]) | patch


def close(r, document, data=None):
    return r["client"].post(URL + "/" + document["id"] + "/close",
        json=data if data is not None else closing_body(r, document))


def cancel(r, document, data=None):
    return r["client"].post(URL + "/" + document["id"] + "/cancel",
        json=data if data is not None else start_body(current(r, document)))


def stock(r, position):
    return next(p for p in r["client"].get("/api/stock-positions").json() if p["id"] == position["id"])["on_hand_qty_base"]


def old_rows(r):
    return {table: rows for table, rows in snapshot(r["path"])["rows"].items()
        if table not in NEW + ["stock_create_receipt"]}


def assert_integrity(r):
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(session.connection().exec_driver_sql("PRAGMA foreign_key_check")) == []


def test_draft_crud_idempotency_search_stale_versions_and_zero_stock_effect(counting):
    r = counting; c = r["client"]; original = old_rows(r); data = body(r)
    first = c.post(URL, json=data); assert first.status_code == 201, first.text
    row = first.json(); assert (row["state"], row["freeze_active"], row["snapshot_at"], row["lines"]) == ("DRAFT", False, None, [])
    assert c.post(URL, json=data).status_code == 200
    assert c.post(URL, json=data | dict(note="changed")).status_code == 409
    assert c.post(URL, json=body(r)).status_code == 409
    edit = {key: row[key] for key in ["document_no", "warehouse_id", "business_date", "note", "row_version"]}
    updated = c.put(URL + "/" + row["id"], json=edit | dict(note="修訂盤點草稿"))
    assert updated.status_code == 200, updated.text
    assert updated.json()["row_version"] == 2
    assert c.get(URL, params=dict(q="修訂盤點草稿")).json()[0]["id"] == row["id"]
    assert c.get(URL, params=dict(q="%" )).json() == []
    assert c.put(URL + "/" + row["id"], json=edit).status_code == 409
    assert c.delete(URL + "/" + row["id"], params=dict(row_version=1)).status_code == 409
    assert c.delete(URL + "/" + row["id"], params=dict(row_version=2)).status_code == 204
    assert c.post(URL, json=data).status_code == 410
    assert old_rows(r) == original
    assert (stock(r, r["raw_position"]), stock(r, r["finished_position"])) == ("3", "0.75")


@pytest.mark.parametrize("patch", [dict(document_no=" "), dict(warehouse_id=str(uuid4())),
    dict(business_date="2026-02-30"), dict(state="CLOSED"), dict(freeze_active=True),
    dict(scope_kind="PARTIAL"), dict(snapshot_at="2026-11-21T00:00:00Z")])
def test_invalid_header_and_client_controlled_lifecycle_are_rejected(counting, patch):
    r = counting; before = snapshot(r["path"])
    assert r["client"].post(URL, json=body(r, **patch)).status_code == 422
    assert snapshot(r["path"]) == before


def test_start_snapshots_every_position_mixed_kinds_lots_units_and_zero_exactly_once(counting):
    r = counting; original = old_rows(r); draft = create(r); data = start_body(draft)
    row = start(r, draft, data)
    positions = [p for p in r["client"].get("/api/stock-positions").json() if p["warehouse_id"] == r["warehouse"]["id"]]
    assert {line["position_id"] for line in row["lines"]} == {p["id"] for p in positions}
    assert len(row["lines"]) == len(positions) >= 3
    assert row["state"] == "ACTIVE_FROZEN" and row["freeze_active"] and row["snapshot_at"]
    assert {line["item_kind"] for line in row["lines"]} == {"RAW", "FINISHED"}
    assert {line["unit_id"] for line in row["lines"]} == {r["raws"][0]["base_unit_id"], r["products"][0]["base_unit_id"]}
    for position in positions:
        line = line_for(row, position)
        assert (line["book_qty"], line["counted_qty"], line["variance"]) == (position["on_hand_qty_base"], None, None)
        assert line["lot_id"] == position["lot_id"] and line["warehouse_id"] == position["warehouse_id"]
    assert line_for(row, r["zero_position"])["book_qty"] == "0"
    replay = start(r, draft, data); assert replay == row
    assert old_rows(r) == original
    assert_integrity(r)


def test_concurrent_same_warehouse_start_one_winner_other_warehouse_independent(counting):
    r = counting; a = create(r, "RACE-A"); b = create(r, "RACE-B")
    def submit(row):
        return r["client"].post(URL + "/" + row["id"] + "/start", json=start_body(row))
    with ThreadPoolExecutor(2) as pool: responses = list(pool.map(submit, [a, b]))
    assert sorted(x.status_code for x in responses) in ([200, 409], [201, 409]), [x.text for x in responses]
    winner = next(x.json() for x in responses if x.status_code < 300)
    loser = current(r, a if winner["id"] != a["id"] else b)
    assert loser["state"] == "DRAFT" and loser["lines"] == [] and not loser["freeze_active"]
    other = start(r, create(r, "OTHER-WH", warehouse_id=r["warehouse2"]["id"]))
    assert other["freeze_active"] and other["warehouse_id"] != winner["warehouse_id"]
    assert cancel(r, winner).status_code == 200 and cancel(r, other).status_code == 200


def test_start_key_conflict_stale_source_warehouse_and_active_delete_rejected(counting):
    r = counting; c = r["client"]; a = create(r); data = start_body(a)
    frozen = start(r, a, data); before = snapshot(r["path"])
    assert c.post(URL + "/" + a["id"] + "/start", json=data | dict(row_version=9)).status_code == 409
    assert c.post(URL + "/" + a["id"] + "/start", json=start_body(frozen)).status_code == 409
    edit = {key: frozen[key] for key in ["document_no", "warehouse_id", "business_date", "note", "row_version"]}
    assert c.put(URL + "/" + a["id"], json=edit | dict(warehouse_id=r["warehouse2"]["id"])).status_code == 409
    assert c.delete(URL + "/" + a["id"], params=dict(row_version=frozen["row_version"])).status_code == 409
    assert snapshot(r["path"]) == before


def test_blank_count_is_not_zero_exact_long_count_edit_and_unit_grouped_variance(counting):
    r = counting; row = start(r); original = old_rows(r)
    zero = line_for(row, r["zero_position"]); assert zero["counted_qty"] is None
    row = save_count(r, row, r["zero_position"], "0")
    assert line_for(row, r["zero_position"])["counted_qty"] == "0"
    assert row["summary"]["counted"] == 1 and row["summary"]["uncounted"] == len(row["lines"]) - 1
    long = "123456789012345678901234567890123456789.123456789012345678901234567890123456789"
    row = save_count(r, row, r["raw_position"], long)
    line = line_for(row, r["raw_position"])
    assert line["counted_qty"] == long and Fraction(line["variance"]) == Fraction(long) - 3
    row = save_count(r, row, r["raw_position"], "2.5")
    assert line_for(row, r["raw_position"])["variance"] == "-0.5"
    row = save_count(r, row, r["finished_position"], "1")
    assert line_for(row, r["finished_position"])["variance"] == "0.25"
    row = count_all(r, row, {r["raw_position"]["id"]: "2.5", r["finished_position"]["id"]: "1"})
    assert row["state"] == "REVIEW_READY" and row["freeze_active"]
    assert (row["summary"]["positive"], row["summary"]["negative"], row["summary"]["zero"]) == (1, 1, len(row["lines"]) - 2)
    assert {group["unit_id"] for group in row["summary"]["unit_groups"]} == {line["unit_id"] for line in row["lines"]}
    row = save_count(r, row, r["zero_position"], None)
    assert row["state"] == "ACTIVE_FROZEN" and line_for(row, r["zero_position"])["counted_qty"] is None
    assert old_rows(r) == original


@pytest.mark.parametrize("quantity", ["-1", "-0.000000000000000000001", "NaN", "Infinity", "1e3", "", 0, 0.5])
def test_invalid_physical_counts_reject_without_defaulting_to_zero(counting, quantity):
    r = counting; row = start(r); line = line_for(row, r["raw_position"]); before = snapshot(r["path"])
    response = r["client"].put(URL + "/" + row["id"] + "/lines/" + line["id"], json=count_body(row, line, quantity))
    assert response.status_code == 422, response.text
    assert snapshot(r["path"]) == before


def test_stale_count_and_cross_document_line_rejected_without_stock_mutation(counting):
    r = counting; row = start(r); line = line_for(row, r["raw_position"]); data = count_body(row, line, "2.5")
    row = save_count(r, row, r["raw_position"], "2.5"); before = snapshot(r["path"])
    endpoint = URL + "/" + row["id"] + "/lines/" + line["id"]
    assert r["client"].put(endpoint, json=data).status_code == 409
    other = create(r, "OTHER-DOC"); stable = snapshot(r["path"])
    assert r["client"].put(URL + "/" + other["id"] + "/lines/" + line["id"], json=count_body(other, line, "0")).status_code in {404, 409}
    assert snapshot(r["path"]) == stable
    assert line_for(current(r, row), r["raw_position"])["book_qty"] == "3"


def test_actual_negative_and_positive_variance_entries_match_final_stock_old_history_unchanged(counting):
    r = counting; original = old_rows(r); row = count_all(r, start(r), {
        r["raw_position"]["id"]: "2.5", r["finished_position"]["id"]: "1"})
    before = snapshot(r["path"]); data = closing_body(r, row)
    assert snapshot(r["path"]) == before
    response = close(r, row, data); assert response.status_code == 201, response.text
    closed = response.json(); assert (closed["state"], closed["freeze_active"]) == ("CLOSED", False)
    assert closed["posting_id"] and closed["closed_at"] and closed["closed_by"]
    assert {(m["position_id"], m["qty_base_delta"]) for m in closed["movements"]} == {
        (r["raw_position"]["id"], "-0.5"), (r["finished_position"]["id"], "0.25")}
    assert (stock(r, r["raw_position"]), stock(r, r["finished_position"])) == ("2.5", "1")
    assert old_rows(r) == original
    with r["db"].read() as session:
        con = session.connection()
        assert set(con.exec_driver_sql("SELECT qty_base_delta FROM physical_stock_ledger WHERE domain='STOCKTAKE'").scalars()) == {"-0.5", "0.25"}
        assert con.exec_driver_sql("SELECT COUNT(*) FROM stocktake_entry").scalar() == 2
        assert set(con.exec_driver_sql("SELECT typeof(qty_base_delta) FROM stocktake_entry").scalars()) == {"text"}
        posting = con.exec_driver_sql("SELECT authorization_basis,confirmation_digest,confirmation_snapshot FROM stocktake_posting").one()
        assert posting[0] == "LOCAL_DEMO_MANUAL" and len(posting[1]) == 64
        assert row["id"] in posting[2] and r["warehouse"]["id"] in posting[2]
    assert close(r, row, data).status_code == 200
    assert_integrity(r)


def test_zero_variance_close_creates_no_entries_and_releases_freeze(counting):
    r = counting; original = old_rows(r); row = count_all(r, start(r))
    response = close(r, row); assert response.status_code == 201, response.text
    closed = response.json(); assert closed["state"] == "CLOSED" and not closed["freeze_active"]
    assert closed["posting_id"] and closed["movements"] == []
    assert old_rows(r) == original
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT COUNT(*) FROM stocktake_entry").scalar() == 0
    again = start(r, create(r, "NEXT-STK")); assert again["freeze_active"]


def test_unmatched_observation_persists_blocks_reconciled_close_and_cancellation_retains_counts(counting):
    r = counting; c = r["client"]; row = count_all(r, start(r)); original = old_rows(r)
    data = dict(request_key=str(uuid4()), stocktake_version=row["row_version"], description="發現未登記紅色紙張／袋上批號 UNKNOWN-9")
    response = c.post(URL + "/" + row["id"] + "/exceptions", json=data)
    assert response.status_code in {200, 201}, response.text
    row = current(r, row); assert len(row["exceptions"]) == 1 and row["exceptions"][0]["description"] == data["description"]
    assert c.post(URL + "/" + row["id"] + "/exceptions", json=data).status_code == 200
    before = snapshot(r["path"])
    review = c.get(URL + "/" + row["id"] + "/closing-review")
    assert review.status_code in {200, 409}
    if review.status_code == 200: assert not review.json()["confirmation_token"]
    assert close(r, row, dict(request_key=str(uuid4()), row_version=row["row_version"], confirmation_token="forged")).status_code == 409
    assert snapshot(r["path"]) == before
    result = cancel(r, row); assert result.status_code == 200, result.text
    saved = result.json(); assert saved["state"] == "CANCELLED" and not saved["freeze_active"]
    assert saved["exceptions"] == row["exceptions"] and saved["lines"] == row["lines"]
    assert saved["movements"] == [] and old_rows(r) == original


def test_unentered_count_cannot_close_and_no_grant_available_outside_request(counting):
    r = counting; row = start(r); before = snapshot(r["path"])
    assert close(r, row, dict(request_key=str(uuid4()), row_version=row["row_version"], confirmation_token="forged")).status_code == 409
    assert snapshot(r["path"]) == before
    with r["db"].read() as session:
        assert tuple(session.connection().exec_driver_sql("SELECT erp_stocktake_authorized(),erp_stocktake_document(),erp_stocktake_edit_action(),erp_stocktake_edit_document()").one()) == (0, None, None, None)


def test_start_and_close_original_retry_persist_across_new_runtime_and_ordinary_app(counting):
    r = counting; draft = create(r); start_data = start_body(draft); row = start(r, draft, start_data)
    row = count_all(r, row, {r["raw_position"]["id"]: "2.5", r["finished_position"]["id"]: "1"})
    data = closing_body(r, row)
    with ThreadPoolExecutor(2) as pool: results = list(pool.map(lambda _: close(r, row, data), range(2)))
    assert sorted(x.status_code for x in results) == [200, 201], [x.text for x in results]
    result = results[0].json(); before = snapshot(r["path"])
    for app in [create_local_demo_app(r["path"], backend_port=PORT, frontend_port=UI_PORT), create_app(r["path"])]:
        with client_for(app) as restarted:
            again = restarted.post(URL + "/" + row["id"] + "/close", json=data)
            assert again.status_code == 200 and again.json()["posting_id"] == result["posting_id"]
            replay = restarted.post(URL + "/" + row["id"] + "/start", json=start_data)
            assert replay.status_code == 200 and replay.json()["state"] == "CLOSED"
    assert snapshot(r["path"]) == before
    assert close(r, row, data | dict(request_key=str(uuid4()))).status_code == 409
    assert close(r, row, data | dict(confirmation_token="changed")).status_code == 409


def test_restart_active_freeze_saved_counts_and_explicit_recovery_cancel(counting):
    r = counting; row = save_count(r, start(r), r["raw_position"], "2.5")
    with client_for(create_local_demo_app(r["path"], backend_port=PORT, frontend_port=UI_PORT)) as restarted:
        saved = restarted.get(URL + "/" + row["id"]).json()
        assert saved["state"] == "ACTIVE_FROZEN" and saved["freeze_active"]
        assert line_for(saved, r["raw_position"])["counted_qty"] == "2.5"
        assert line_for(saved, r["raw_position"])["book_qty"] == "3"
        result = restarted.post(URL + "/" + row["id"] + "/cancel", json=start_body(saved))
        assert result.status_code == 200 and result.json()["state"] == "CANCELLED"
    assert (stock(r, r["raw_position"]), stock(r, r["finished_position"])) == ("3", "0.75")


def test_ordinary_runtime_forged_cross_document_expired_future_and_restart_tokens_reject(counting, monkeypatch):
    from backend.app import stocktake_authorization as auth
    r = counting; row = count_all(r, start(r)); data = closing_body(r, row); before = snapshot(r["path"])
    with client_for(create_app(r["path"])) as ordinary:
        assert not ordinary.get(URL + "/policy").json()["posting_allowed"]
        assert ordinary.post(URL + "/" + row["id"] + "/close", json=data, headers={"X-Local-Demo": "true"}).status_code == 403
    assert close(r, row, data | dict(confirmation_token="forged")).status_code == 403
    for headers in [{"Host": "foreign.invalid"}, {"Origin": "https://foreign.invalid"}]:
        assert r["client"].post(URL + "/" + row["id"] + "/close", json=data, headers=headers).status_code == 403
    actual = auth.observed_time
    for delta in [timedelta(minutes=11), timedelta(minutes=-1)]:
        monkeypatch.setattr(auth, "observed_time", lambda delta=delta: actual() + delta)
        assert close(r, row, data).status_code == 409
    monkeypatch.setattr(auth, "observed_time", actual)
    with client_for(create_local_demo_app(r["path"], backend_port=PORT, frontend_port=UI_PORT)) as restarted:
        assert restarted.post(URL + "/" + row["id"] + "/close", json=data).status_code == 403
    assert snapshot(r["path"]) == before


def test_stale_review_after_count_edit_and_cross_document_confirmation_reject(counting):
    r = counting; row = count_all(r, start(r)); data = closing_body(r, row)
    row = save_count(r, row, r["raw_position"], "2.5"); before = snapshot(r["path"])
    assert close(r, row, data).status_code == 409
    assert close(r, row, data | dict(row_version=row["row_version"])).status_code == 409
    assert snapshot(r["path"]) == before
    assert cancel(r, row).status_code == 200
    other = count_all(r, start(r, create(r, "OTHER-STK"))); before = snapshot(r["path"])
    assert close(r, other, data | dict(row_version=other["row_version"])).status_code in {403, 409}
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("stage", ["entry", "posting", "freeze_release"])
def test_injected_closure_failure_rolls_back_every_entry_identity_and_freeze_release(counting, stage):
    r = counting; row = count_all(r, start(r), {r["raw_position"]["id"]: "2.5", r["finished_position"]["id"]: "1"})
    data = closing_body(r, row); before = snapshot(r["path"]); seen = 0
    def inject(connection, cursor, statement, parameters, context, executemany):
        nonlocal seen
        lowered = statement.lower()
        if ((stage == "entry" and lowered.startswith("insert into stocktake_entry"))
            or (stage == "posting" and lowered.startswith("update stocktake_posting"))
            or (stage == "freeze_release" and lowered.startswith("update stocktake set") and "closed_at" in lowered)):
            seen += 1
            raise IntegrityError(statement, parameters, RuntimeError("injected stocktake " + stage))
    event.listen(r["db"].engine, "before_cursor_execute", inject)
    try:
        response = close(r, row, data); assert response.status_code in {409, 422}, response.text
    finally: event.remove(r["db"].engine, "before_cursor_execute", inject)
    assert seen >= 1 and snapshot(r["path"]) == before
    persisted = current(r, row); assert persisted["state"] == "REVIEW_READY" and persisted["freeze_active"]
    assert close(r, row, data).status_code == 201
    assert_integrity(r)


def raw_connection(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON"); con.execute("PRAGMA recursive_triggers=OFF")
    register_exact_sql(con)
    from backend.app import completion_authorization, finished_goods_authorization, sales_shipment_authorization, customer_return_authorization, stocktake_authorization
    for module in [completion_authorization, finished_goods_authorization, sales_shipment_authorization, customer_return_authorization, stocktake_authorization]:
        module.register_denied_sql(con)
    stocktake_authorization.register_edit_sql(con)
    return con


@pytest.mark.parametrize("table", WRITERS)
@pytest.mark.parametrize("delta", ["0.125", "-0.125"])
def test_direct_sql_each_existing_entry_writer_positive_negative_is_frozen(counting, table, delta):
    r = counting; start(r); before = snapshot(r["path"])
    with closing(raw_connection(r["path"])) as con:
        columns = [row[1] for row in con.execute('PRAGMA table_info("' + table + '")')]
        old = dict(zip(columns, con.execute('SELECT * FROM "' + table + '" LIMIT 1').fetchone()))
        assert old["position_id"] in {p["id"] for p in r["positions"] if p["warehouse_id"] == r["warehouse"]["id"]}
        old.update(id=str(uuid4()), qty_base_delta=delta)
        with pytest.raises(sqlite3.DatabaseError, match="(?i)frozen|凍結"):
            con.execute('INSERT INTO "' + table + '" (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(old[c] for c in columns))
        con.rollback()
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("operation", ["new_position", "move_position", "replace_position", "delete_position", "new_lot_position", "edit_lot", "replace_lot"])
def test_raw_sql_position_lot_mutation_and_replace_cannot_bypass_freeze(counting, operation):
    r = counting; start(r); before = snapshot(r["path"])
    with closing(raw_connection(r["path"])) as con:
        p = con.execute("SELECT * FROM stock_position WHERE id=?", (r["raw_position"]["id"],)).fetchone()
        with pytest.raises(sqlite3.DatabaseError):
            if operation == "new_position":
                con.execute("INSERT INTO stock_position SELECT ?,created_at,created_by,item_id,warehouse_id,lot_id FROM stock_position WHERE id=?", (str(uuid4()), p[0]))
            elif operation == "move_position":
                con.execute("UPDATE stock_position SET warehouse_id=? WHERE id=?", (r["warehouse2"]["id"], p[0]))
            elif operation == "replace_position":
                con.execute("INSERT OR REPLACE INTO stock_position SELECT * FROM stock_position WHERE id=?", (p[0],))
            elif operation == "delete_position": con.execute("DELETE FROM stock_position WHERE id=?", (p[0],))
            elif operation == "new_lot_position":
                lot_columns = [row[1] for row in con.execute("PRAGMA table_info(inventory_lot)")]
                lot = dict(zip(lot_columns, con.execute("SELECT * FROM inventory_lot WHERE id=?", (r["raw_position"]["lot_id"],)).fetchone()))
                lot.update(id=str(uuid4()), lot_code="NEW-FROZEN-LOT")
                con.execute("INSERT INTO inventory_lot (" + ','.join(lot_columns) + ") VALUES (" + ','.join('?' for _ in lot_columns) + ")", tuple(lot[c] for c in lot_columns))
                con.execute("INSERT INTO stock_position SELECT ?,created_at,created_by,item_id,warehouse_id,? FROM stock_position WHERE id=?", (str(uuid4()), lot["id"], p[0]))
            elif operation == "edit_lot": con.execute("UPDATE inventory_lot SET lot_code='CHANGED-FROZEN' WHERE id=?", (r["raw_position"]["lot_id"],))
            else: con.execute("INSERT OR REPLACE INTO inventory_lot SELECT * FROM inventory_lot WHERE id=?", (r["raw_position"]["lot_id"],))
        con.rollback()
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("phase", ["ACTIVE", "CLOSED", "CANCELLED"])
def test_sql_snapshot_lifecycle_and_final_history_are_immutable_including_replace(counting, phase):
    r = counting; row = count_all(r, start(r))
    if phase == "CLOSED": assert close(r, row).status_code == 201
    elif phase == "CANCELLED": assert cancel(r, row).status_code == 200
    before = snapshot(r["path"])
    statements = [
        ("UPDATE stocktake SET freeze_active=0 WHERE id=?", (row["id"],)),
        ("UPDATE stocktake SET state='CANCELLED',freeze_active=0 WHERE id=?", (row["id"],)),
        ("DELETE FROM stocktake WHERE id=?", (row["id"],)),
        ("INSERT OR REPLACE INTO stocktake SELECT * FROM stocktake WHERE id=?", (row["id"],)),
        ("UPDATE stocktake_line SET book_qty='999' WHERE stocktake_id=?", (row["id"],)),
        ("UPDATE stocktake_line SET counted_qty='999' WHERE stocktake_id=?", (row["id"],)),
        ("DELETE FROM stocktake_line WHERE stocktake_id=?", (row["id"],)),
        ("INSERT OR REPLACE INTO stocktake_line SELECT * FROM stocktake_line WHERE stocktake_id=? LIMIT 1", (row["id"],)),
    ]
    if phase == "CLOSED":
        statements += [("UPDATE stocktake_posting SET request_fingerprint='changed' WHERE stocktake_id=?", (row["id"],)),
            ("DELETE FROM stocktake_posting WHERE stocktake_id=?", (row["id"],)),
            ("INSERT OR REPLACE INTO stocktake_posting SELECT * FROM stocktake_posting WHERE stocktake_id=?", (row["id"],))]
    with closing(raw_connection(r["path"])) as con:
        for sql, parameters in statements:
            with pytest.raises(sqlite3.DatabaseError): con.execute(sql, parameters)
            con.rollback()
    assert snapshot(r["path"]) == before


def prepare_writer(r, kind, warehouse=None):
    """Create a legitimate ordinary posting request before activation freezes it."""
    from backend.tests import test_receiving as receiving, test_supplier_returns as supplier
    from backend.tests import test_material_issues as issues, test_local_demo_issues as issue_demo
    from backend.tests import test_material_returns as returns, test_finished_goods_receipts as finished
    from backend.tests import test_sales_shipments as shipments, test_customer_returns as customers
    c = r["client"]
    if kind == "purchase":
        purchase = c.get("/api/purchase-orders/" + r["sources"][0]["purchase_order_id"]).json()
        rr = dict(r, order=purchase)
        doc = receiving.header(rr, "RC-FROZEN-QA")
        receiving.line(rr, doc, "0.125", warehouse_id=(warehouse or r["warehouse"])["id"],
            lot_code="OTHER-WH-LOT" if warehouse else "LOT-R8-001",
            supplier_lot_code=None if warehouse else "SUPPLIER-REAL-LOT")
        endpoint = "/api/purchase-receipts/" + doc["id"] + "/post"
        data = dict(request_key=str(uuid4()), row_version=receiving.read(rr, doc)["row_version"])
    elif kind == "supplier":
        doc = supplier.header(r, "SRET-FROZEN-QA"); supplier.line(r, doc, "0.125", origin=1)
        endpoint = "/api/supplier-returns/" + doc["id"] + "/post"
        data = dict(request_key=str(uuid4()), row_version=supplier.read(r, doc)["row_version"])
    elif kind == "issue":
        doc = issues.header(r, "ISSUE-FROZEN-QA"); issues.add(r, doc, "0.125")
        endpoint = "/api/material-issues/" + doc["id"] + "/post"; data = issue_demo.confirmed_body(r, doc)
    elif kind == "material_return":
        doc = returns.header(r, "MRET-FROZEN-QA"); returns.add(r, doc, "0.125")
        endpoint = "/api/material-returns/" + doc["id"] + "/post"; data = returns.post_data(r, doc)
    elif kind == "finished":
        rr = dict(r, completions=[r["completions"][2], r["completions"][1]])
        doc = finished.create(rr, "FGRCV-FROZEN-QA", quantity="0.125", lot_code="FG-NEW-FROZEN-QA")
        endpoint = "/api/finished-goods-receipts/" + doc["id"] + "/post"; data = finished.post_body(rr, doc)
    elif kind == "shipment":
        doc = shipments.create(r, "SHIP-FROZEN-QA", quantity="0.125")
        endpoint = "/api/sales-shipments/" + doc["id"] + "/post"; data = shipments.post_body(r, doc)
    else:
        doc = customers.create(r, "CRET-FROZEN-QA", quantity="0.125")
        endpoint = "/api/customer-returns/" + doc["id"] + "/finalize"; data = customers.final_body(r, doc)
    return endpoint, data


@pytest.mark.parametrize("kind", ["purchase", "supplier", "issue", "material_return", "finished", "shipment", "customer_return"])
def test_every_real_ordinary_api_writer_is_blocked_and_rolls_back_while_frozen(counting, kind):
    r = counting; endpoint, data = prepare_writer(r, kind); start(r); before = snapshot(r["path"])
    response = r["client"].post(endpoint, json=data)
    assert response.status_code in {409, 422}, response.text
    assert "凍結" in response.text or "frozen" in response.text.lower(), response.text
    assert snapshot(r["path"]) == before


def test_another_warehouse_ordinary_posting_and_new_lot_remain_possible(counting):
    r = counting; endpoint, data = prepare_writer(r, "purchase", warehouse=r["warehouse2"])
    row = start(r); response = r["client"].post(endpoint, json=data)
    assert response.status_code == 201, response.text
    assert response.json()["movements"][0]["qty_base_delta"] == "0.125"
    actual = next(p for p in r["client"].get("/api/stock-positions").json() if p["lot_code"] == "OTHER-WH-LOT")
    assert actual["warehouse_id"] == r["warehouse2"]["id"] and actual["on_hand_qty_base"] == "0.125"
    assert current(r, row)["freeze_active"]
    assert (stock(r, r["raw_position"]), stock(r, r["finished_position"])) == ("3", "0.75")
    assert_integrity(r)


@pytest.mark.parametrize("stage", ["snapshot_line", "activate"])
def test_start_failure_rolls_back_freeze_snapshot_start_key_and_allows_clean_retry(counting, stage):
    r = counting; row = create(r); data = start_body(row); before = snapshot(r["path"]); seen = 0
    def inject(connection, cursor, statement, parameters, context, executemany):
        nonlocal seen
        lowered = statement.lower()
        if ((stage == "snapshot_line" and lowered.startswith("insert into stocktake_line"))
            or (stage == "activate" and lowered.startswith("update stocktake set state=") and "row_version" not in lowered)):
            seen += 1
            raise IntegrityError(statement, parameters, RuntimeError("injected start " + stage))
    event.listen(r["db"].engine, "before_cursor_execute", inject)
    try:
        response = r["client"].post(URL + "/" + row["id"] + "/start", json=data)
        assert response.status_code in {409, 422}, response.text
    finally: event.remove(r["db"].engine, "before_cursor_execute", inject)
    assert seen and snapshot(r["path"]) == before
    persisted = current(r, row)
    assert persisted["state"] == "DRAFT" and not persisted["freeze_active"] and persisted["lines"] == []
    assert start(r, row, data)["freeze_active"]


def test_movement_committed_before_start_is_included_in_snapshot(counting):
    r = counting; endpoint, data = prepare_writer(r, "purchase")
    response = r["client"].post(endpoint, json=data); assert response.status_code == 201, response.text
    assert stock(r, r["raw_position"]) == "3.125"
    row = start(r)
    assert line_for(row, r["raw_position"])["book_qty"] == "3.125"


def test_closed_variance_entries_refuse_update_delete_replace_and_forged_extra_entry(counting):
    r = counting; row = count_all(r, start(r), {r["raw_position"]["id"]: "2.5"})
    result = close(r, row); assert result.status_code == 201, result.text
    entry = result.json()["movements"][0]; before = snapshot(r["path"])
    with closing(raw_connection(r["path"])) as con:
        attempts = [
            ("UPDATE stocktake_entry SET qty_base_delta='-1' WHERE id=?", (entry["id"],)),
            ("DELETE FROM stocktake_entry WHERE id=?", (entry["id"],)),
            ("INSERT OR REPLACE INTO stocktake_entry SELECT * FROM stocktake_entry WHERE id=?", (entry["id"],)),
            ("INSERT INTO stocktake_entry SELECT ?,created_at,created_by,posting_id,stocktake_id,stocktake_line_id,position_id,leg_no,qty_base_delta FROM stocktake_entry WHERE id=?", (str(uuid4()), entry["id"])),
        ]
        for sql, parameters in attempts:
            with pytest.raises(sqlite3.DatabaseError): con.execute(sql, parameters)
            con.rollback()
    assert snapshot(r["path"]) == before


def test_raw_connection_without_application_functions_fails_closed(counting):
    r = counting; row = start(r); before = snapshot(r["path"])
    with closing(sqlite3.connect(r["path"])) as con:
        for sql in ["UPDATE stocktake SET freeze_active=0 WHERE id=?",
            "INSERT OR REPLACE INTO stocktake SELECT * FROM stocktake WHERE id=?"]:
            with pytest.raises(sqlite3.DatabaseError): con.execute(sql, (row["id"],))
            con.rollback()
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("table", ["stock_posting", "supplier_return_posting", "material_issue_posting",
    "material_return_posting", "finished_goods_posting", "sales_shipment_posting", "customer_return_finalization"])
def test_posted_ordinary_posting_replace_cannot_remove_ledger_participation_during_freeze(counting, table):
    r = counting; start(r); before = snapshot(r["path"])
    with closing(raw_connection(r["path"])) as con:
        with pytest.raises(sqlite3.DatabaseError, match="(?i)frozen|凍結"):
            con.execute('INSERT OR REPLACE INTO "' + table + '" SELECT * FROM "' + table + '" LIMIT 1')
        con.rollback()
    assert snapshot(r["path"]) == before


def test_draft_cancel_and_durable_cancel_retry_do_not_post_or_consume_another_document(counting):
    r = counting; row = create(r); data = start_body(row); original = old_rows(r)
    result = cancel(r, row, data); assert result.status_code == 200, result.text
    saved = result.json(); assert saved["state"] == "CANCELLED" and not saved["freeze_active"]
    assert saved["lines"] == [] and saved["movements"] == [] and saved["snapshot_at"] is None
    assert cancel(r, row, data).json() == saved
    assert cancel(r, row, data | dict(row_version=999)).status_code == 409
    other = create(r, "OTHER-CANCEL"); before = snapshot(r["path"])
    assert cancel(r, other, data).status_code == 409
    assert snapshot(r["path"]) == before and old_rows(r) == original


def test_stocktake_posting_key_cannot_be_reused_by_an_ordinary_posting_after_unfreeze(counting):
    r = counting; endpoint, ordinary = prepare_writer(r, "purchase")
    row = count_all(r, start(r)); data = closing_body(r, row)
    assert close(r, row, data).status_code == 201
    before = snapshot(r["path"])
    response = r["client"].post(endpoint, json=ordinary | dict(request_key=data["request_key"]))
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before


def test_5000_digit_count_and_tiny_variance_are_exact_without_quantity_cap(counting):
    r = counting; row = start(r); huge = "9" * 5000 + ".123456789012345678901234567890123456789"
    row = save_count(r, row, r["raw_position"], huge)
    assert line_for(row, r["raw_position"])["counted_qty"] == huge
    assert line_for(row, r["raw_position"])["variance"] == "9" * 4999 + "6.123456789012345678901234567890123456789"
    tiny_count = "0.750000000000000000000000000000000000001"
    row = count_all(r, row, {r["finished_position"]["id"]: tiny_count})
    response = close(r, row); assert response.status_code == 201, response.text
    movements = response.json()["movements"]
    assert len(movements) == 1 and movements[0]["qty_base_delta"] == "0.000000000000000000000000000000000000001"
    assert stock(r, r["finished_position"]) == tiny_count


@pytest.mark.parametrize("delta", ["0", "0.5", "-1"])
def test_even_signed_closure_cannot_insert_zero_wrong_sign_or_wrong_quantity(counting, delta):
    from backend.app.stocktake_models import StocktakeEntry
    r = counting; row = count_all(r, start(r), {r["raw_position"]["id"]: "2.5"})
    data = closing_body(r, row); before = snapshot(r["path"])
    def alter(mapper, connection, target): target.qty_base_delta = delta
    event.listen(StocktakeEntry, "before_insert", alter)
    try:
        response = close(r, row, data); assert response.status_code == 409, response.text
    finally: event.remove(StocktakeEntry, "before_insert", alter)
    assert snapshot(r["path"]) == before and current(r, row)["freeze_active"]


def test_signed_close_grant_is_unavailable_on_another_physical_connection(counting, monkeypatch):
    from backend.app import stocktakes as api
    r = counting; row = count_all(r, start(r)); actual = api.verified_confirmation; checked = []
    @contextmanager
    def verify(session, parent, token, current_snapshot, fail):
        with actual(session, parent, token, current_snapshot, fail) as grant:
            assert session.connection().exec_driver_sql("SELECT erp_stocktake_authorized()").scalar() == 1
            with r["db"].read() as other:
                assert other.connection().exec_driver_sql("SELECT erp_stocktake_authorized()").scalar() == 0
                checked.append(True)
            yield grant
    monkeypatch.setattr(api, "verified_confirmation", verify)
    response = close(r, row); assert response.status_code == 201, response.text
    assert checked == [True]

