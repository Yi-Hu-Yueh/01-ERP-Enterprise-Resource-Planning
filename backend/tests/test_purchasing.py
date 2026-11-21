"""Purchase aggregates, exact decimal invariants, durable retries and real SQLite guards."""
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.db import DEMO_ACTOR_ID, ROOT, make_engine, migrate
from backend.app.decimal_text import exact_product, exact_sum
from backend.app.main import create_app
from backend.tests.test_foundation import setup, resources, payload
from backend.tests.test_supplier_offers import old_0002, snapshot

URL = "/api/purchase-orders"


@pytest.fixture
def po(setup):
    client, db, path = setup
    cat, unit = resources(client)
    currency = client.post("/api/currencies", json={"code": "QA-CUR", "name": "測試幣別"}).json()
    suppliers = [client.post("/api/suppliers", json={"code": "QA-S"+str(i), "name": "測試供應商"+str(i),
        "currency_id": currency["id"], "is_active": True, "payment_terms": "原始付款條件"}).json() for i in [1, 2]]
    raws = [client.post("/api/raw-materials", json=payload(cat, unit, "QA-R"+str(i))).json() for i in [1, 2]]
    finished = client.post("/api/finished-goods", json=payload(cat, unit, "QA-FG")).json()
    warehouse = client.post("/api/warehouses", json={"code": "QA-W", "name": "測試倉庫", "is_active": True, "mrp_included": False}).json()
    return {"client": client, "db": db, "path": path, "currency": currency, "suppliers": suppliers,
            "raws": raws, "finished": finished, "warehouse": warehouse, "unit": unit}


def header_data(po, number="PO-A", **patch):
    return {"request_key": str(uuid4()), "document_no": number, "supplier_id": po["suppliers"][0]["id"],
        "supplier_name_snapshot": "供應商名稱快照", "currency_id": po["currency"]["id"], "order_date": "2026-11-19",
        "delivery_address_snapshot": "原始交貨地址", "payment_terms_snapshot": "原始付款條件", "note": "QA 採購", **patch}


def create_header(po, number="PO-A", **patch):
    result = po["client"].post(URL, json=header_data(po, number, **patch))
    assert result.status_code == 201, result.text
    return result.json()


def get(po, row):
    return po["client"].get(URL+"/"+row["id"]).json()


def line_data(po, parent, qty="10", price="12.5", item=0, **patch):
    return {"request_key": str(uuid4()), "order_version": parent["row_version"], "item_id": po["raws"][item]["id"],
        "description_snapshot": "原始品名規格快照", "unit_id": po["unit"]["id"], "ordered_qty": qty,
        "unit_price": price, "due_date": "2026-11-20", "preferred_warehouse_id": po["warehouse"]["id"],
        "supplier_offer_id": None, **patch}


def add_line(po, parent, qty="10", price="12.5", **patch):
    result = po["client"].post(URL+"/"+parent["id"]+"/lines", json=line_data(po, get(po, parent), qty, price, **patch))
    assert result.status_code == 201, result.text
    return result.json()


def line_edit(po, parent, row, **patch):
    return {**{k: row[k] for k in ["item_id", "description_snapshot", "unit_id", "ordered_qty", "unit_price", "due_date",
        "preferred_warehouse_id", "supplier_offer_id", "row_version"]}, "order_version": get(po, parent)["row_version"], **patch}


def header_edit(parent, **patch):
    return {**{k: parent[k] for k in ["document_no", "supplier_id", "supplier_name_snapshot", "currency_id", "order_date",
        "delivery_address_snapshot", "payment_terms_snapshot", "note", "row_version"]}, **patch}


def test_exact_baseline_300_325_150_parent_isolation_and_restart(po):
    client = po["client"]
    a, b = create_header(po), create_header(po, "PO-B")
    assert a["total_amount"] == b["total_amount"] == "0"
    one, two = add_line(po, a), add_line(po, a, "20", "8.75", item=1)
    add_line(po, b, "7", "-0.125")
    other = get(po, b)
    assert one["line_amount"] == "125" and two["line_amount"] == "175"
    assert get(po, a)["total_amount"] == "300"
    changed = client.put(URL+"/"+a["id"]+"/lines/"+one["id"], json=line_edit(po, a, one, ordered_qty="12"))
    assert changed.status_code == 200, changed.text
    assert changed.json()["line_amount"] == "150" and get(po, a)["total_amount"] == "325"
    assert get(po, b) == other
    assert client.delete(URL+"/"+a["id"]+"/lines/"+two["id"], params={"row_version": 1, "order_version": get(po, a)["row_version"]}).status_code == 204
    assert get(po, a)["total_amount"] == "150" and get(po, b) == other
    saved = get(po, a)
    assert saved["row_version"] == 5 and saved["lines"][0]["row_version"] == 2
    assert saved["created_by"] == saved["lines"][0]["created_by"] == DEMO_ACTOR_ID
    assert saved["lines"][0]["base_factor_snapshot"] == "1"
    assert client.get(URL+"/"+b["id"]+"/lines/"+one["id"]).status_code == 404
    assert len(client.get(URL, params={"q": "PO-A"}).json()) == 1
    assert len(client.get(URL, params={"q": "供應商名稱快照"}).json()) == 2
    assert client.get(URL, params={"q": "%"}).json() == []
    migrate(po["path"])
    with TestClient(create_app(po["path"])) as reopened:
        assert reopened.get(URL+"/"+a["id"]).json() == saved
        assert reopened.get(URL+"/"+b["id"]).json() == other
    with po["db"].read() as session:
        c = session.connection()
        assert c.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(c.exec_driver_sql("PRAGMA foreign_key_check")) == []
        assert set(c.exec_driver_sql("SELECT typeof(ordered_qty),typeof(unit_price),typeof(base_factor_snapshot) FROM purchase_order_line")) == {("text", "text", "text")}
        for table, column in [("purchase_order", "total_amount"), ("purchase_order_line", "line_amount")]:
            assert column not in [r[1] for r in c.exec_driver_sql("PRAGMA table_info("+table+")")]


@pytest.mark.parametrize("qty,price", [("0", "-1.25"), ("-3.25", "-2"), ("0.000000000000000000001", "1.12345678901234567890123456789"),
    ("1234567890123456789012345678901234567890.123456789", "987654321098765432109876543210.987654321"),
    ("-0.0000000000000000000000000000000000000000000001", "-0.000000000000000000000000000000000000000001")])
def test_exact_signed_fractional_large_products_and_separate_sum(po, qty, price):
    parent = create_header(po)
    row = add_line(po, parent, qty, price)
    assert Fraction(row["line_amount"]) == Fraction(qty) * Fraction(price)
    second = add_line(po, parent, "1", "-0.125")
    assert Fraction(get(po, parent)["total_amount"]) == Fraction(qty)*Fraction(price)+Fraction(second["line_amount"])
    response = po["client"].put(URL+"/"+parent["id"]+"/lines/"+row["id"], json=line_edit(po, parent, row, unit_price="0"))
    assert response.status_code == 200 and response.json()["line_amount"] == "0"
    assert get(po, parent)["total_amount"] == "-0.125"


def test_exact_arithmetic_over_int_conversion_limits_and_large_sum(po):
    huge = "9"*5000
    assert exact_product(huge, "10") == huge+"0"
    assert exact_sum([huge, "-"+huge, "0.000000000000000000000000000001"]) == "0.000000000000000000000000000001"
    assert exact_sum([]) == "0"
    parent = create_header(po)
    values = []
    for i in range(25):
        quantity = str(i-12)+".123456789012345678901234567890123456789"
        row = add_line(po, parent, quantity, "999999999999999999999999999999.999999999999999999999999999999")
        values.append(Fraction(row["line_amount"]))
    assert Fraction(get(po, parent)["total_amount"]) == sum(values, Fraction(0))
    assert get(po, parent)["line_count"] == 25


@pytest.mark.parametrize("patch", [{"supplier_id": str(uuid4())}, {"currency_id": str(uuid4())}, {"document_no": " "},
    {"order_date": "2026-02-30"}, {"order_date": "2026-1-01"}, {"order_date": 0}, {"order_date": "0000-01-01"},
    {"total_amount": "99"}, {"approval_state": "APPROVED"}, {"approved_by": str(uuid4())}, {"id": str(uuid4())}])
def test_invalid_headers_and_system_overrides_do_not_write(po, patch):
    client = po["client"]
    parent = create_header(po)
    assert client.post(URL, json=header_data(po, "NEW", **patch)).status_code == 422
    assert client.put(URL+"/"+parent["id"], json=header_edit(parent, **patch)).status_code == 422
    assert get(po, parent) == parent
    assert len(client.get(URL).json()) == 1


@pytest.mark.parametrize("patch", [{"ordered_qty": 0.1}, {"unit_price": 1}, {"ordered_qty": "NaN"}, {"unit_price": "Infinity"},
    {"ordered_qty": "1e2"}, {"ordered_qty": "1,000"}, {"ordered_qty": True}, {"unit_price": None},
    {"item_id": str(uuid4())}, {"preferred_warehouse_id": str(uuid4())}, {"supplier_offer_id": str(uuid4())},
    {"due_date": "2026-02-30"}, {"unit_id": str(uuid4())}, {"purchase_order_id": str(uuid4())},
    {"base_factor_snapshot": "2"}, {"line_amount": "9"}, {"line_no": 9}, {"created_by": str(uuid4())}])
def test_invalid_line_write_leaves_parent_total_and_version_unchanged(po, patch):
    client = po["client"]
    parent = create_header(po)
    row = add_line(po, parent)
    before = get(po, parent)
    response = client.post(URL+"/"+parent["id"]+"/lines", json={**line_data(po, before), **patch})
    assert response.status_code == 422, response.text
    response = client.put(URL+"/"+parent["id"]+"/lines/"+row["id"], json=line_edit(po, parent, row, **patch))
    assert response.status_code == 422, response.text
    assert get(po, parent) == before


def test_optional_offer_snapshots_and_reference_protection(po):
    client = po["client"]
    parent, other = create_header(po), create_header(po, "PO-B")
    offer_data = {"supplier_id": parent["supplier_id"], "item_id": po["raws"][0]["id"], "unit_price": "900", "is_active": True}
    offer = client.post("/api/supplier-offers", json=offer_data).json()
    row = add_line(po, parent, "10", "12.5", supplier_offer_id=offer["id"])
    add_line(po, other, "1", "3", item=1)  # No offering is required.
    assert row["unit_price"] == "12.5" and row["line_amount"] == "125"
    assert client.put("/api/supplier-offers/"+offer["id"], json={**offer_data, "unit_price": "800", "row_version": 1}).status_code == 200
    assert get(po, parent)["lines"][0] == row
    assert client.delete("/api/supplier-offers/"+offer["id"], params={"row_version": 2}).status_code == 409
    assert client.put("/api/supplier-offers/"+offer["id"], json={**offer_data, "item_id": po["raws"][1]["id"], "row_version": 2}).status_code == 409
    before = get(po, parent)
    assert client.put(URL+"/"+parent["id"], json=header_edit(before, supplier_id=po["suppliers"][1]["id"])).status_code == 422
    assert client.delete(URL+"/"+parent["id"], params={"row_version": before["row_version"]}).status_code == 409
    for resource, record in [("suppliers", po["suppliers"][0]), ("raw-materials", po["raws"][0]), ("warehouses", po["warehouse"])]:
        assert client.delete("/api/"+resource+"/"+record["id"], params={"row_version": record["row_version"]}).status_code == 409
    for resource, record in [("suppliers", po["suppliers"][0]), ("raw-materials", po["raws"][0])]:
        assert client.get("/api/"+resource+"/"+record["id"]).json() == record
    assert client.post(URL+"/"+parent["id"]+"/lines", json=line_data(po, before, item_id=po["finished"]["id"])).status_code == 422
    assert client.put(URL+"/"+other["id"]+"/lines/"+row["id"], json=line_edit(po, other, row)).status_code == 404
    assert get(po, parent) == before


def test_durable_retry_receipts_conflicting_payloads_and_deleted_resources(po):
    client = po["client"]
    body = header_data(po)
    one = client.post(URL, json=body)
    replay = client.post(URL, json=body)
    assert one.status_code == 201 and replay.status_code == 200 and one.json() == replay.json()
    parent = one.json()
    assert client.post(URL, json={**body, "note": "different"}).status_code == 409
    assert client.post(URL, json={**body, "request_key": str(uuid4())}).status_code == 409
    payload = line_data(po, parent)
    a = client.post(URL+"/"+parent["id"]+"/lines", json=payload)
    b = client.post(URL+"/"+parent["id"]+"/lines", json=payload)
    assert a.status_code == 201 and b.status_code == 200 and a.json() == b.json()
    saved = get(po, parent)
    assert saved["line_count"] == 1 and saved["total_amount"] == "125" and saved["row_version"] == 2
    assert client.post(URL+"/"+parent["id"]+"/lines", json={**payload, "order_version": 2}).status_code == 200
    assert client.post(URL+"/"+parent["id"]+"/lines", json={**payload, "unit_price": "0"}).status_code == 409
    assert client.delete(URL+"/"+parent["id"]+"/lines/"+a.json()["id"], params={"row_version": 1, "order_version": 2}).status_code == 204
    assert client.post(URL+"/"+parent["id"]+"/lines", json=payload).status_code == 410
    assert get(po, parent)["total_amount"] == "0"
    assert client.delete(URL+"/"+parent["id"], params={"row_version": 3}).status_code == 204
    assert client.post(URL, json=body).status_code == 410
    assert client.get(URL).json() == []


def test_concurrent_duplicate_and_stale_aggregate_writes(po):
    client = po["client"]
    body = header_data(po)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: client.post(URL, json=body).status_code, range(2))) == [200, 201]
    parent = client.get(URL).json()[0]
    payload = line_data(po, parent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: client.post(URL+"/"+parent["id"]+"/lines", json=payload).status_code, range(2))) == [200, 201]
    saved = get(po, parent)
    row = saved["lines"][0]
    update = line_edit(po, parent, row)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda price: client.put(URL+"/"+parent["id"]+"/lines/"+row["id"], json={**update,"unit_price":price}).status_code, ["2","3"])) == [200,409]
    latest = get(po, parent)
    assert latest["row_version"] == 3 and latest["line_count"] == 1
    assert latest["total_amount"] in {"20","30"}
    assert client.put(URL+"/"+parent["id"], json=header_edit(saved, note="stale")).status_code == 409
    assert client.delete(URL+"/"+parent["id"]+"/lines/"+row["id"], params={"row_version": 1,"order_version":2}).status_code == 409
    assert get(po, parent) == latest


def test_real_sqlite_constraints_and_all_failed_mutations_rollback(po):
    client, db = po["client"], po["db"]
    parent = create_header(po)
    row = add_line(po, parent)
    before = get(po, parent)
    with db.engine.connect() as a, db.engine.connect() as b:
        assert a.connection.driver_connection is not b.connection.driver_connection
        assert a.exec_driver_sql("PRAGMA foreign_keys").scalar() == b.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    for sql, parameters in [
        ("UPDATE purchase_order_line SET purchase_order_id=?", (str(uuid4()),)),
        ("UPDATE purchase_order_line SET item_id=?", (po["finished"]["id"],)),
        ("UPDATE purchase_order_line SET unit_id=?", (str(uuid4()),)),
        ("UPDATE purchase_order_line SET preferred_warehouse_id=?", (str(uuid4()),)),
        ("UPDATE purchase_order_line SET ordered_qty='1e2'", ()),
        ("UPDATE purchase_order_line SET unit_price='01'", ()),
        ("DELETE FROM purchase_order WHERE id=?", (parent["id"],)),
        ("DELETE FROM supplier WHERE id=?", (parent["supplier_id"],)),
        ("DELETE FROM item WHERE id=?", (row["item_id"],)),
        ("UPDATE item SET item_kind='FINISHED' WHERE id=?", (row["item_id"],)),
        ("UPDATE purchase_order SET supplier_id=?", (str(uuid4()),)),
    ]:
        with pytest.raises(IntegrityError):
            with db.write() as session:
                session.connection().exec_driver_sql(sql, parameters)
        assert get(po, parent) == before
    with db.write() as session:
        c = session.connection()
        c.exec_driver_sql("CREATE TRIGGER fail_insert AFTER INSERT ON purchase_order_line WHEN NEW.ordered_qty='999' BEGIN SELECT RAISE(ABORT,'forced create rollback'); END")
        c.exec_driver_sql("CREATE TRIGGER fail_update AFTER UPDATE ON purchase_order_line WHEN NEW.unit_price='777' BEGIN SELECT RAISE(ABORT,'forced update rollback'); END")
        c.exec_driver_sql("CREATE TRIGGER fail_delete AFTER DELETE ON purchase_order_line BEGIN SELECT RAISE(ABORT,'forced delete rollback'); END")
    assert client.post(URL+"/"+parent["id"]+"/lines", json=line_data(po,before,"999")).status_code == 409
    assert client.put(URL+"/"+parent["id"]+"/lines/"+row["id"], json=line_edit(po,parent,row,unit_price="777")).status_code == 409
    assert client.delete(URL+"/"+parent["id"]+"/lines/"+row["id"],params={"row_version":1,"order_version":2}).status_code == 409
    assert get(po,parent) == before
    with db.write() as session:
        session.connection().exec_driver_sql("CREATE TRIGGER fail_receipt BEFORE INSERT ON purchase_create_receipt BEGIN SELECT RAISE(ABORT,'forced receipt rollback'); END")
    assert client.post(URL,json=header_data(po,"FAIL-RECEIPT")).status_code == 409
    assert client.post(URL+"/"+parent["id"]+"/lines",json=line_data(po,before,"3")).status_code == 409
    assert get(po,parent) == before and len(client.get(URL).json()) == 1
    with db.read() as session:
        assert session.connection().exec_driver_sql("SELECT count(*) FROM purchase_create_receipt").scalar() == 2


def test_additive_0003_upgrade_and_partial_0004_failure_rollback(tmp_path):
    from alembic import command
    from alembic.config import Config
    def old(path):
        old_0002(path)
        engine=make_engine(path)
        config=Config(str(ROOT/'backend'/'alembic.ini'));config.set_main_option('script_location',str(ROOT/'backend'/'migrations'))
        with engine.connect().execution_options(erp_write=True) as connection:
            config.attributes['connection']=connection;command.upgrade(config,'0003_supplier_offers')
        engine.dispose()
    path=tmp_path/'old.sqlite';old(path);before=snapshot(path);migrate(path);migrate(path);after=snapshot(path)
    assert all(after['rows'][t]==rows for t,rows in before['rows'].items())
    assert all(obj in after['objects'] for obj in before['objects'])
    assert all(after['rows'][t]==[] for t in ['purchase_order','purchase_order_line','purchase_create_receipt'])
    blocked=tmp_path/'blocked.sqlite';old(blocked)
    with sqlite3.connect(blocked) as c:c.execute('CREATE TABLE purchase_order_line (legacy_marker TEXT)')
    original=snapshot(blocked)
    with pytest.raises(OperationalError):migrate(blocked)
    assert snapshot(blocked)==original
    with sqlite3.connect(blocked) as c:
        assert c.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0003_supplier_offers'
        assert c.execute("SELECT name FROM sqlite_master WHERE name='purchase_order'").fetchone() is None
