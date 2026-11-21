"""Actual many-to-many, exact TEXT, DB-boundary integrity and migration acceptance."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.db import ROOT, DEMO_ACTOR_ID, make_engine, migrate
from backend.app.main import create_app
from backend.tests.test_foundation import setup, resources, payload
from backend.tests.test_master_data import legacy_database

URL = "/api/supplier-offers"


@pytest.fixture
def matrix(setup):
    client, db, path = setup
    category, unit = resources(client)
    currency = client.post("/api/currencies", json={"code": "QA", "name": "測試參照"}).json()
    suppliers = [client.post("/api/suppliers", json={"code": "S"+str(i), "name": "供應商"+str(i),
        "currency_id": currency["id"], "is_active": True}).json() for i in [1, 2]]
    raws = [client.post("/api/raw-materials", json=payload(category, unit, "R"+str(i))).json() for i in [1, 2]]
    finished = client.post("/api/finished-goods", json=payload(category, unit, "FG")).json()
    return client, db, path, suppliers, raws, finished


def data(supplier, item, price="1.25", active=True):
    return {"supplier_id": supplier["id"], "item_id": item["id"], "unit_price": price, "is_active": active}


def edit(row, **patch):
    return {**{key: row[key] for key in ["supplier_id", "item_id", "unit_price", "is_active", "row_version"]}, **patch}


def post(client, body):
    result = client.post(URL, json=body)
    assert result.status_code == 201, result.text
    return result.json()


def test_complete_two_by_two_matrix_search_independent_delete_and_restart(matrix):
    client, db, path, suppliers, raws, _ = matrix
    masters = {"suppliers": client.get("/api/suppliers").json(), "raw-materials": client.get("/api/raw-materials").json()}
    records = [post(client, data(s, r, p)) for (s, r), p in zip(
        [(s, r) for s in suppliers for r in raws], ["0", "-2.125", "0.000000000000000000000000000001", "123456789012345678901234567890.1234567890123456789"])]
    rows = client.get(URL).json()
    assert {(r["supplier_id"], r["item_id"]) for r in rows} == {(s["id"], r["id"]) for s in suppliers for r in raws}
    for q in ["S1", suppliers[0]["name"], "R1", raws[0]["name"]]:
        assert client.get(URL, params={"q": q}).json()
    assert len(client.get(URL, params={"q": "S1"}).json()) == 2
    assert len(client.get(URL, params={"q": "R1"}).json()) == 2
    assert client.get(URL, params={"q": "%"}).json() == []
    for original in records:
        assert original["row_version"] == 1 and original["created_by"] == DEMO_ACTOR_ID
        assert client.get(URL+"/"+original["id"]).json() == original
    removed = records.pop()
    assert client.delete(URL+"/"+removed["id"], params={"row_version": 1}).status_code == 204
    assert client.get(URL+"/"+removed["id"]).status_code == 404
    for resource, before in masters.items():
        assert client.get("/api/"+resource).json() == before
    migrate(path)
    with TestClient(create_app(path)) as reopened:
        for original in records:
            assert reopened.get(URL+"/"+original["id"]).json() == original
    with db.read() as session:
        connection = session.connection()
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []
        assert set(connection.exec_driver_sql("SELECT typeof(unit_price) FROM supplier_offer").scalars()) == {"text"}


@pytest.mark.parametrize("value,canonical", [
    ("0", "0"), ("-0.000", "0"), ("00012.34000", "12.34"), ("-0.125", "-0.125"),
    ("123456789012345678901234567890123456789012345678901234567890.12345678901234567890123456789",
     "123456789012345678901234567890123456789012345678901234567890.12345678901234567890123456789"),
    ("0."+"0"*100+"1", "0."+"0"*100+"1"), ("-"+"9"*5000+".1", "-"+"9"*5000+".1"),
])
def test_exact_decimal_without_float_context_rounding_or_business_limits(matrix, value, canonical):
    client, db, _, suppliers, raws, _ = matrix
    row = post(client, data(suppliers[0], raws[0], value))
    assert row["unit_price"] == canonical and isinstance(row["unit_price"], str)
    with db.read() as session:
        assert session.connection().exec_driver_sql("SELECT unit_price,typeof(unit_price) FROM supplier_offer").one() == (canonical, "text")
    changed = client.put(URL+"/"+row["id"], json=edit(row, unit_price="-123.000001")).json()
    assert changed["unit_price"] == "-123.000001" and changed["row_version"] == 2
    for key in ["id", "created_at", "created_by"]:
        assert changed[key] == row[key]
    assert changed["updated_at"] != row["updated_at"]


@pytest.mark.parametrize("price", [0, -0.1, True, None, "", "NaN", "Infinity", "1e2", "+1", "1,000", " 1", "1.", ".1", "1.2.3", "--1"])
def test_invalid_price_rejected_without_mutation(matrix, price):
    client, _, _, suppliers, raws, _ = matrix
    row = post(client, data(suppliers[0], raws[0]))
    assert client.post(URL, json=data(suppliers[1], raws[1], price)).status_code == 422
    assert client.put(URL+"/"+row["id"], json=edit(row, unit_price=price)).status_code == 422
    assert client.get(URL).json() == [row]


def test_active_pair_uniqueness_reassignment_and_reactivation(matrix):
    client, _, _, suppliers, raws, _ = matrix
    a = post(client, data(suppliers[0], raws[0]))
    b = post(client, data(suppliers[1], raws[1], "-5"))
    assert client.post(URL, json=data(suppliers[0], raws[0])).status_code == 409
    assert client.put(URL+"/"+b["id"], json=edit(b, supplier_id=suppliers[0]["id"], item_id=raws[0]["id"])).status_code == 409
    assert client.get(URL+"/"+b["id"]).json() == b
    moved = client.put(URL+"/"+b["id"], json=edit(b, supplier_id=suppliers[0]["id"])).json()
    assert moved["supplier_id"] == suppliers[0]["id"] and moved["item_id"] == raws[1]["id"]
    inactive = post(client, data(suppliers[0], raws[0], "3", False))
    assert client.put(URL+"/"+inactive["id"], json=edit(inactive, is_active=True)).status_code == 409
    assert client.delete(URL+"/"+a["id"], params={"row_version": 1}).status_code == 204
    assert client.put(URL+"/"+inactive["id"], json=edit(inactive, is_active=True)).status_code == 200


def test_invalid_references_system_override_and_finished_rejection(matrix):
    client, _, _, suppliers, raws, finished = matrix
    row = post(client, data(suppliers[0], raws[0]))
    for patch in [{"supplier_id": str(uuid4())}, {"item_id": str(uuid4())}, {"item_id": finished["id"]},
                  {"id": str(uuid4())}, {"created_by": str(uuid4())}, {"created_at": "2000-01-01T00:00:00Z"},
                  {"updated_at": "2000-01-01T00:00:00Z"}, {"is_active": "true"}]:
        assert client.post(URL, json={**data(suppliers[1], raws[1]), **patch}).status_code == 422
        assert client.put(URL+"/"+row["id"], json={**edit(row), **patch}).status_code == 422
    assert client.post(URL, json={**data(suppliers[1], raws[1]), "row_version": 5}).status_code == 422
    assert client.get(URL).json() == [row]
    assert client.get(URL+"/"+str(uuid4())).status_code == 404
    assert client.put(URL+"/"+str(uuid4()), json=edit(row)).status_code == 404
    assert client.delete(URL+"/"+str(uuid4()), params={"row_version": 1}).status_code == 404


def test_actual_database_constraints_master_protection_and_rollback(matrix):
    client, db, _, suppliers, raws, finished = matrix
    row = post(client, data(suppliers[0], raws[0]))
    with db.engine.connect() as a, db.engine.connect() as b:
        assert a.connection.driver_connection is not b.connection.driver_connection
        assert a.exec_driver_sql("PRAGMA foreign_keys").scalar() == b.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    statements = [
        ("UPDATE supplier_offer SET supplier_id=?", (str(uuid4()),)),
        ("UPDATE supplier_offer SET item_id=?", (str(uuid4()),)),
        ("UPDATE supplier_offer SET item_id=?", (finished["id"],)),
        ("UPDATE supplier_offer SET created_by=?", (str(uuid4()),)),
        ("DELETE FROM supplier WHERE id=?", (suppliers[0]["id"],)),
        ("DELETE FROM item WHERE id=?", (raws[0]["id"],)),
        ("UPDATE item SET item_kind='FINISHED' WHERE id=?", (raws[0]["id"],)),
    ]
    for value in ["01", "1.0", "-0", "1e2", "NaN", "1..2", ".1", "1.", "--1", "", "00.1"]:
        statements.append(("UPDATE supplier_offer SET unit_price=?", (value,)))
    for sql, parameters in statements:
        with pytest.raises(IntegrityError):
            with db.write() as session:
                session.connection().exec_driver_sql(sql, parameters)
        assert client.get(URL+"/"+row["id"]).json() == row
    # Direct SQL cannot bypass either the insert trigger or partial unique index.
    for sid, iid in [(suppliers[0]["id"], raws[0]["id"]), (suppliers[1]["id"], finished["id"])]:
        with pytest.raises(IntegrityError):
            with db.write() as session:
                session.connection().exec_driver_sql("""INSERT INTO supplier_offer
                    SELECT ?,?,?,unit_price,is_active,created_at,created_by,updated_at,row_version FROM supplier_offer LIMIT 1""",
                    (str(uuid4()), sid, iid))
    for resource, master in [("suppliers", suppliers[0]), ("raw-materials", raws[0])]:
        result = client.delete("/api/"+resource+"/"+master["id"], params={"row_version": master["row_version"]})
        assert result.status_code == 409 and "引用" in result.json()["message"]
        assert client.get("/api/"+resource+"/"+master["id"]).json() == master
    with db.write() as session:
        session.connection().exec_driver_sql("""CREATE TRIGGER forced_offer_failure AFTER UPDATE ON supplier_offer
            WHEN NEW.unit_price='999' BEGIN SELECT RAISE(ABORT,'verification rollback'); END""")
    assert client.put(URL+"/"+row["id"], json=edit(row, unit_price="999", supplier_id=suppliers[1]["id"], item_id=raws[1]["id"])).status_code == 409
    assert client.get(URL+"/"+row["id"]).json() == row
    with db.write() as session:
        session.connection().exec_driver_sql("UPDATE supplier_offer SET is_active=0")
    assert client.delete("/api/suppliers/"+suppliers[0]["id"], params={"row_version": 1}).status_code == 409


def test_simultaneous_duplicate_create_stale_update_and_delete(matrix):
    client, _, _, suppliers, raws, _ = matrix
    body = data(suppliers[0], raws[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: client.post(URL, json=body).status_code, range(2))) == [201, 409]
    row = client.get(URL).json()[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda price: client.put(URL+"/"+row["id"], json=edit(row, unit_price=price)).status_code, ["2", "3"])) == [200, 409]
    saved = client.get(URL+"/"+row["id"]).json()
    assert saved["row_version"] == 2
    assert client.delete(URL+"/"+row["id"], params={"row_version": 1}).status_code == 409
    assert client.get(URL+"/"+row["id"]).json() == saved


def snapshot(path):
    with sqlite3.connect(path) as connection:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'")]
        return {"rows": {t: connection.execute('SELECT * FROM "'+t+'" ORDER BY 1').fetchall() for t in tables},
                "objects": connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()}


def old_0002(path):
    legacy_database(path)
    engine = make_engine(path)
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    with engine.connect().execution_options(erp_write=True) as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0002_business_masters")
    engine.dispose()


def test_additive_0002_upgrade_and_failed_0003_rollback(tmp_path):
    path = tmp_path / "previous.sqlite"
    old_0002(path)
    before = snapshot(path)
    migrate(path)
    migrate(path)
    after = snapshot(path)
    assert all(after["rows"][table] == rows for table, rows in before["rows"].items())
    assert all(obj in after["objects"] for obj in before["objects"])
    assert after["rows"]["supplier_offer"] == []
    with sqlite3.connect(path) as connection:
        from backend.app.stock_ledger import register_exact_sql
        register_exact_sql(connection)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0020_inventory_transfers"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    blocked = tmp_path / "blocked.sqlite"
    old_0002(blocked)
    with sqlite3.connect(blocked) as connection:
        connection.execute("CREATE TABLE supplier_offer (legacy_marker TEXT)")
    original = snapshot(blocked)
    with pytest.raises(OperationalError):
        migrate(blocked)
    assert snapshot(blocked) == original
    with sqlite3.connect(blocked) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0002_business_masters"
