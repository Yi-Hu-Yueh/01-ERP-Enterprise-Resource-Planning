"""Five provenance contracts on isolated synthetic databases; never Owner rows."""
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

import pytest
from sqlalchemy import event

from backend.app.db import ROOT
from backend.app.local_demo import create_local_demo_app
from backend.tests.seed_inventory_transfers_browser import seed, PORT, UI_PORT
from backend.tests.test_finished_goods_receipts import snapshot
from backend.tests import test_inventory_transfers as transfer
from backend.tests import test_receiving as receiving
from backend.tests import test_material_issues as issues
from backend.tests import test_finished_goods_receipts as finished
from backend.tests import test_sales_shipments as shipping
from backend.tests import test_customer_returns as customer_returns


MOVEMENTS = "/api/inventory-movements"
TRACE = "/api/lot-traceability"
ARTIFACTS = ROOT / "artifacts" / "round27-2026-11-21" / "backend-tests"


def artifact_directory(prefix):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=ARTIFACTS)).resolve()


@pytest.fixture(scope="session")
def trace_source():
    path = artifact_directory("source-") / "synthetic.sqlite"
    metadata = seed(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with transfer.client_for(app) as client:
        r = dict(metadata, client=client)
        draft = transfer.create(r, "TRF-TRACE-BASELINE")
        response = transfer.post(r, draft)
        assert response.status_code == 201, response.text
        metadata["transfer"] = response.json()
    return path, metadata


@pytest.fixture
def tracing(trace_source):
    source, metadata = trace_source
    path = artifact_directory("case-") / "traceability.sqlite"
    assert source.is_relative_to(ARTIFACTS.resolve()) and not path.exists()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(path)) as target:
            original.backup(target)
    assert not path.samefile(source)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with transfer.client_for(app) as client:
        yield dict(deepcopy(metadata), client=client, app=app, db=app.state.db, path=path)


def get(r, url=MOVEMENTS, **params):
    response = r["client"].get(url, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["read_only"] is True
    return result


def trace(r, **params):
    return get(r, TRACE, **params)


def node(graph, table, source_id):
    matched = [n for n in graph["nodes"] if n["source_table"] == table and n["source_id"] == source_id]
    assert len(matched) == 1, (table, source_id, graph["nodes"])
    return matched[0]


def reachable(graph, start, finish):
    seen, pending = set(), [start["id"]]
    while pending:
        current = pending.pop()
        if current == finish["id"]:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(e["to"] for e in graph["edges"] if e["from"] == current)
    return False


def assert_graph_integrity(graph):
    ids = {n["id"] for n in graph["nodes"]}
    assert len(ids) == len(graph["nodes"])
    assert len({(n["source_table"], n["source_id"]) for n in graph["nodes"]}) == len(ids)
    assert len({e["id"] for e in graph["edges"]}) == len(graph["edges"])
    assert len({(e["from"], e["to"], e["relationship"]) for e in graph["edges"]}) == len(graph["edges"])
    assert all(e["from"] in ids and e["to"] in ids for e in graph["edges"])
    assert all(e["evidence"] in {"DIRECT", "WORK_ORDER", "POOLED", "UNPROVEN"} for e in graph["edges"])
    # Manufacturing traversal excludes side-context and association edges. Real
    # returns and paired transfer positions may form context cycles; they must
    # never restart the manufacturing walk.
    traversed = [edge for edge in graph["edges"] if edge["lineage"]]
    assert all(edge["evidence"] == "DIRECT" for edge in traversed)
    incoming = {identity: 0 for identity in ids}
    for edge in traversed:
        incoming[edge["to"]] += 1
    pending = [identity for identity, count in incoming.items() if count == 0]
    visited = 0
    while pending:
        identity = pending.pop()
        visited += 1
        for edge in traversed:
            if edge["from"] == identity:
                incoming[edge["to"]] -= 1
                if incoming[edge["to"]] == 0:
                    pending.append(edge["to"])
    assert visited == len(ids), "Manufacturing traversal must not contain cycles"


def post_issue(r, draft):
    current = issues.current(r, draft)
    reviewed = r["client"].get("/api/material-issues/" + draft["id"] + "/posting-review")
    assert reviewed.status_code == 200, reviewed.text
    response = issues.post(r, current, dict(request_key=str(uuid4()), row_version=current["row_version"],
        work_order_version=current["work_order_version"], confirmation_token=reviewed.json()["confirmation_token"]))
    assert response.status_code == 201, response.text
    return response.json()


def test_signed_ledger_sources_filters_and_zero_stocktake_do_not_invent_movements(tracing):
    r = tracing
    result = get(r, limit=500)
    rows = result["rows"]
    assert result["quantity_basis"] == "COMMITTED_PHYSICAL_STOCK"
    assert result["total"] == len(rows) == 13
    assert len({row["id"] for row in rows}) == len(rows)
    with r["db"].read() as session:
        persisted = list(session.connection().exec_driver_sql(
            "SELECT domain,entry_id,posting_id,document_line_id,position_id,qty_base_delta FROM physical_stock_ledger"))
    assert {(row["source_kind"], row["id"], row["posting_id"], row["source_line_id"] or row["source_id"],
             row["position_id"], row["qty_base_delta"]) for row in rows} == {tuple(row) for row in persisted}
    raw = [row for row in rows if row["item_id"] == r["raw_position"]["item_id"]]
    fg = [row for row in rows if row["item_id"] == r["finished_position"]["item_id"]]
    assert sum((Fraction(row["qty_base_delta"]) for row in raw), Fraction(0)) == Fraction("2.75")
    assert sum((Fraction(row["qty_base_delta"]) for row in fg), Fraction(0)) == Fraction("0.75")
    assert {row["unit_id"] for row in raw} == {r["raws"][0]["base_unit_id"]}
    assert {row["unit_id"] for row in fg} == {r["products"][0]["base_unit_id"]}
    expected = {"PURCHASE_RECEIPT": ["4", "6"], "SUPPLIER_RETURN": ["-3", "-2", "-1"],
        "MATERIAL_ISSUE": ["-1.75"], "MATERIAL_RETURN": ["0.75"], "INVENTORY_ADJUSTMENT": ["-0.25"],
        "FINISHED_GOODS_RECEIPT": ["1.25"], "SALES_SHIPMENT": ["-0.75"],
        "CUSTOMER_RETURN": ["0.25"], "INVENTORY_TRANSFER": ["-0.25", "0.25"]}
    assert {row["source_kind"] for row in rows} == set(expected)
    for kind, quantities in expected.items():
        filtered = get(r, movement_type=kind)
        assert sorted(row["qty_base_delta"] for row in filtered["rows"]) == sorted(quantities)
        assert all(row["source_kind"] == kind and row["source_id"] and row["document_no"] for row in filtered["rows"])
    assert all(isinstance(row["qty_base_delta"], str) and row["posted_at"] and row["business_date"] for row in rows)
    filtered = get(r, item_kind="FINISHED", q=r["finished_position"]["item_code"],
        warehouse_id=r["warehouse2"]["id"], lot_code=r["finished_position"]["lot_code"])
    assert len(filtered["rows"]) == 1 and filtered["rows"][0]["qty_base_delta"] == "0.25"
    shipment = get(r, document_no=r["shipment"]["document_no"])["rows"]
    assert len(shipment) == 1 and shipment[0]["source_id"] == r["shipment"]["id"]
    day = rows[0]["posted_at"][:10]
    assert get(r, date_from=day, date_to=day, limit=500)["rows"] == rows
    assert get(r, date_to="2000-01-01")["rows"] == []
    page = get(r, offset=2, limit=3)
    assert page["total"] == len(rows) and page["rows"] == rows[2:5]
    assert get(r, offset=10000)["rows"] == []
    assert r["stocktake"]["movements"] == []


def test_forward_actual_raw_lot_path_preserves_direct_and_work_order_evidence(tracing):
    r = tracing
    graph = trace(r, lot_id=r["raw_position"]["lot_id"], direction="forward")
    assert_graph_integrity(graph)
    raw = node(graph, "inventory_lot", r["raw_position"]["lot_id"])
    issue = node(graph, "material_issue", r["issue"]["id"])
    work = node(graph, "work_order", r["orders"][0]["id"])
    completion = node(graph, "production_completion", r["completions"][0]["id"])
    receipt_id = next(row["source_id"] for row in graph["movements"] if row["source_kind"] == "FINISHED_GOODS_RECEIPT")
    receipt = node(graph, "finished_goods_receipt", receipt_id)
    fg = node(graph, "inventory_lot", r["finished_position"]["lot_id"])
    shipment = node(graph, "sales_shipment", r["shipment"]["id"])
    customer = node(graph, "customer", r["shipment"]["customer_id"])
    for start, finish in [(raw, issue), (issue, work), (work, completion), (completion, receipt),
                          (receipt, fg), (fg, shipment), (shipment, customer)]:
        assert reachable(graph, start, finish), (start, finish)
    assert any(e["evidence"] == "DIRECT" and e["from"] == work["id"] and e["to"] == completion["id"] for e in graph["edges"])
    for start, finish in [(issue, work), (completion, receipt), (receipt, fg), (fg, shipment)]:
        assert any(e["evidence"] == "DIRECT" and e["from"] == start["id"] and e["to"] == finish["id"] for e in graph["edges"])
    assert any(e["evidence"] == "WORK_ORDER" for e in graph["edges"])
    assert not any(e["evidence"] == "DIRECT" and e["from"] == raw["id"] and e["to"] == fg["id"] for e in graph["edges"])
    for origin in r["origins"]:
        original = node(graph, "stock_document_line", origin["id"])
        assert any(e["from"] == original["id"] and e["to"] == raw["id"] and e["evidence"] == "DIRECT" for e in graph["edges"])
    assert any(e["evidence"] == "POOLED" for e in graph["edges"])
    assert graph["limitations"] and any("無法由現有紀錄精確確認" in item["message"] for item in graph["limitations"])
    assert not any(n["source_table"] == "work_order" and n["source_id"] == r["orders"][1]["id"] for n in graph["nodes"])


def test_reverse_shipment_global_lot_transfers_and_genuine_return_lineage(tracing):
    r = tracing
    ledger_before = get(r, limit=500)["rows"]
    draft = customer_returns.create(r, "CRET-TRACE-NO-RESTOCK", quantity="0.125", restock_eligible=False)
    response = customer_returns.finalize(r, draft)
    assert response.status_code == 201, response.text
    recorded = response.json()
    assert recorded["finalization_state"] == "RECORDED_NO_RESTOCK" and recorded["movements"] == []
    assert get(r, limit=500)["rows"] == ledger_before
    graph = trace(r, shipment_id=r["shipment"]["id"], direction="reverse")
    assert_graph_integrity(graph)
    fg = node(graph, "inventory_lot", r["finished_position"]["lot_id"])
    raw = node(graph, "inventory_lot", r["raw_position"]["lot_id"])
    shipment = node(graph, "sales_shipment", r["shipment"]["id"])
    work = node(graph, "work_order", r["orders"][0]["id"])
    assert reachable(graph, raw, work) and reachable(graph, work, fg) and reachable(graph, fg, shipment)
    reverse_lot = trace(r, lot_id=r["finished_position"]["lot_id"], direction="reverse")
    node(reverse_lot, "work_order", r["orders"][0]["id"])
    transfer_rows = [row for row in graph["movements"] if row["source_kind"] == "INVENTORY_TRANSFER"]
    assert len(transfer_rows) == 2
    assert {row["lot_id"] for row in transfer_rows} == {r["finished_position"]["lot_id"]}
    assert len({row["position_id"] for row in transfer_rows}) == 2
    assert {row["warehouse_id"] for row in transfer_rows} == {r["warehouse"]["id"], r["warehouse2"]["id"]}
    assert sum(Fraction(row["qty_base_delta"]) for row in transfer_rows) == 0
    assert len([n for n in graph["nodes"] if n["source_table"] == "finished_goods_receipt"]) == 1
    customer_return = node(graph, "customer_return", r["customer_return"]["id"])
    assert any({e["from"], e["to"]} == {shipment["id"], customer_return["id"]} and e["evidence"] == "DIRECT" for e in graph["edges"])
    no_restock = node(graph, "customer_return", recorded["id"])
    assert any(e["from"] == shipment["id"] and e["to"] == no_restock["id"] and e["evidence"] == "DIRECT" and not e["lineage"] for e in graph["edges"])
    assert no_restock["details"].get("ledger_entries", []) == []
    assert no_restock["details"]["inventory_effect"] == "已完成退貨登記／未退庫／無實體庫存異動"
    assert no_restock["details"]["finalization_state"] == "RECORDED_NO_RESTOCK"
    assert no_restock["quantity"] == recorded["quantity"] == "0.125"
    assert no_restock["unit_code"] == r["sales_unit"]["code"]
    assert not any(row["source_id"] == recorded["id"] for row in graph["movements"])
    forward = trace(r, lot_id=r["raw_position"]["lot_id"], direction="forward")
    node(forward, "customer_return", recorded["id"])
    limits = {item["code"]: item for item in graph["limitations"]}
    assert "不證明時間先後" in limits["NO_INPUT_OUTPUT_ALLOCATION"]["message"]
    assert "不證明時間先後" in limits["POOLED_LOT_SOURCES"]["message"]
    returned = node(graph, "material_return", r["material_return"]["id"])
    assert returned["source_id"] != r["receipts"][0]["id"]
    issue_line_id = r["material_return"]["lines"][0]["origin_line_id"]
    issue_line = node(graph, "material_issue_line", issue_line_id)
    returned_line = node(graph, "material_return_line", r["material_return"]["lines"][0]["id"])
    assert any({e["from"], e["to"]} == {issue_line["id"], returned_line["id"]} and e["evidence"] == "DIRECT" for e in graph["edges"])
    for row in graph["movements"]:
        if row["source_kind"] == "SUPPLIER_RETURN":
            assert row["work_order_id"] is None
            assert row["related_documents"]
        if row["source_kind"] == "MATERIAL_RETURN":
            assert row["work_order_id"] == r["orders"][0]["id"]
            assert any(d["id"] == r["issue"]["id"] for d in row["related_documents"])


def test_many_to_many_receipts_inputs_outputs_and_shipments_never_allocate_consumption(tracing):
    r = tracing
    # Add only facts needed to expose a distinct many-to-many/pooling risk.
    purchase = dict(r, order=r["client"].get("/api/purchase-orders/" + r["receipts"][0]["purchase_order_id"]).json())
    receipt = receiving.header(purchase, "RCV-TRACE-SECOND-RAW")
    receiving.line(purchase, receipt, "0.5", lot_code="LOT-TRACE-SECOND-RAW")
    response = receiving.post(purchase, receipt)
    assert response.status_code == 201, response.text
    raw2 = next(p for p in r["client"].get("/api/stock-positions").json() if p["lot_code"] == "LOT-TRACE-SECOND-RAW")
    extra = issues.header(r, "ISSUE-TRACE-MULTI-INPUT")
    issues.add(r, extra, "0.25", material_id=raw2["item_id"], position_id=raw2["id"], warehouse_id=raw2["warehouse_id"])
    post_issue(r, extra)
    shared = issues.header(r, "ISSUE-TRACE-SHARED-RAW", order=1)
    issues.add(r, shared, "0.25")
    post_issue(r, shared)
    for no, quantity, lot_id, lot_code in [
        ("FGRCV-TRACE-POOLED", "0.25", r["finished_position"]["lot_id"], r["finished_position"]["lot_code"]),
        ("FGRCV-TRACE-SECOND-LOT", "0.25", None, "LOT-TRACE-SECOND-FG")]:
        draft = finished.create(r, no, source_index=0, completion_id=r["completions"][2]["id"],
            completion_version=r["completions"][2]["row_version"], quantity=quantity, lot_id=lot_id, lot_code=lot_code)
        response = finished.post(r, draft)
        assert response.status_code == 201, response.text
    draft = shipping.create(r, "SHIP-TRACE-SECOND", quantity="0.125")
    response = shipping.post(r, draft)
    assert response.status_code == 201, response.text
    graph = trace(r, lot_id=r["raw_position"]["lot_id"], direction="forward")
    assert_graph_integrity(graph)
    for order in r["orders"]:
        node(graph, "work_order", order["id"])
    assert len([n for n in graph["nodes"] if n["source_table"] == "sales_shipment"]) == 2
    assert len([n for n in graph["nodes"] if n["source_table"] == "finished_goods_receipt"]) == 3
    finished_lots = [n for n in graph["nodes"] if n["source_table"] == "inventory_lot" and n["source_id"] != r["raw_position"]["lot_id"]]
    assert len([n for n in finished_lots if n["source_id"] != raw2["lot_id"]]) == 2
    reverse = trace(r, shipment_id=r["shipment"]["id"], direction="reverse")
    node(reverse, "inventory_lot", raw2["lot_id"])
    assert len([n for n in reverse["nodes"] if n["source_table"] == "finished_goods_receipt"]) >= 2
    assert {e["evidence"] for e in reverse["edges"]} >= {"DIRECT", "WORK_ORDER", "POOLED"}
    assert any("無法由現有紀錄精確確認" in item["message"] for item in reverse["limitations"])
    forbidden = {"allocated_qty", "allocated_quantity", "consumed_qty", "consumption_quantity", "allocation_ratio", "quantity"}
    assert all(not forbidden.intersection(edge) for edge in graph["edges"] + reverse["edges"])


def test_read_snapshots_filters_options_and_queries_preserve_all_rows_and_schema(tracing):
    r = tracing
    # A real unrelated output has the selected RAW lot's *text*, another item,
    # another work order, and a shipment to the very same customer. Neither text
    # nor customer membership establishes a manufacturing connection.
    draft = finished.create(r, "FGRCV-TRACE-UNRELATED", source_index=1, quantity="0.25",
        lot_code=r["raw_position"]["lot_code"])
    response = finished.post(r, draft)
    assert response.status_code == 201, response.text
    unrelated_receipt = response.json()
    unrelated_position = next(p for p in r["client"].get("/api/stock-positions").json()
        if p["item_id"] == r["products"][1]["id"])
    draft = shipping.create(r, "SHIP-TRACE-UNRELATED", quantity="0.125",
        sales_order_line_id=r["sales_lines"][1]["id"], position_id=unrelated_position["id"])
    response = shipping.post(r, draft)
    assert response.status_code == 201, response.text
    unrelated_shipment = response.json()
    assert unrelated_shipment["customer_id"] == r["shipment"]["customer_id"]
    unrelated_return_runtime = dict(r, shipment=unrelated_shipment)
    draft = customer_returns.create(unrelated_return_runtime, "CRET-TRACE-UNRELATED-NO-RESTOCK",
        quantity="0.0625", restock_eligible=False)
    response = customer_returns.finalize(unrelated_return_runtime, draft)
    assert response.status_code == 201, response.text
    unrelated_return = response.json()
    assert unrelated_return["finalization_state"] == "RECORDED_NO_RESTOCK"
    unposted = shipping.create(r, "SHIP-TRACE-UNPOSTED", quantity="0.125")
    before = snapshot(r["path"])
    observed = []

    def inspect_query(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            driver = connection.connection.driver_connection
            observed.append((driver.execute("PRAGMA query_only").fetchone()[0], driver.in_transaction))

    event.listen(r["db"].engine, "before_cursor_execute", inspect_query)
    try:
        options = trace(r, mode="options")
        assert r["finished_position"]["lot_id"] in {lot["id"] for lot in options["lots"]}
        assert r["shipment"]["id"] in {shipment["id"] for shipment in options["shipments"]}
        assert unposted["id"] not in {shipment["id"] for shipment in options["shipments"]}
        assert len({lot["id"] for lot in options["lots"]}) == len(options["lots"])
        get(r)
        forward = trace(r, lot_id=r["raw_position"]["lot_id"], direction="forward")
        trace(r, lot_id=r["finished_position"]["lot_id"], direction="reverse")
        reverse = trace(r, shipment_id=r["shipment"]["id"], direction="reverse")
        unrelated_ids = {unrelated_receipt["id"], unrelated_shipment["id"], unrelated_position["lot_id"],
            r["orders"][1]["id"], unposted["id"], unrelated_return["id"]}
        assert not unrelated_ids.intersection(n["source_id"] for n in forward["nodes"] + reverse["nodes"])
    finally:
        event.remove(r["db"].engine, "before_cursor_execute", inspect_query)
    assert observed and set(observed) == {(1, True)}
    for params in [dict(q="%' OR 1=1 --"), dict(lot_code="%_"), dict(document_no="%_'")]:
        assert get(r, **params)["rows"] == []
    for params in [dict(item_kind="INVALID"), dict(warehouse_id="invalid"), dict(warehouse_id=str(uuid4())),
                   dict(movement_type="INVALID"), dict(date_from="2026-02-30"),
                   dict(date_from="2026-11-22", date_to="2026-11-21"), dict(limit=0), dict(offset=-1)]:
        assert r["client"].get(MOVEMENTS, params=params).status_code == 422
    for params in [dict(lot_id="LOT-R8-001"), dict(lot_id=r["finished_position"]["id"]),
                   dict(lot_id=str(uuid4())), dict(shipment_id=str(uuid4()), direction="reverse")]:
        assert r["client"].get(TRACE, params=params).status_code in {404, 422}
    for url in [MOVEMENTS, TRACE]:
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            assert r["client"].request(method, url, json={}).status_code == 405
    assert snapshot(r["path"]) == before
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("PRAGMA query_only").scalar() == 0
