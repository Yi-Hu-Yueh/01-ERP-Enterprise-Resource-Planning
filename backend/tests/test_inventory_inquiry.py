"""Four inquiry contracts on one synthetic seed, cloned per test; no Owner DB."""
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import event
from backend.app.models import Item

from backend.app.inventory_inquiry import inventory_inquiry
from backend.app.local_demo import create_local_demo_app
from backend.app.mrp import read_snapshot
from backend.tests.seed_inventory_transfers_browser import seed, PORT, UI_PORT
from backend.tests.test_finished_goods_receipts import snapshot
from backend.tests import test_inventory_transfers as transfer
from backend.tests import test_inventory_adjustments as adjustment

URL = "/api/inventory-inquiry"


@pytest.fixture(scope="session")
def inquiry_source(tmp_path_factory):
    path = tmp_path_factory.mktemp("inquiry-source") / "synthetic.sqlite"
    metadata = seed(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with transfer.client_for(app) as client:
        record = dict(metadata, client=client)
        draft = transfer.create(record, "TRF-INQUIRY-BASELINE")
        posted = transfer.post(record, draft)
        assert posted.status_code == 201, posted.text
        assert posted.json()["state"] == "POSTED"
    return path, metadata


@pytest.fixture
def inquiry(inquiry_source, tmp_path):
    source, metadata = inquiry_source
    path = tmp_path / "inquiry.sqlite"
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(path)) as target:
        original.backup(target)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with transfer.client_for(app) as client:
        yield dict(deepcopy(metadata), client=client, app=app, db=app.state.db, path=path)


def get(r, **filters):
    response = r["client"].get(URL, params=filters)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def item(result, identity):
    return next(i for i in result["items"] if i["id"] == identity)


def test_exact_item_warehouse_and_genuine_lot_conservation(inquiry):
    r = inquiry
    result = get(r)
    assert result["read_only"] is True
    assert result["quantity_basis"] == "COMMITTED_PHYSICAL_STOCK"
    assert not result["scope"]["location_filtered"]
    raw = item(result, r["raw_position"]["item_id"])
    fg = item(result, r["finished_position"]["item_id"])
    with r["db"].read() as session:
        assert (raw["item_kind"], raw["company_qty_base"], raw["unit_id"]) == (
            "RAW", "2.75", session.get(Item, raw["id"]).base_unit_id)
        assert (fg["item_kind"], fg["company_qty_base"], fg["unit_id"]) == (
            "FINISHED", "0.75", session.get(Item, fg["id"]).base_unit_id)
    assert raw["unit_id"] != fg["unit_id"]
    fg_groups = {w["id"]: w for w in fg["warehouses"]}
    assert fg_groups[r["warehouse"]["id"]]["warehouse_qty_base"] == "0.5"
    assert fg_groups[r["warehouse2"]["id"]]["warehouse_qty_base"] == "0.25"
    assert fg_groups[r["warehouse2"]["id"]]["mrp_included"] is False
    fg_positions = [p for w in fg["warehouses"] for p in w["positions"]]
    assert len(fg_positions) == 2
    assert {p["lot_id"] for p in fg_positions} == {r["finished_position"]["lot_id"]}
    assert {p["lot_code"] for p in fg_positions} == {r["finished_position"]["lot_code"]}
    assert {p["origin_kind"] for p in fg_positions} == {"MANUFACTURED"}
    assert {p["id"] for w in raw["warehouses"] for p in w["positions"]} >= {r["raw_position"]["id"]}
    for entry in result["items"]:
        assert Fraction(entry["company_qty_base"]) == sum(
            (Fraction(w["warehouse_qty_base"]) for w in entry["warehouses"]), Fraction(0))
        assert entry["company_qty_base"] == entry["matched_qty_base"]
        for warehouse in entry["warehouses"]:
            assert Fraction(warehouse["warehouse_qty_base"]) == sum(
                (Fraction(p["qty_base"]) for p in warehouse["positions"]), Fraction(0))
    assert "total_qty_base" not in result  # No quantity across unlike items/units.


def test_filters_keep_true_company_total_and_literal_lot_scope(inquiry):
    r = inquiry
    raw = get(r, item_kind="RAW")
    assert raw["items"] and {i["item_kind"] for i in raw["items"]} == {"RAW"}
    fg = item(get(r, item_kind="FINISHED"), r["finished_position"]["item_id"])
    by_name = get(r, q=fg["name"])
    assert fg["id"] in {i["id"] for i in by_name["items"]}
    filtered = get(r, item_kind="FINISHED", q=fg["code"], warehouse_id=r["warehouse2"]["id"],
                   lot_code=r["finished_position"]["lot_code"])
    assert filtered["scope"]["location_filtered"]
    row = item(filtered, fg["id"])
    assert (row["company_qty_base"], row["matched_qty_base"]) == ("0.75", "0.25")
    assert [w["id"] for w in row["warehouses"]] == [r["warehouse2"]["id"]]
    assert len(row["warehouses"][0]["positions"]) == 1
    assert get(r, q="%_")["items"] == []
    assert get(r, lot_code="%")["items"] == []
    assert get(r, q="no-such-inquiry-item")["items"] == []
    for filters in [dict(item_kind="INVALID"), dict(warehouse_id="invalid"), dict(warehouse_id=str(uuid4()))]:
        assert r["client"].get(URL, params=filters).status_code == 422
    cleared = get(r)
    assert not cleared["scope"]["location_filtered"]
    assert cleared["items"] == get(r)["items"]  # Stable ordering on repeated reads.
    assert item(cleared, fg["id"])["matched_qty_base"] == "0.75"


def test_http_and_sqlite_query_only_leave_all_rows_and_schema_unchanged(inquiry):
    r = inquiry
    before = snapshot(r["path"])
    observed = []

    def inspect_query(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            observed.append(connection.connection.driver_connection.execute("PRAGMA query_only").fetchone()[0])

    event.listen(r["db"].engine, "before_cursor_execute", inspect_query)
    try:
        get(r)
        get(r, warehouse_id=r["warehouse2"]["id"], lot_code="LOT")
    finally:
        event.remove(r["db"].engine, "before_cursor_execute", inspect_query)
    assert observed and set(observed) == {1}
    for method in ["POST", "PUT", "PATCH", "DELETE"]:
        assert r["client"].request(method, URL, json={}).status_code == 405
    assert snapshot(r["path"]) == before
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("PRAGMA query_only").scalar() == 0


def test_refresh_uses_new_commits_not_drafts_or_a_permanent_cache(inquiry):
    r = inquiry
    raw_id = r["raw_position"]["item_id"]
    draft = adjustment.create(r, "ADJ-INQUIRY-REFRESH", quantity="0.125")
    assert item(get(r), raw_id)["company_qty_base"] == "2.75"
    # A real posting commits in a different connection while this reader holds
    # its existing WAL snapshot. Assertions use exact strings/Fraction above.
    with read_snapshot(r["db"]) as session:
        old = inventory_inquiry(session)
        posted = adjustment.post(r, draft)
        assert posted.status_code == 201, posted.text
        assert item(inventory_inquiry(session), raw_id)["company_qty_base"] == "2.75"
    fresh = get(r)
    assert item(old, raw_id)["company_qty_base"] == "2.75"
    assert item(fresh, raw_id)["company_qty_base"] == "2.625"
    assert item(fresh, r["finished_position"]["item_id"])["company_qty_base"] == "0.75"
    assert fresh["as_of"] != old["as_of"]
