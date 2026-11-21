"""Independent two-warehouse conservation, authority and SQLite bypass checks."""
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
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.app.db import ROOT, DEMO_ACTOR_ID, make_engine
from backend.app.local_demo import create_local_demo_app
from backend.app.main import create_app
from backend.tests.seed_inventory_transfers_browser import seed, PORT, UI_PORT
from backend.tests.test_finished_goods_receipts import snapshot
from backend.tests import test_stocktakes as stk

URL = "/api/inventory-transfers"
NEW = {"inventory_transfer", "inventory_transfer_posting", "inventory_transfer_entry"}
FIELDS = ["document_no", "business_date", "source_position_id", "destination_warehouse_id", "quantity", "reason", "note", "row_version"]


def client_for(app):
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))


def artifact_directory():
    parent = ROOT / "artifacts" / "round25-backend-tests"
    parent.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="transfer-", dir=parent))


@pytest.fixture(scope="session")
def transfer_sources():
    path = artifact_directory() / "fresh-sources.sqlite"
    return path, seed(path)


@pytest.fixture
def transferring(transfer_sources):
    source, metadata = transfer_sources
    path = artifact_directory() / "transfers.sqlite"
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(path)) as target:
        original.backup(target)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        yield dict(metadata, client=client, app=app, db=app.state.db, path=path)


def body(r, number="TRF-QA", **patch):
    return dict(request_key=str(uuid4()), document_no=number, business_date="2026-11-21",
        source_position_id=r["finished_position"]["id"], destination_warehouse_id=r["warehouse2"]["id"],
        quantity="0.25", reason="本機展示跨倉調撥驗收", note="隔離後端驗證") | patch


def create(r, number="TRF-QA", **patch):
    response = r["client"].post(URL, json=body(r, number, **patch))
    assert response.status_code == 201, response.text
    return response.json()


def current(r, row):
    response = r["client"].get(URL + "/" + row["id"])
    assert response.status_code == 200, response.text
    return response.json()


def edit(row, **patch):
    return {key: row[key] for key in FIELDS} | patch


def review(r, row):
    response = r["client"].get(URL + "/" + row["id"] + "/posting-review")
    assert response.status_code == 200, response.text
    return response.json()


def post_body(r, row, **patch):
    checked = review(r, row)
    return dict(request_key=str(uuid4()), row_version=checked["transfer"]["row_version"],
        confirmation_token=checked["confirmation_token"] or "") | patch


def post(r, row, data=None):
    return r["client"].post(URL + "/" + row["id"] + "/post", json=data if data is not None else post_body(r, row))


def positions(r):
    response = r["client"].get("/api/stock-positions")
    assert response.status_code == 200, response.text
    return response.json()


def stock(r, position):
    return next(p for p in positions(r) if p["id"] == position["id"])["on_hand_qty_base"]


def total(r, item_id):
    # Fraction is an independent rational oracle, never the new arithmetic code.
    return sum((Fraction(p["on_hand_qty_base"]) for p in positions(r) if p["item_id"] == item_id), Fraction(0))


def old_rows(r):
    return {table: rows for table, rows in snapshot(r["path"])["rows"].items()
        if table not in NEW | {"stock_create_receipt", "stock_position"}}


def assert_integrity(r):
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(session.connection().exec_driver_sql("PRAGMA foreign_key_check")) == []


def assert_pair(r, row, source, before_source, before_destination):
    saved = current(r, row)
    assert saved["state"] == "POSTED" and saved["posting_state"] == "POSTED" and saved["posting_id"]
    legs = sorted(saved["movements"], key=lambda leg: leg["leg_no"])
    assert len(legs) == 2 and [leg["leg_no"] for leg in legs] == [1, 2]
    assert legs[0]["position_id"] == source["id"]
    assert legs[1]["position_id"] == saved["destination_position_id"] != source["id"]
    assert len({leg["id"] for leg in legs}) == 2
    assert {leg["posting_id"] for leg in legs} == {saved["posting_id"]}
    assert {leg["transfer_id"] for leg in legs} == {saved["id"]}
    quantity = Fraction(saved["quantity"])
    assert [Fraction(leg["qty_base_delta"]) for leg in legs] == [-quantity, quantity]
    assert sum(Fraction(leg["qty_base_delta"]) for leg in legs) == 0
    assert Fraction(saved["source_before_qty_base"]) == Fraction(before_source)
    assert Fraction(saved["destination_before_qty_base"]) == Fraction(before_destination)
    assert Fraction(saved["source_after_qty_base"]) == Fraction(before_source) - quantity
    assert Fraction(saved["destination_after_qty_base"]) == Fraction(before_destination) + quantity
    destination = next(p for p in positions(r) if p["id"] == saved["destination_position_id"])
    assert (destination["item_id"], destination["lot_id"], destination["warehouse_id"]) == (
        source["item_id"], source["lot_id"], saved["destination_warehouse_id"])
    with r["db"].read() as session:
        ledger = list(session.connection().exec_driver_sql(
            "SELECT entry_id,posting_id,document_line_id,position_id,qty_base_delta FROM physical_stock_ledger WHERE document_line_id=? AND domain='INVENTORY_TRANSFER'",
            (saved["id"],)))
        assert {tuple(x) for x in ledger} == {(leg["id"], saved["posting_id"], saved["id"], leg["position_id"], leg["qty_base_delta"]) for leg in legs}
    return saved


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


def test_draft_crud_search_retry_stale_versions_and_no_destination_creation(transferring):
    r = transferring; c = r["client"]; original = old_rows(r); initial_positions = positions(r); data = body(r)
    response = c.post(URL, json=data); assert response.status_code == 201, response.text
    row = response.json()
    assert row["state"] == "DRAFT" and row["movements"] == [] and row["posting_id"] is None
    assert row["destination_position_id"] is None
    before_review = snapshot(r["path"]); checked = review(r, row)
    assert checked["posting_allowed"] and checked["confirmation_token"]
    assert checked["destination"]["will_create"] and checked["destination"]["position_id"] is None
    assert (checked["source_before_qty_base"], checked["destination_before_qty_base"], checked["source_delta_qty_base"],
        checked["destination_delta_qty_base"], checked["source_after_qty_base"], checked["destination_after_qty_base"],
        checked["net_qty_base"]) == ("0.75", "0", "-0.25", "0.25", "0.5", "0.25", "0")
    assert snapshot(r["path"]) == before_review
    assert c.post(URL, json=data).status_code == 200
    assert c.post(URL, json=data | dict(reason="different")).status_code == 409
    assert c.post(URL, json=body(r)).status_code == 409
    changed = c.put(URL + "/" + row["id"], json=edit(row, note="修訂調撥草稿"))
    assert changed.status_code == 200, changed.text
    assert changed.json()["row_version"] == 2
    assert c.get(URL, params=dict(q="修訂調撥草稿")).json()[0]["id"] == row["id"]
    assert c.get(URL, params=dict(q="%_'" )).json() == []
    assert c.put(URL + "/" + row["id"], json=edit(row)).status_code == 409
    assert c.delete(URL + "/" + row["id"], params=dict(row_version=1)).status_code == 409
    assert c.delete(URL + "/" + row["id"], params=dict(row_version=2)).status_code == 204
    assert c.post(URL, json=data).status_code == 410
    assert old_rows(r) == original and positions(r) == initial_positions


@pytest.mark.parametrize("patch", [dict(document_no=" "), dict(source_position_id=str(uuid4())),
    dict(destination_warehouse_id=str(uuid4())), dict(business_date="2026-02-30"), dict(state="POSTED"),
    dict(item_id=str(uuid4())), dict(source_warehouse_id=str(uuid4())), dict(lot_id=str(uuid4())),
    dict(unit_id=str(uuid4())), dict(item_kind="RAW"), dict(destination_position_id=str(uuid4())),
    dict(lot_code="FABRICATED-LOT"), dict(quantity="0"), dict(quantity="-0.25"), dict(reason=" \t\u3000")])
def test_invalid_or_client_forged_identity_rejects_without_partial_draft(transferring, patch):
    r = transferring; before = snapshot(r["path"])
    assert r["client"].post(URL, json=body(r, **patch)).status_code == 422
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("quantity", [0.25, "1e-3", "NaN", "Infinity", "+0.25", "1,000"])
def test_only_exact_plain_decimal_text_is_accepted(transferring, quantity):
    r = transferring; before = snapshot(r["path"])
    assert r["client"].post(URL, json=body(r, quantity=quantity)).status_code == 422
    assert snapshot(r["path"]) == before


def test_same_warehouse_is_rejected_and_source_selectors_are_genuine_readonly(transferring):
    r = transferring; before = snapshot(r["path"]); c = r["client"]
    assert c.post(URL, json=body(r, destination_warehouse_id=r["warehouse"]["id"])).status_code in {409, 422}
    sources = c.get(URL + "/sources").json()
    assert {s["position_id"] for s in sources} == {p["id"] for p in r["positions"]}
    assert {s["item_kind"] for s in sources} == {"RAW", "FINISHED"}
    assert len({s["unit_id"] for s in sources}) == 2
    assert all(s["warehouse_id"] == r["warehouse2"]["id"] for s in c.get(URL + "/sources", params=dict(warehouse_id=r["warehouse2"]["id"])).json())
    assert all(s["item_id"] == r["raw_position"]["item_id"] for s in c.get(URL + "/sources", params=dict(item_id=r["raw_position"]["item_id"])).json())
    assert c.get(URL + "/sources", params=dict(q="%'")).json() == []
    assert snapshot(r["path"]) == before


def test_real_fg_new_destination_and_raw_reused_destination_conserve_stock_and_history(transferring):
    r = transferring; original = old_rows(r); initial = positions(r)
    fg = r["finished_position"]; raw = r["raw_position"]
    assert total(r, fg["item_id"]) == Fraction("0.75")
    assert total(r, raw["item_id"]) == Fraction("2.75")
    a = create(r, "FG-NEW-DEST"); checked = review(r, a)
    assert checked["destination"]["will_create"] and len(positions(r)) == len(initial)
    result = post(r, a); assert result.status_code == 201, result.text
    posted_fg = assert_pair(r, result.json(), fg, "0.75", "0")
    assert stock(r, fg) == "0.5" and stock(r, dict(id=posted_fg["destination_position_id"])) == "0.25"
    assert len(positions(r)) == len(initial) + 1
    b = create(r, "RAW-REUSE-DEST", source_position_id=raw["id"], quantity="0.5")
    checked = review(r, b)
    assert not checked["destination"]["will_create"] and checked["destination"]["position_id"] == r["other_position"]["id"]
    result = post(r, b); assert result.status_code == 201, result.text
    posted_raw = assert_pair(r, result.json(), raw, "2.75", "0")
    assert posted_raw["destination_position_id"] == r["other_position"]["id"]
    assert (stock(r, raw), stock(r, r["other_position"])) == ("2.25", "0.5")
    assert total(r, fg["item_id"]) == Fraction("0.75") and total(r, raw["item_id"]) == Fraction("2.75")
    assert old_rows(r) == original and len(positions(r)) == len(initial) + 1
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT count(*) FROM inventory_transfer_entry").scalar() == 4
        assert set(session.connection().exec_driver_sql("SELECT typeof(qty_base_delta) FROM inventory_transfer_entry").scalars()) == {"text"}
    from backend.tests.test_mrp import preview
    material = next(m for m in preview(r)["materials"] if m["material"]["id"] == raw["item_id"])
    assert (material["gross_demand_qty_base"], material["physical_stock_qty_base"], material["shortage_qty_base"]) == ("11.5625", "2.25", "9.3125")
    # The preview helper explicitly selects WH1; physical company total is2.75.
    order = next(o for o in r["client"].get("/api/outstanding-sales").json()["orders"] if o["id"] == r["order"]["id"])
    assert order["total_amount"] == "68.125" and order["document_no"].endswith(" ")
    assert order["quantity_totals"][0]["outstanding_qty_base"] == "3"
    assert r["client"].get("/api/purchase-outstanding").json()["currency_totals"][0]["total_amount"] == "192.25"
    assert_integrity(r)


def test_insufficient_source_and_zero_balance_fail_then_exact_exhaustion_is_valid(transferring):
    r = transferring; row = create(r, quantity="0.750000000000000000000000000000000000001")
    before = snapshot(r["path"]); checked = review(r, row)
    assert not checked["posting_allowed"] and checked["confirmation_token"] is None
    assert post(r, row).status_code in {409, 422} and snapshot(r["path"]) == before
    zero = create(r, "ZERO-SOURCE", source_position_id=r["zero_position"]["id"])
    before = snapshot(r["path"])
    assert not review(r, zero)["posting_allowed"] and post(r, zero).status_code in {409, 422}
    assert snapshot(r["path"]) == before
    exact = create(r, "EXACT-EXHAUSTION", quantity="0.75")
    response = post(r, exact); assert response.status_code == 201, response.text
    assert_pair(r, response.json(), r["finished_position"], "0.75", "0")
    assert stock(r, r["finished_position"]) == "0" and total(r, r["finished_position"]["item_id"]) == Fraction("0.75")


def test_durable_cancel_and_delete_never_create_destination_or_use_final_authority(transferring):
    r = transferring; row = create(r); before = old_rows(r); original_positions = positions(r)
    data = dict(request_key=str(uuid4()), row_version=row["row_version"])
    response = r["client"].post(URL + "/" + row["id"] + "/cancel", json=data)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["state"] == "CANCELLED" and saved["cancelled_at"] and saved["cancelled_by"]
    assert saved["reason"] == row["reason"] and saved["destination_position_id"] is None and saved["movements"] == []
    assert r["client"].post(URL + "/" + row["id"] + "/cancel", json=data).json() == saved
    assert r["client"].put(URL + "/" + row["id"], json=edit(saved)).status_code == 409
    assert r["client"].delete(URL + "/" + row["id"], params=dict(row_version=saved["row_version"])).status_code == 409
    assert post(r, saved, dict(request_key=str(uuid4()), row_version=saved["row_version"], confirmation_token="")).status_code in {409, 422}
    assert old_rows(r) == before and positions(r) == original_positions
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT erp_transfer_authorized()").scalar() == 0


@pytest.mark.parametrize("side", ["source", "destination", "both"])
@pytest.mark.parametrize("phase", ["ACTIVE_FROZEN", "REVIEW_READY"])
def test_either_or_both_frozen_warehouses_block_preview_and_post(transferring, side, phase):
    r = transferring; row = create(r); stale = post_body(r, row)
    for index, warehouse in enumerate(([r["warehouse"]] if side == "source" else [r["warehouse2"]] if side == "destination" else [r["warehouse"], r["warehouse2"]])):
        take = stk.start(r, stk.create(r, "FROZEN-" + str(index), warehouse_id=warehouse["id"]))
        if phase == "REVIEW_READY": take = stk.count_all(r, take)
        assert take["state"] == phase
    before = snapshot(r["path"]); checked = review(r, row)
    assert not checked["posting_allowed"] and checked["confirmation_token"] is None
    assert checked["source"]["frozen"] == (side in {"source", "both"})
    assert checked["destination"]["frozen"] == (side in {"destination", "both"})
    assert post(r, row, stale).status_code == 409
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("release", ["cancel", "close"])
def test_entire_freeze_cycle_on_either_side_invalidates_old_signed_review(transferring, side, release):
    r = transferring; row = create(r); stale = post_body(r, row)
    warehouse = r["warehouse"] if side == "source" else r["warehouse2"]
    take = stk.start(r, stk.create(r, "FREEZE-CYCLE", warehouse_id=warehouse["id"]))
    if release == "cancel": assert stk.cancel(r, take).status_code == 200
    else:
        take = stk.count_all(r, take); assert stk.close(r, take).status_code == 201
    before = snapshot(r["path"])
    assert not stk.current(r, take)["freeze_active"]
    assert post(r, row, stale).status_code == 409 and snapshot(r["path"]) == before
    assert post(r, row).status_code == 201


@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("phase", ["ACTIVE_FROZEN", "REVIEW_READY"])
def test_common_flush_barrier_checks_both_warehouses_before_entry_or_posting_insert(transferring, side, phase):
    from backend.app.transfer_models import InventoryTransferEntry, InventoryTransferPosting
    from backend.app.models import now_utc
    r = transferring; row = create(r)
    warehouse = r["warehouse"] if side == "source" else r["warehouse2"]
    take = stk.start(r, stk.create(r, "COMMON-FROZEN", warehouse_id=warehouse["id"]))
    if phase == "REVIEW_READY": take = stk.count_all(r, take)
    before = snapshot(r["path"]); inserts = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith(("insert into inventory_transfer_entry", "insert into inventory_transfer_posting")):
            inserts.append(statement)
    event.listen(r["db"].engine, "before_cursor_execute", observe)
    try:
        for model in ["entry", "posting"]:
            with pytest.raises(IntegrityError, match="凍結|frozen"), r["db"].write() as session:
                if model == "entry":
                    target = InventoryTransferEntry(posting_id=str(uuid4()), transfer_id=row["id"],
                        position_id=r["finished_position"]["id"], leg_no=1, qty_base_delta="-0.25", created_by=DEMO_ACTOR_ID)
                else:
                    target = InventoryTransferPosting(transfer_id=row["id"], idempotency_key=str(uuid4()),
                        request_fingerprint="a" * 64, source_version=row["row_version"], posting_state="PREPARING",
                        authorization_basis="LOCAL_DEMO_MANUAL", confirmation_digest="b" * 64,
                        confirmation_snapshot="{}", confirmation_received_at=now_utc(), posted_at=now_utc(),
                        posted_by=DEMO_ACTOR_ID, created_by=DEMO_ACTOR_ID)
                session.add(target); session.flush()
    finally: event.remove(r["db"].engine, "before_cursor_execute", observe)
    assert inserts == [] and snapshot(r["path"]) == before


@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("phase", ["ACTIVE_FROZEN", "REVIEW_READY"])
def test_sql_freeze_guards_check_both_sides_for_committed_entry_and_parent_actions(transferring, side, phase):
    r = transferring; row = create(r); assert post(r, row).status_code == 201
    warehouse = r["warehouse"] if side == "source" else r["warehouse2"]
    take = stk.start(r, stk.create(r, "SQL-FROZEN", warehouse_id=warehouse["id"]))
    if phase == "REVIEW_READY": take = stk.count_all(r, take)
    before = snapshot(r["path"])
    with raw_driver(r) as con:
        for table in ["inventory_transfer_entry", "inventory_transfer_posting"]:
            for sql in [f"UPDATE {table} SET created_by=created_by", f"DELETE FROM {table}",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} LIMIT 1"]:
                with pytest.raises(sqlite3.DatabaseError, match="(?i)frozen|凍結"):
                    con.execute(sql)
                con.rollback()
    assert snapshot(r["path"]) == before


def test_old_balances_and_review_stale_after_another_transfer_but_posted_snapshot_is_historical(transferring):
    r = transferring; a = create(r, "FIRST"); b = create(r, "SECOND", quantity="0.125")
    stale = post_body(r, b)
    assert post(r, a).status_code == 201
    before = snapshot(r["path"])
    assert post(r, b, stale).status_code == 409 and snapshot(r["path"]) == before
    response = post(r, b); assert response.status_code == 201, response.text
    assert_pair(r, response.json(), r["finished_position"], "0.5", "0.25")
    first = current(r, a)
    assert (first["source_before_qty_base"], first["destination_before_qty_base"], first["source_after_qty_base"], first["destination_after_qty_base"]) == ("0.75", "0", "0.5", "0.25")
    assert (first["source"]["current_stock_qty_base"], first["destination"]["current_stock_qty_base"]) == ("0.375", "0.375")


@pytest.mark.parametrize("side", ["source", "destination"])
def test_independent_source_or_destination_stock_change_invalidates_review(transferring, side):
    from backend.tests import test_inventory_adjustments as adj
    r = transferring; row = create(r, source_position_id=r["raw_position"]["id"], quantity="0.5")
    stale = post_body(r, row)
    position = r["raw_position"] if side == "source" else r["other_position"]
    adjustment = adj.create(r, "STOCK-CHANGED", position_id=position["id"], direction="INCREASE", quantity="0.125")
    assert adj.post(r, adjustment).status_code == 201
    assert stock(r, r["raw_position"]) == ("2.875" if side == "source" else "2.75")
    assert stock(r, r["other_position"]) == ("0.125" if side == "destination" else "0")
    before = snapshot(r["path"])
    assert post(r, row, stale).status_code == 409 and snapshot(r["path"]) == before
    result = post(r, row); assert result.status_code == 201, result.text
    assert_pair(r, result.json(), r["raw_position"], "2.875" if side == "source" else "2.75", "0.125" if side == "destination" else "0")


@pytest.mark.parametrize("side", ["source", "destination"])
def test_master_version_changes_on_either_side_invalidate_preview(transferring, side):
    r = transferring; row = create(r); data = post_body(r, row)
    warehouse = r["client"].get("/api/warehouses/" + (r["warehouse"]["id"] if side == "source" else r["warehouse2"]["id"])).json()
    response = r["client"].put("/api/warehouses/" + warehouse["id"], json={key: warehouse[key] for key in
        ["code", "name", "address", "mrp_included", "is_active", "row_version"]} | dict(name="已更新隔離倉庫名稱"))
    assert response.status_code == 200, response.text
    before = snapshot(r["path"])
    assert post(r, row, data).status_code == 409 and snapshot(r["path"]) == before
    assert post(r, row).status_code == 201


def test_double_click_lost_response_and_replay_have_one_pair_even_after_runtime_replacement(transferring):
    r = transferring; row = create(r); data = post_body(r, row)
    with ThreadPoolExecutor(2) as pool: responses = list(pool.map(lambda _: post(r, row, data), range(2)))
    assert sorted(x.status_code for x in responses) == [200, 201], [x.text for x in responses]
    saved = current(r, row); before = snapshot(r["path"])
    assert post(r, row, data).json() == saved
    with client_for(create_local_demo_app(r["path"], backend_port=PORT, frontend_port=UI_PORT)) as fresh:
        replay = fresh.post(URL + "/" + row["id"] + "/post", json=data)
        assert replay.status_code == 200 and replay.json() == saved
    assert snapshot(r["path"]) == before
    assert post(r, row, data | dict(request_key=str(uuid4()))).status_code == 409
    assert len(current(r, row)["movements"]) == 2


def test_concurrent_same_source_serializes_without_overdraw_and_creates_one_new_destination(transferring):
    r = transferring; rows = [create(r, "RACE-" + str(i), quantity="0.5") for i in range(2)]
    payloads = [post_body(r, row) for row in rows]
    with ThreadPoolExecutor(2) as pool: responses = list(pool.map(lambda pair: post(r, *pair), zip(rows, payloads)))
    assert sorted(x.status_code for x in responses) == [201, 409], [x.text for x in responses]
    assert stock(r, r["finished_position"]) == "0.25"
    dest = [p for p in positions(r) if p["item_id"] == r["finished_position"]["item_id"] and p["warehouse_id"] == r["warehouse2"]["id"]]
    assert len(dest) == 1 and dest[0]["on_hand_qty_base"] == "0.5"
    assert total(r, r["finished_position"]["item_id"]) == Fraction("0.75")
    loser = next(row for row in rows if current(r, row)["state"] == "DRAFT")
    assert not review(r, loser)["posting_allowed"] and post(r, loser).status_code in {409, 422}
    assert_integrity(r)


def test_concurrent_destination_accumulation_uses_fresh_review_and_one_canonical_position(transferring):
    r = transferring; rows = [create(r, "ACCUMULATE-" + str(i), quantity="0.125") for i in range(2)]
    payloads = [post_body(r, row) for row in rows]
    with ThreadPoolExecutor(2) as pool: responses = list(pool.map(lambda pair: post(r, *pair), zip(rows, payloads)))
    assert sorted(x.status_code for x in responses) == [201, 409], [x.text for x in responses]
    loser = next(row for row in rows if current(r, row)["state"] == "DRAFT")
    assert post(r, loser).status_code == 201
    dest = [p for p in positions(r) if p["item_id"] == r["finished_position"]["item_id"] and p["warehouse_id"] == r["warehouse2"]["id"]]
    assert len(dest) == 1 and dest[0]["on_hand_qty_base"] == "0.25"
    assert stock(r, r["finished_position"]) == "0.5" and total(r, r["finished_position"]["item_id"]) == Fraction("0.75")
    assert all(len(current(r, row)["movements"]) == 2 for row in rows)


@pytest.mark.parametrize("stage", ["new_position", "source_debit", "destination_credit", "posting_finish", "header_finish"])
def test_fault_at_each_atomic_stage_rolls_back_pair_new_position_audit_and_authority(transferring, stage):
    r = transferring; row = create(r); data = post_body(r, row); before = snapshot(r["path"]); entries = []; reached = []
    def inject(connection, cursor, statement, parameters, context, executemany):
        lower = statement.lower()
        if lower.startswith("insert into inventory_transfer_entry"): entries.append(True)
        match = ((stage == "new_position" and lower.startswith("insert into stock_position"))
            or (stage == "source_debit" and lower.startswith("insert into inventory_transfer_entry") and len(entries) == 1)
            or (stage == "destination_credit" and lower.startswith("insert into inventory_transfer_entry") and len(entries) == 2)
            or (stage == "posting_finish" and lower.startswith("update inventory_transfer_posting set"))
            or (stage == "header_finish" and lower.startswith("update inventory_transfer set") and "posted_at" in lower))
        if match:
            reached.append(True)
            raise IntegrityError(statement, parameters, RuntimeError("injected transfer " + stage))
    hook = "before_cursor_execute" if stage in {"posting_finish", "header_finish"} else "after_cursor_execute"
    event.listen(r["db"].engine, hook, inject)
    try:
        response = post(r, row, data); assert response.status_code == 409, response.text
    finally: event.remove(r["db"].engine, hook, inject)
    assert reached and snapshot(r["path"]) == before
    assert current(r, row)["state"] == "DRAFT" and stock(r, r["finished_position"]) == "0.75"
    with r["db"].read() as session:
        assert tuple(session.connection().exec_driver_sql("SELECT erp_transfer_authorized(),erp_transfer_document(),erp_transfer_digest(),erp_transfer_snapshot(),erp_transfer_time()").one()) == (0, None, None, None, None)
    assert post(r, row, data).status_code == 201


def test_stale_version_or_other_document_confirmation_cannot_post(transferring):
    r = transferring; a = create(r, "FIRST"); b = create(r, "SECOND"); data = post_body(r, a)
    changed = r["client"].put(URL + "/" + a["id"], json=edit(a, note="review became stale"))
    assert changed.status_code == 200
    before = snapshot(r["path"])
    assert post(r, changed.json(), data).status_code == 409
    assert post(r, b, data | dict(request_key=str(uuid4()))).status_code in {403, 409}
    assert snapshot(r["path"]) == before


def test_ordinary_server_wrong_origin_forgery_expiry_future_or_other_runtime_cannot_authorize(transferring, monkeypatch):
    from backend.app import transfer_authorization as auth
    r = transferring; row = create(r); data = post_body(r, row); before = snapshot(r["path"])
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


def test_signed_grant_is_private_to_the_exact_physical_connection_and_clears(transferring, monkeypatch):
    from backend.app import inventory_transfers as api
    r = transferring; row = create(r); actual = api.verified_confirmation; captured = []
    @contextmanager
    def verify(session, parent, token, current_snapshot, fail):
        with actual(session, parent, token, current_snapshot, fail) as grant:
            assert session.connection().exec_driver_sql("SELECT erp_transfer_authorized()").scalar() == 1
            with r["db"].read() as other:
                assert other.connection().exec_driver_sql("SELECT erp_transfer_authorized()").scalar() == 0
                captured.append(True)
            yield grant
    monkeypatch.setattr(api, "verified_confirmation", verify)
    assert post(r, row).status_code == 201 and captured == [True]
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("SELECT erp_transfer_authorized()").scalar() == 0


@pytest.mark.parametrize("leg,field,value", [(1, "qty_base_delta", "0"), (1, "qty_base_delta", "0.25"),
    (1, "qty_base_delta", "-0.5"), (2, "qty_base_delta", "-0.25"), (2, "qty_base_delta", "0.5"),
    (1, "leg_no", 2), (2, "leg_no", 1), (1, "position_id", "RAW"), (2, "position_id", "SOURCE")])
def test_sql_guards_reject_wrong_sign_magnitude_leg_and_other_item_even_inside_valid_grant(transferring, leg, field, value):
    from backend.app.transfer_models import InventoryTransferEntry
    r = transferring; row = create(r); data = post_body(r, row); before = snapshot(r["path"])
    def alter(mapper, connection, target):
        if target.leg_no == leg:
            actual = r["raw_position"]["id"] if value == "RAW" else r["finished_position"]["id"] if value == "SOURCE" else value
            setattr(target, field, actual)
    event.listen(InventoryTransferEntry, "before_insert", alter)
    try:
        response = post(r, row, data); assert response.status_code == 409, response.text
    finally: event.remove(InventoryTransferEntry, "before_insert", alter)
    assert snapshot(r["path"]) == before


def test_database_refuses_posted_finalization_after_only_one_leg_even_with_valid_grant(transferring):
    r = transferring; row = create(r); captured = []
    def probe(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith("insert into inventory_transfer_entry") and not captured:
            count = connection.exec_driver_sql("SELECT count(*) FROM inventory_transfer_entry").scalar()
            assert count == 1
            with pytest.raises(DBAPIError):
                connection.exec_driver_sql("UPDATE inventory_transfer_posting SET posting_state='POSTED'")
            captured.append(True)
    event.listen(r["db"].engine, "after_cursor_execute", probe)
    try:
        response = post(r, row); assert response.status_code == 201, response.text
    finally: event.remove(r["db"].engine, "after_cursor_execute", probe)
    assert captured == [True]
    assert_pair(r, response.json(), r["finished_position"], "0.75", "0")


@pytest.mark.parametrize("phase", ["POSTED", "CANCELLED"])
def test_terminal_api_sql_update_delete_replace_and_rowid_are_immutable(transferring, phase):
    r = transferring; row = create(r)
    result = post(r, row) if phase == "POSTED" else r["client"].post(URL + "/" + row["id"] + "/cancel", json=dict(request_key=str(uuid4()), row_version=row["row_version"]))
    assert result.status_code in {200, 201}, result.text
    saved = result.json(); before = snapshot(r["path"])
    assert r["client"].put(URL + "/" + row["id"], json=edit(saved, reason="rewrite")).status_code == 409
    assert r["client"].delete(URL + "/" + row["id"], params=dict(row_version=saved["row_version"])).status_code == 409
    tables = ["inventory_transfer"] + (["inventory_transfer_posting", "inventory_transfer_entry"] if phase == "POSTED" else [])
    with raw_driver(r) as con:
        for table in tables:
            for sql in [f"UPDATE {table} SET created_by=created_by", f"UPDATE {table} SET rowid=rowid+10000", f"DELETE FROM {table}", f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} LIMIT 1"]:
                with pytest.raises(sqlite3.DatabaseError): con.execute(sql)
                con.rollback()
    assert snapshot(r["path"]) == before


def test_default_sql_cannot_forge_header_posting_entry_or_destination_link(transferring):
    r = transferring; posted = create(r, "POSTED"); assert post(r, posted).status_code == 201
    row = create(r, "FORGED"); before = snapshot(r["path"])
    with raw_driver(r) as con:
        assert con.execute("SELECT erp_transfer_authorized()").fetchone()[0] == 0
        statements = [
            ("UPDATE inventory_transfer SET state='POSTED',row_version=row_version+1,posted_at=created_at,posted_by=created_by WHERE id=?", (row["id"],)),
            ("UPDATE inventory_transfer SET destination_position_id=? WHERE id=?", (r["other_position"]["id"], row["id"])),
            ("INSERT OR REPLACE INTO inventory_transfer_posting SELECT * FROM inventory_transfer_posting LIMIT 1", ()),
            ("INSERT OR REPLACE INTO inventory_transfer_entry SELECT * FROM inventory_transfer_entry LIMIT 1", ())]
        for sql, parameters in statements:
            with pytest.raises(sqlite3.DatabaseError): con.execute(sql, parameters)
            con.rollback()
    assert snapshot(r["path"]) == before


def test_source_destination_lot_item_unit_and_provenance_cannot_be_redirected_or_replaced(transferring):
    r = transferring; row = create(r); response = post(r, row); assert response.status_code == 201
    saved = response.json(); before = snapshot(r["path"]); source = r["finished_position"]
    attempts = [
        ("UPDATE stock_position SET warehouse_id=? WHERE id=?", (r["warehouse"]["id"], saved["destination_position_id"])),
        ("UPDATE stock_position SET lot_id=? WHERE id=?", (r["raw_position"]["lot_id"], saved["destination_position_id"])),
        ("UPDATE stock_position SET rowid=rowid+10000 WHERE id=?", (saved["destination_position_id"],)),
        ("INSERT OR REPLACE INTO stock_position SELECT * FROM stock_position WHERE id=?", (saved["destination_position_id"],)),
        ("INSERT OR REPLACE INTO stock_position SELECT * FROM stock_position WHERE id=?", (source["id"],)),
        ("UPDATE inventory_lot SET lot_code='FORGED-LOT' WHERE id=?", (source["lot_id"],)),
        ("UPDATE inventory_lot SET origin_kind='PURCHASED',supplier_id=? WHERE id=?", (r["suppliers"][0]["id"], source["lot_id"])),
        ("INSERT OR REPLACE INTO inventory_lot SELECT * FROM inventory_lot WHERE id=?", (source["lot_id"],)),
        ("UPDATE item SET base_unit_id=? WHERE id=?", (r["raws"][0]["base_unit_id"], source["item_id"])),
        ("UPDATE item SET item_kind='RAW' WHERE id=?", (source["item_id"],)),
        ("UPDATE inventory_transfer SET destination_warehouse_id=? WHERE id=?", (r["warehouse"]["id"], saved["id"]))]
    with raw_driver(r) as con:
        for sql, parameters in attempts:
            with pytest.raises(sqlite3.DatabaseError): con.execute(sql, parameters)
            con.rollback()
    assert snapshot(r["path"]) == before


def test_same_lot_text_never_merges_another_item_or_allows_conflicting_batch_metadata(transferring):
    from backend.app.receiving_models import InventoryLot, StockPosition
    r = transferring; source = r["finished_position"]
    with r["db"].write() as session:
        wrong = InventoryLot(item_id=r["raws"][0]["id"], lot_code=source["lot_code"], origin_kind="PURCHASED",
            supplier_id=r["suppliers"][0]["id"], supplier_lot_code="DIFFERENT-BATCH", created_by=DEMO_ACTOR_ID)
        session.add(wrong); session.flush()
        session.add(StockPosition(item_id=wrong.item_id, warehouse_id=r["warehouse2"]["id"], lot_id=wrong.id, created_by=DEMO_ACTOR_ID))
    row = create(r); response = post(r, row); assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["lot_id"] == source["lot_id"] and saved["destination"]["lot_id"] == source["lot_id"] != wrong.id
    before = snapshot(r["path"])
    with raw_driver(r) as con:
        with pytest.raises(sqlite3.DatabaseError):
            con.execute("INSERT INTO inventory_lot(id,created_at,created_by,item_id,lot_code,origin_kind,supplier_id,supplier_lot_code) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid4()), "2026-11-21T00:00:00.000000Z", DEMO_ACTOR_ID, source["item_id"], source["lot_code"], "PURCHASED", r["suppliers"][0]["id"], "CONFLICT"))
        con.rollback()
    assert snapshot(r["path"]) == before


def test_cross_domain_posting_keys_reject_in_both_directions(transferring):
    r = transferring; row = create(r)
    with r["db"].read() as session:
        keys = [session.connection().exec_driver_sql("SELECT idempotency_key FROM " + table + " LIMIT 1").scalar_one()
            for table in ["stock_posting", "supplier_return_posting", "material_issue_posting", "material_return_posting",
                "finished_goods_posting", "sales_shipment_posting", "customer_return_finalization", "stocktake_posting", "inventory_adjustment_posting"]]
    for key in keys:
        before = snapshot(r["path"])
        assert post(r, row, post_body(r, row, request_key=key)).status_code == 409
        assert snapshot(r["path"]) == before
    data = post_body(r, row); assert post(r, row, data).status_code == 201
    endpoint, ordinary = stk.prepare_writer(r, "purchase")
    before = snapshot(r["path"])
    response = r["client"].post(endpoint, json=ordinary | dict(request_key=data["request_key"]))
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before


@pytest.mark.parametrize("kind", ["supplier", "issue", "material_return", "finished", "shipment", "customer_return"])
def test_transfer_key_cannot_be_reused_by_other_existing_inventory_domains(transferring, kind):
    r = transferring; row = create(r); data = post_body(r, row)
    assert post(r, row, data).status_code == 201
    endpoint, ordinary = stk.prepare_writer(r, kind)
    before = snapshot(r["path"])
    response = r["client"].post(endpoint, json=ordinary | dict(request_key=data["request_key"]))
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before


def test_transfer_posting_key_cannot_be_reused_by_inventory_adjustment(transferring):
    from backend.tests import test_inventory_adjustments as adj
    r = transferring; row = create(r); data = post_body(r, row)
    assert post(r, row, data).status_code == 201
    adjustment = adj.create(r, "KEY-REUSE", quantity="0.125")
    payload = adj.post_body(r, adjustment, request_key=data["request_key"])
    before = snapshot(r["path"])
    response = adj.post(r, adjustment, payload)
    assert response.status_code == 409, response.text
    assert snapshot(r["path"]) == before


def test_tiny_transfer_and_huge_real_stock_have_no_rounding_or_arbitrary_precision_cap(transferring):
    r = transferring; tiny = "0.000000000000000000000000000000000000001"
    row = create(r, "TINY", quantity=tiny)
    result = post(r, row); assert result.status_code == 201, result.text
    assert_pair(r, result.json(), r["finished_position"], "0.75", "0")
    assert Fraction(stock(r, r["finished_position"])) == Fraction("0.75") - Fraction(tiny)
    assert total(r, r["finished_position"]["item_id"]) == Fraction("0.75")
    huge = "9" * 5000 + ".123456789012345678901234567890123456789"
    from backend.tests import test_inventory_adjustments as adj
    addition = adj.create(r, "HUGE-SYNTHETIC-STOCK", position_id=r["zero_position"]["id"], direction="INCREASE", quantity=huge)
    assert adj.post(r, addition).status_code == 201
    row = create(r, "HUGE", source_position_id=r["zero_position"]["id"], quantity=huge)
    result = post(r, row); assert result.status_code == 201, result.text
    saved = result.json()
    assert [leg["qty_base_delta"] for leg in sorted(saved["movements"], key=lambda x: x["leg_no"])] == ["-" + huge, huge]
    assert stock(r, r["zero_position"]) == "0" and stock(r, dict(id=saved["destination_position_id"])) == huge
    assert_integrity(r)


def test_genuine_nonbatch_finished_goods_transfer_creates_then_reuses_null_lot_destination():
    from backend.tests import test_finished_goods_receipts as fg
    from backend.app.receiving_models import StockPosition
    path = artifact_directory() / "nonbatch-finished.sqlite"
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        r = dict(client=client, app=app, db=app.state.db, path=path, **fg.seed_small(client))
        # The existing approved workflow creates genuine output for an actual
        # non-batch item; no position or production provenance is fabricated.
        completion = fg.add_completion(r, "COMP-NONBATCH-TRANSFER", order=1)
        r["completions"] = [None, completion]
        receipt = fg.create(r, "FGRCV-NONBATCH-TRANSFER", source_index=1)
        response = fg.post(r, receipt)
        assert response.status_code == 201, response.text
        source = next(p for p in positions(r) if p["item_id"] == r["products"][1]["id"])
        r["finished_position"] = source
        assert source["lot_id"] is None and source["lot_code"] is None
        assert source["on_hand_qty_base"] == "1.25"
        before = snapshot(path)
        with pytest.raises(IntegrityError), r["db"].write() as session:
            session.add(StockPosition(item_id=source["item_id"], warehouse_id=r["warehouse2"]["id"],
                lot_id=None, created_by=DEMO_ACTOR_ID))
            session.flush()
        assert snapshot(path) == before
        row = create(r, "NONBATCH-NEW"); checked = review(r, row)
        assert row["lot_id"] is None and checked["destination"]["lot_id"] is None
        assert checked["destination"]["will_create"] and snapshot(path)["rows"]["stock_position"] == before["rows"]["stock_position"]
        response = post(r, row); assert response.status_code == 201, response.text
        saved = assert_pair(r, response.json(), source, "1.25", "0")
        target_id = saved["destination_position_id"]
        assert stock(r, source) == "1" and stock(r, dict(id=target_id)) == "0.25"
        row = create(r, "NONBATCH-REUSE", quantity="0.125")
        checked = review(r, row)
        assert not checked["destination"]["will_create"] and checked["destination"]["position_id"] == target_id
        response = post(r, row); assert response.status_code == 201, response.text
        saved = assert_pair(r, response.json(), source, "1", "0.25")
        assert saved["destination_position_id"] == target_id
        assert (stock(r, source), stock(r, dict(id=target_id))) == ("0.875", "0.375")
        assert total(r, source["item_id"]) == Fraction("1.25")
        assert snapshot(path)["rows"]["inventory_lot"] == before["rows"]["inventory_lot"]
        assert_integrity(r)
