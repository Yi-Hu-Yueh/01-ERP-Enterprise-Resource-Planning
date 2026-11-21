"""Five independent reporting contracts on synthetic SQLite only; never Owner rows."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime
from fractions import Fraction
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from backend.app.db import ROOT
from backend.app.local_demo import create_local_demo_app
from backend.tests.seed_customer_returns_browser import seed
from backend.tests.test_foundation import payload
from backend.tests import test_purchasing as purchasing
from backend.tests import test_receiving as receiving
from backend.tests import test_sales as sales
from backend.tests import test_sales_shipments as shipping
from backend.tests import test_customer_returns as returning
from backend.tests import test_work_orders as work
from backend.tests import test_completions as completions
from backend.tests import test_finished_goods_receipts as finished
from backend.tests import test_material_issues as issues
from backend.tests import test_material_returns as material_returns


URL = "/api/business-statistics"
ARTIFACTS = ROOT / "artifacts" / "round28-2026-11-21" / "backend-tests"
PORT, UI_PORT = 8048, 5211
TINY = "0.000000000000000000000000000000000001"
PRICE = "1.123456789012345678901234567890123456789"


def artifact_directory(prefix):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=ARTIFACTS)).resolve()


def client_for(app):
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))


@contextmanager
def event_clock(value):
    """Control only the synthetic transaction clock, never reporting arithmetic."""
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    with patch("backend.app.models.datetime", Clock), patch("backend.app.local_demo.datetime", Clock):
        yield


def posted(response):
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(scope="session")
def statistics_source():
    path = artifact_directory("source-") / "synthetic.sqlite"
    with event_clock("2026-09-30T10:00:00.000000Z"):
        metadata = seed(path)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        r = dict(metadata, client=client, app=app, db=app.state.db, path=path)
        r["currency"] = client.get("/api/currencies").json()[0]
        raw = {**r, "unit": r["raw_unit"]}
        extra = purchasing.add_line(raw, r["order"] if r["order"].get("supplier_id") else
            client.get("/api/purchase-orders/" + r["sources"][0]["purchase_order_id"]).json(),
            "3", "2.25", item=1)
        purchase_order = client.get("/api/purchase-orders/" + extra["purchase_order_id"]).json()
        rr = {**raw, "order": purchase_order, "sources": [r["sources"][0], extra]}
        receipt = receiving.header(rr, "RCV-STATS-%_MULTI", business_date="2030-01-01")
        with r["db"].read() as session:
            lot = dict(session.connection().exec_driver_sql(
                "SELECT lot_code,supplier_id,supplier_lot_code,manufactured_date,expiry_date FROM inventory_lot WHERE item_id=? AND lot_code=?",
                (r["raws"][0]["id"], "LOT-R8-001")).mappings().one())
        assert lot.pop("supplier_id") == purchase_order["supplier_id"]
        receiving.line(rr, receipt, "0.5", **lot)
        receiving.line(rr, receipt, "1.25", source=1, lot_code=None)
        with event_clock("2026-10-01T23:59:59.999999Z"):
            metadata["statistics_receipt"] = posted(receiving.post(rr, receipt))
        draft = receiving.header(rr, "RCV-STATS-DRAFT")
        receiving.line(rr, draft, "999", lot_code="UNPOSTED-STATS")

        other_unit = posted(client.post("/api/units", json=dict(code="PAIR-STATS", name="對")))
        category = client.get("/api/categories").json()[0]
        other_raw = posted(client.post("/api/raw-materials", json=payload(category, other_unit, "RAW-STATS-PAIR")))
        other_product = posted(client.post("/api/finished-goods", json=payload(category, other_unit, "FG-STATS-PAIR")))
        other_currency = posted(client.post("/api/currencies", json=dict(code="CUR-STATS-OTHER", name="其他幣別")))
        other_po = purchasing.create_header(raw, "PO-STATS-OTHER-CURRENCY",
            supplier_id=r["suppliers"][1]["id"], currency_id=other_currency["id"])
        purchasing.add_line(raw, other_po, "0.1", "0.2")
        purchasing.add_line({**raw, "raws": [other_raw], "unit": other_unit}, other_po, "2.125", PRICE)

        shipment = shipping.create(r, "SHIP-STATS-PARTIAL", quantity="0.125", business_date="2030-01-02")
        with event_clock("2026-10-02T00:00:00.000000Z"):
            metadata["statistics_shipment"] = posted(shipping.post(r, shipment))
        metadata["statistics_restock"] = None
        with event_clock("2026-10-04T23:59:59.999999Z"):
            document = returning.create(r, "CRET-STATS-RESTOCK", quantity="0.25", business_date="2030-01-03")
            metadata["statistics_restock"] = posted(returning.finalize(r, document))
            document = returning.create(r, "CRET-STATS-NO-RESTOCK", quantity="0.125", restock_eligible=False)
            metadata["statistics_nonrestock"] = posted(returning.finalize(r, document))
        returning.create(r, "CRET-STATS-DRAFT", quantity="0.05")

        other_so = sales.create(r, "SO-STATS-OTHER", customer_id=r["customers"][1]["id"])
        sales.add(r, other_so, TINY, PRICE)
        sales.add(r, other_so, "2.125", "0.125", index=1)
        sales.add({**r, "products": [other_product], "unit": other_unit}, other_so, "2.5", "-7.25")

        other_wo = work.create({**r, "products": [other_product]}, "WO-STATS-PAIR", planned_qty_base="4.125")
        cr = {**r, "orders": [other_wo]}
        draft = completions.create(cr, "COMP-STATS-PAIR", good_qty_base="0.5", defective_qty_base="0.125", report_date="2030-01-04")
        with event_clock("2026-10-03T12:00:00.000000Z"):
            metadata["statistics_completion"] = posted(completions.confirm(cr, draft))
        completions.create(r, "COMP-STATS-DRAFT", good_qty_base="999", defective_qty_base="999")
        fr = {**r, "completions": [r["completions"][2]], "products": [r["products"][0]]}
        fg_receipt = finished.create(fr, "FGRCV-STATS-PARTIAL", quantity="0.25", business_date="2030-01-05", lot_code="LOT-STATS-PARTIAL")
        with event_clock("2026-10-03T00:00:00.000000Z"):
            metadata["statistics_fg_receipt"] = posted(finished.post(fr, fg_receipt))
        finished.create(fr, "FGRCV-STATS-DRAFT", quantity="999", lot_code="LOT-STATS-DRAFT")
        issue = issues.header(r, "ISSUE-STATS-DRAFT")
        issues.add(r, issue, "999")
        material_return = material_returns.header(r, "MRET-STATS-DRAFT")
        material_returns.add(r, material_return, "999")
        metadata.update(statistics_purchase_order=purchase_order, statistics_other_po=other_po,
            statistics_other_so=sales.get(r, other_so), statistics_other_wo=other_wo,
            statistics_other_unit=other_unit, statistics_other_raw=other_raw,
            statistics_other_product=other_product, statistics_other_currency=other_currency)
    app.state.db.engine.dispose()
    (ARTIFACTS / "source-metadata.json").write_text(
        json.dumps(dict(metadata, database=str(path)), ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return path, metadata


@pytest.fixture
def statistics(statistics_source):
    source, metadata = statistics_source
    path = artifact_directory("case-") / "statistics.sqlite"
    assert source.is_relative_to(ARTIFACTS.resolve()) and not path.exists()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(path)) as target:
            original.backup(target)
    assert not path.samefile(source)
    app = create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT)
    with client_for(app) as client:
        yield dict(deepcopy(metadata), client=client, app=app, db=app.state.db, path=path)


def report(r, area, **params):
    response = r["client"].get(URL, params=dict(area=area, **params))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["read_only"] is True and result["area"] == area
    def exact_numbers(value):
        assert not isinstance(value, float), value
        if isinstance(value, dict):
            for child in value.values():
                exact_numbers(child)
        elif isinstance(value, list):
            for child in value:
                exact_numbers(child)
    exact_numbers(result)
    return result


def quantities(result, unit):
    return next(row for row in result["summary"]["quantities"] if row["unit_id"] == unit["id"])


def counts(result):
    return {row["key"]: row["count"] for row in result["summary"]["counts"]}


def all_snapshot(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        objects = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = [row[1] for row in objects if row[0] == "table" and not row[1].startswith("sqlite_")]
        return objects, {table: connection.execute('SELECT rowid,* FROM "' + table + '" ORDER BY rowid').fetchall()
            for table in tables}


def assert_metrics(row, expected):
    for key, value in expected.items():
        assert isinstance(row[key], str), (key, row[key])
        assert Fraction(row[key]) == Fraction(value), (key, row[key], value)


def test_purchase_source_grains_exact_header_sums_gross_outstanding_and_currency_units(statistics):
    r = statistics
    result = report(r, "purchasing")
    assert counts(result) == dict(order_count=3, receipt_count=3, supplier_return_count=3)
    assert_metrics(quantities(result, r["raw_unit"]), dict(ordered_qty_base="20.1",
        gross_received_qty_base="11.75", posted_return_qty_base="6", net_retained_qty_base="5.75",
        current_outstanding_qty_base="8.35"))
    assert_metrics(quantities(result, r["statistics_other_unit"]), dict(ordered_qty_base="2.125",
        gross_received_qty_base="0", posted_return_qty_base="0", net_retained_qty_base="0",
        current_outstanding_qty_base="2.125"))
    amounts = {row["currency_id"]: row for row in result["summary"]["amounts"]}
    assert len(amounts) == 2
    own = amounts[r["statistics_other_currency"]["id"]]
    assert own["order_count"] == 1 and isinstance(own["amount"], str)
    assert Fraction(own["amount"]) == Fraction("2.125") * Fraction(PRICE) + Fraction("0.02")
    original = next(row for key, row in amounts.items() if key != r["statistics_other_currency"]["id"])
    assert (original["amount"], original["order_count"]) == ("199", 2)
    assert "gross" in result["scope"]["outstanding_basis"].lower() or "毛" in result["scope"]["outstanding_basis"]
    # Independent existing authoritative inquiry must retain its gross rule.
    outstanding = r["client"].get("/api/purchase-outstanding").json()
    assert sum(Fraction(row["outstanding_qty_base"]) for row in outstanding["quantity_totals"]
        if row["unit_id"] == r["raw_unit"]["id"]) == Fraction("8.35")


def test_manufacturing_plans_confirmed_output_and_physical_postings_remain_separate(statistics):
    r = statistics
    result = report(r, "manufacturing")
    assert counts(result) == dict(work_order_count=3, completion_count=4, fg_receipt_count=2,
        material_issue_count=1, material_return_count=1)
    assert_metrics(quantities(result, r["sales_unit"]), dict(planned_qty_base="6.25",
        confirmed_good_qty_base="3.75", confirmed_defective_qty_base="0.75", confirmed_total_qty_base="4.5",
        fg_received_qty_base="1.5"))
    assert_metrics(quantities(result, r["statistics_other_unit"]), dict(planned_qty_base="4.125",
        confirmed_good_qty_base="0.5", confirmed_defective_qty_base="0.125", confirmed_total_qty_base="0.625",
        fg_received_qty_base="0"))
    assert_metrics(quantities(result, r["raw_unit"]), dict(material_issued_qty_base="1.75", material_returned_qty_base="0.75",
        net_material_issued_qty_base="1"))
    assert not {"planned_qty_base", "confirmed_good_qty_base", "confirmed_defective_qty_base", "confirmed_total_qty_base",
        "fg_received_qty_base"}.intersection(quantities(result, r["raw_unit"]))
    assert not {"material_issued_qty_base", "material_returned_qty_base", "net_material_issued_qty_base"}.intersection(
        quantities(result, r["sales_unit"]))
    indicators = {row["unit_id"]: row for row in result["summary"]["indicators"]}
    assert (indicators[r["sales_unit"]["id"]]["numerator"], indicators[r["sales_unit"]["id"]]["denominator"]) == ("3.75", "4.5")
    assert (indicators[r["statistics_other_unit"]["id"]]["numerator"],
        indicators[r["statistics_other_unit"]["id"]]["denominator"]) == ("0.5", "0.625")
    assert r["raw_unit"]["id"] not in indicators
    assert result["summary"]["amounts"] == []
    with r["db"].read() as session:
        rows = session.connection().exec_driver_sql(
            "SELECT domain,qty_base_delta FROM physical_stock_ledger WHERE work_order_id=?",
            (r["statistics_other_wo"]["id"],)).all()
    assert rows == [], "Confirmed production without an FG posting creates no physical output"


def test_sales_exact_per_order_values_shipments_return_dispositions_and_gross_outstanding(statistics):
    r = statistics
    result = report(r, "sales")
    assert counts(result) == dict(order_count=2, shipment_count=2, customer_return_count=2, nonrestock_return_count=1)
    assert_metrics(quantities(result, r["sales_unit"]), dict(
        ordered_qty_base=Fraction("5.875") + Fraction(TINY), gross_shipped_qty_base="0.875",
        finalized_returned_qty_base="0.375", restocked_return_qty_base="0.25",
        nonrestocked_return_qty_base="0.125", net_physical_shipped_qty_base="0.625",
        current_outstanding_qty_base=Fraction("5") + Fraction(TINY)))
    assert_metrics(quantities(result, r["statistics_other_unit"]), dict(ordered_qty_base="2.5",
        gross_shipped_qty_base="0", current_outstanding_qty_base="2.5"))
    # Sales orders have no persisted currency. Customer defaults must never be
    # promoted to order-currency evidence or used to sum unrelated order values.
    values = result["summary"]["amounts"]
    assert len(values) == 2 and all(row["currency_id"] is None and row["order_count"] == 1 for row in values)
    assert all(isinstance(row["amount"], str) for row in values)
    expected = {r["order"]["id"]: Fraction("68.125"), r["statistics_other_so"]["id"]:
        Fraction(TINY) * Fraction(PRICE) + Fraction("0.265625") - Fraction("18.125")}
    assert {row["order_id"]: Fraction(row["amount"]) for row in values} == expected
    assert result["scope"]["amount_basis"] and result["notes"]
    original = r["client"].get("/api/outstanding-sales").json()
    original_book = next(row for row in original["quantity_totals"] if row["unit_id"] == r["sales_unit"]["id"])
    assert Fraction(original_book["outstanding_qty_base"]) == Fraction("5") + Fraction(TINY)


def test_independent_utc_event_periods_filters_group_reconciliation_and_detail_paging(statistics):
    r = statistics
    derived = dict(net_retained_qty_base=("gross_received_qty_base", "posted_return_qty_base"),
        net_physical_shipped_qty_base=("gross_shipped_qty_base", "restocked_return_qty_base"),
        net_material_issued_qty_base=("material_issued_qty_base", "material_returned_qty_base"))
    for area in ["purchasing", "manufacturing", "sales"]:
        result = report(r, area, limit=200)
        detail_rows = [row for dataset in result["details"] for row in dataset["rows"]]
        for dataset in result["details"]:
            assert dataset["total"] == len(dataset["rows"])
            assert len({row["id"] for row in dataset["rows"]}) == len(dataset["rows"])
            assert all(row["source_id"] and row["document_no"] and row["date_basis"] for row in dataset["rows"])
        for unit in result["summary"]["quantities"]:
            rows = [row for row in detail_rows if row["unit_id"] == unit["unit_id"]]
            for key, value in unit.items():
                if not key.endswith("_qty_base"):
                    continue
                if key in derived:
                    left, right = derived[key]
                    expected = sum((Fraction(row["metrics"].get(left, "0")) - Fraction(row["metrics"].get(right, "0"))
                        for row in rows), Fraction(0))
                else:
                    expected = sum((Fraction(row["metrics"].get(key, "0")) for row in rows), Fraction(0))
                assert Fraction(value) == expected, (area, key, value, expected)
                for group in result["breakdowns"]:
                    if group["key"] == "statuses":
                        continue
                    grouped = sum((Fraction(q.get(key, "0")) for bucket in group["rows"] for q in bucket["quantities"]
                        if q["unit_id"] == unit["unit_id"]), Fraction(0))
                    assert grouped == expected, (area, group["key"], key)
        page = report(r, area, offset=1, limit=1)
        assert page["summary"] == result["summary"] and page["breakdowns"] == result["breakdowns"]
        for original, paged in zip(result["details"], page["details"]):
            assert paged["key"] == original["key"] and paged["total"] == original["total"]
            assert paged["rows"] == original["rows"][1:2]

    raw_filter = report(r, "purchasing", item_id=r["raws"][1]["id"])
    assert counts(raw_filter)["order_count"] == 2
    assert raw_filter["summary"]["amounts"][0]["amount"] == "199", "Matching headers retain their complete amount once"
    assert_metrics(quantities(raw_filter, r["raw_unit"]), dict(ordered_qty_base="8", gross_received_qty_base="1.25",
        posted_return_qty_base="0", current_outstanding_qty_base="6.75"))
    supplier = report(r, "purchasing", party_id=r["suppliers"][1]["id"])
    assert counts(supplier) == dict(order_count=1, receipt_count=0, supplier_return_count=0)
    literal = report(r, "purchasing", document_no="%_MULTI")
    assert counts(literal) == dict(order_count=0, receipt_count=1, supplier_return_count=0)
    assert all(row["document_no"] == "RCV-STATS-%_MULTI" for source in literal["details"] for row in source["rows"])
    selected_work = report(r, "manufacturing", work_order_id=r["statistics_other_wo"]["id"])
    assert counts(selected_work) == dict(work_order_count=1, completion_count=1, fg_receipt_count=0,
        material_issue_count=0, material_return_count=0)
    assert_metrics(quantities(selected_work, r["statistics_other_unit"]), dict(planned_qty_base="4.125", confirmed_good_qty_base="0.5"))
    customer = report(r, "sales", party_id=r["customers"][0]["id"])
    assert counts(customer)["order_count"] == 1 and customer["summary"]["amounts"][0]["amount"] == "68.125"
    assert_metrics(quantities(customer, r["sales_unit"]), dict(current_outstanding_qty_base="2.875"))

    purchase_day = report(r, "purchasing", date_from="2026-10-01", date_to="2026-10-01")
    assert counts(purchase_day) == dict(order_count=0, receipt_count=1, supplier_return_count=0)
    assert_metrics(quantities(purchase_day, r["raw_unit"]), dict(gross_received_qty_base="1.75", current_outstanding_qty_base="0"))
    assert report(r, "purchasing", date_from="2026-10-02", date_to="2026-10-02")["summary"]["quantities"] == []
    shipment_day = report(r, "sales", date_from="2026-10-02", date_to="2026-10-02")
    assert counts(shipment_day) == dict(order_count=0, shipment_count=1, customer_return_count=0, nonrestock_return_count=0)
    assert_metrics(quantities(shipment_day, r["sales_unit"]), dict(gross_shipped_qty_base="0.125", current_outstanding_qty_base="0"))
    manufacturing_day = report(r, "manufacturing", date_from="2026-10-03", date_to="2026-10-03")
    assert counts(manufacturing_day) == dict(work_order_count=0, completion_count=1, fg_receipt_count=1,
        material_issue_count=0, material_return_count=0)
    assert_metrics(quantities(manufacturing_day, r["sales_unit"]), dict(fg_received_qty_base="0.25", confirmed_good_qty_base="0"))
    assert_metrics(quantities(manufacturing_day, r["statistics_other_unit"]), dict(confirmed_good_qty_base="0.5", fg_received_qty_base="0"))
    return_day = report(r, "sales", date_from="2026-10-04", date_to="2026-10-04")
    assert counts(return_day) == dict(order_count=0, shipment_count=0, customer_return_count=2, nonrestock_return_count=1)
    assert_metrics(quantities(return_day, r["sales_unit"]), dict(restocked_return_qty_base="0.25",
        nonrestocked_return_qty_base="0.125", net_physical_shipped_qty_base="-0.25"))
    cohort = report(r, "purchasing", date_from="2026-11-19", date_to="2026-11-19")
    assert counts(cohort) == dict(order_count=3, receipt_count=0, supplier_return_count=0)
    assert_metrics(quantities(cohort, r["raw_unit"]), dict(gross_received_qty_base="0", current_outstanding_qty_base="8.35"))
    for params in [dict(date_from="2026-02-30"), dict(date_from="2026-10-05", date_to="2026-10-01"), dict(limit=201)]:
        assert r["client"].get(URL, params=dict(area="sales", **params)).status_code == 422


def test_readonly_schema_rows_rowids_query_guard_and_refresh_of_fresh_commits(statistics):
    r = statistics
    before = all_snapshot(r["path"])
    observations = []
    attempts = []

    def inspect(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            driver = connection.connection.driver_connection
            flag = driver.execute("PRAGMA query_only").fetchone()[0]
            observations.append((statement, flag))
            if flag and not attempts:
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    driver.execute("UPDATE sales_order SET document_no=document_no WHERE 0")
                attempts.append(True)

    event.listen(r["db"].engine, "before_cursor_execute", inspect)
    try:
        first = report(r, "sales")
        first_query_count = len(observations)
        assert first_query_count > 0 and all(flag == 1 for _, flag in observations) and attempts == [True]
        observations.clear()
        report(r, "purchasing")
        report(r, "manufacturing")
        response = r["client"].get(URL, params=dict(area="sales", mode="options"))
        assert response.status_code == 200, response.text
        assert observations and all(flag == 1 for _, flag in observations)
    finally:
        event.remove(r["db"].engine, "before_cursor_execute", inspect)
    for method in ["POST", "PUT", "DELETE", "PATCH"]:
        assert r["client"].request(method, URL, json={}).status_code == 405
    assert all_snapshot(r["path"]) == before
    with r["db"].read() as session:
        assert session.connection().exec_driver_sql("PRAGMA query_only").scalar() == 0

    # A real committed synthetic order proves refresh is not cached. The same
    # report uses the same query count after its source cardinality increases.
    fresh = sales.create(r, "SO-STATS-COMMITTED-REFRESH")
    sales.add(r, fresh, "0.3", "0.2")
    committed = all_snapshot(r["path"])
    observations.clear()
    event.listen(r["db"].engine, "before_cursor_execute", inspect)
    try:
        refreshed = report(r, "sales")
    finally:
        event.remove(r["db"].engine, "before_cursor_execute", inspect)
    assert len(observations) == first_query_count and all(flag == 1 for _, flag in observations)
    assert counts(refreshed)["order_count"] == counts(first)["order_count"] + 1
    assert Fraction(quantities(refreshed, r["sales_unit"])["ordered_qty_base"]) == (
        Fraction(quantities(first, r["sales_unit"])["ordered_qty_base"]) + Fraction("0.3"))
    assert any(row["order_id"] == fresh["id"] and row["amount"] == "0.06"
        for row in refreshed["summary"]["amounts"])
    assert all_snapshot(r["path"]) == committed
