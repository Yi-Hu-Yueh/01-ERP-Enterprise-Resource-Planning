"""Integrated four-master API, constraints, concurrency and migration acceptance."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from backend.app.db import ROOT, DEMO_ACTOR_ID, migrate
from backend.app.main import create_app
from backend.app.models import Currency, Customer, Supplier, Warehouse, WorkCenter
from backend.tests.test_foundation import setup, resources, payload

CASES = {
    "customers": (Customer, {"tax_identifier": "TEST-TAX", "contact_name": "林窗口", "phone": "+886 (02) 001-002",
                           "email": "buyer@example.com", "payment_terms": "月底討論", "billing_address": "帳單地址", "default_ship_address": "送貨地址"}),
    "suppliers": (Supplier, {"tax_identifier": "TEST-TAX", "contact_name": "林窗口", "phone": "+886 (02) 001-002",
                           "email": "seller@example.com", "payment_terms": "月底討論", "address": "供應地址"}),
    "warehouses": (Warehouse, {"address": "北區位置", "mrp_included": True}),
    "work-centers": (WorkCenter, {"description": "裁切區說明"}),
}


def currency(client, code="CUR"):
    response = client.post("/api/currencies", json={"code": code, "name": "測試幣別"})
    assert response.status_code == 201, response.text
    assert response.json()["rounding_scale"] is None
    assert response.json()["rounding_mode"] is None
    return response.json()


def data_for(client, resource, code="A"):
    result = {"code": code, "name": "主檔測試", "is_active": True, **CASES[resource][1]}
    if resource in {"customers", "suppliers"}:
        result["currency_id"] = currency(client)["id"]
    return result


@pytest.mark.parametrize("resource", CASES)
def test_full_crud_search_fields_audit_and_repeatable_restart(setup, resource):
    client, db, path = setup
    data = data_for(client, resource)
    url = "/api/" + resource
    first = client.post(url, json=data)
    assert first.status_code == 201, first.text
    row = first.json()
    other = client.post(url, json={**data, "code": "B"})
    assert other.status_code == 201
    assert len(client.get(url).json()) == 2
    assert client.get(url + "?q=A").json()[0]["id"] == row["id"]
    assert client.get(url + "?q=%").json() == []
    search = {"customers": "001-002", "suppliers": "林窗口", "warehouses": "北區", "work-centers": "裁切區"}[resource]
    assert len(client.get(url, params={"q": search}).json()) == 2
    edit = {**data, "code": "A-EDIT", "name": "修訂主檔", "is_active": False, "row_version": 1}
    for field in CASES[resource][1]:
        edit[field] = False if field == "mrp_included" else ("updated@example.com" if field == "email" else "修訂 " + data[field])
    if "currency_id" in data:
        edit["currency_id"] = currency(client, "ALT")["id"]
    response = client.put(url + "/" + row["id"], json=edit)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert all(updated[key] == row[key] for key in ["id", "created_at", "created_by"])
    assert updated["created_by"] == DEMO_ACTOR_ID and updated["row_version"] == 2
    assert updated["updated_at"] != row["updated_at"]
    assert all(updated[key] == value for key, value in edit.items() if key != "row_version")
    with db.read() as session:
        assert session.get(CASES[resource][0], row["id"]).name == "修訂主檔"
        assert session.connection().exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(session.connection().exec_driver_sql("PRAGMA foreign_key_check")) == []
    assert client.delete(url + "/" + other.json()["id"] + "?row_version=1").status_code == 204
    migrate(path)
    with TestClient(create_app(path)) as reopened:
        assert reopened.get(url + "/" + row["id"]).json() == updated
        assert reopened.get(url).json() == [updated]


@pytest.mark.parametrize("resource", CASES)
def test_invalid_fields_system_overrides_and_failures_leave_no_changes(setup, resource):
    client, _, _ = setup
    url = "/api/" + resource
    data = data_for(client, resource)
    row = client.post(url, json=data).json()
    for patch in [{"code": " "}, {"name": ""}, {"is_active": "true"},
                  {"id": str(uuid4())}, {"created_by": str(uuid4())},
                  {"updated_at": "2000-01-01T00:00:00Z"}, {"created_at": "2000-01-01T00:00:00Z"}]:
        assert client.post(url, json={**data, "code": "NEW", **patch}).status_code == 422
        assert client.put(url + "/" + row["id"], json={**data, "row_version": 1, **patch}).status_code == 422
    assert client.post(url, json={**data, "code": "NEW", "row_version": 77}).status_code == 422
    if "currency_id" in data:
        for patch in [{"currency_id": str(uuid4())}, *[{"email": value} for value in ["invalid-email", "x@@example.com", "x@", "@example.com", "x\ny@example.com"]]]:
            assert client.post(url, json={**data, "code": "BAD", **patch}).status_code == 422
            assert client.put(url + "/" + row["id"], json={**data, "row_version": 1, **patch}).status_code == 422
    if resource == "warehouses":
        assert client.post(url, json={**data, "code": "BAD", "mrp_included": "false"}).status_code == 422
    assert client.get(url).json() == [row]


@pytest.mark.parametrize("resource", CASES)
def test_unique_codes_real_fk_constraints_and_atomic_rollback(setup, resource):
    client, db, _ = setup
    url = "/api/" + resource
    data = data_for(client, resource)
    row = client.post(url, json=data).json()
    second = client.post(url, json={**data, "code": "B"}).json()
    assert client.post(url, json=data).status_code == 409
    assert client.put(url + "/" + second["id"], json={**data, "row_version": 1}).status_code == 409
    model = CASES[resource][0]
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.add(model(**data, created_by=DEMO_ACTOR_ID))
            session.flush()
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.add(model(**{**data, "code": "INVALID-ACTOR"}, created_by=str(uuid4())))
            session.flush()
    if "currency_id" in data:
        with pytest.raises(IntegrityError):
            with db.write() as session:
                session.add(model(**{**data, "code": "INVALID-CURRENCY", "currency_id": str(uuid4())}, created_by=DEMO_ACTOR_ID))
                session.flush()
    with db.write() as session:
        session.connection().exec_driver_sql(f"""CREATE TRIGGER forced_failure AFTER UPDATE ON {model.__tablename__}
            WHEN NEW.name='FORCE_FAIL' BEGIN SELECT RAISE(ABORT, 'verification rollback'); END""")
    assert client.put(url + "/" + row["id"], json={**data, "name": "FORCE_FAIL", "row_version": 1}).status_code == 409
    assert client.get(url).json() == [row, second]


@pytest.mark.parametrize("resource", CASES)
def test_referenced_delete_protection_and_eligible_delete(setup, resource):
    client, db, _ = setup
    url = "/api/" + resource
    data = data_for(client, resource)
    row = client.post(url, json=data).json()
    table = CASES[resource][0].__tablename__
    with db.write() as session:
        connection = session.connection()
        connection.exec_driver_sql(f"CREATE TABLE reference_probe (record_id TEXT REFERENCES {table}(id) ON DELETE RESTRICT)")
        connection.exec_driver_sql("INSERT INTO reference_probe VALUES (?)", (row["id"],))
    blocked = client.delete(url + "/" + row["id"] + "?row_version=1")
    assert blocked.status_code == 409 and "引用" in blocked.json()["message"]
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.connection().exec_driver_sql(f"DELETE FROM {table} WHERE id=?", (row["id"],))
    assert client.get(url + "/" + row["id"]).json() == row
    with db.write() as session:
        session.connection().exec_driver_sql("DROP TABLE reference_probe")
    assert client.delete(url + "/" + row["id"] + "?row_version=1").status_code == 204
    assert client.get(url + "/" + row["id"]).status_code == 404


@pytest.mark.parametrize("resource", CASES)
def test_concurrent_and_stale_versions(setup, resource):
    client, _, _ = setup
    url = "/api/" + resource
    data = data_for(client, resource)
    row = client.post(url, json=data).json()
    def update(name):
        return client.put(url + "/" + row["id"], json={**data, "name": name, "row_version": 1}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["第一個", "第二個"])) == [200, 409]
    saved = client.get(url + "/" + row["id"]).json()
    assert saved["row_version"] == 2
    assert client.delete(url + "/" + row["id"] + "?row_version=1").status_code == 409
    assert client.get(url + "/" + row["id"]).json() == saved


def test_business_identities_are_independent_and_currency_is_reference_only(setup):
    client, db, _ = setup
    shared_currency = currency(client)
    records = {}
    for resource in CASES:
        data = {"code": "SAME-CODE", "name": "同名", "is_active": True, **CASES[resource][1]}
        if resource in {"customers", "suppliers"}:
            data["currency_id"] = shared_currency["id"]
        response = client.post("/api/" + resource, json=data)
        assert response.status_code == 201
        records[resource] = response.json()
    assert len({row["id"] for row in records.values()}) == 4
    for resource, row in records.items():
        other = next(other for key, other in records.items() if key != resource)
        assert client.get("/api/" + resource + "/" + other["id"]).status_code == 404
        data = {key: value for key, value in row.items() if key not in {"id", "created_at", "updated_at", "created_by"}}
        assert client.put("/api/" + resource + "/" + other["id"], json=data).status_code == 404
        assert client.delete("/api/" + resource + "/" + other["id"] + "?row_version=1").status_code == 404
    assert client.delete("/api/currencies/" + shared_currency["id"] + "?row_version=1").status_code == 409
    assert client.post("/api/currencies", json={"code": "NO", "name": "不得推定", "rounding_scale": 2}).status_code == 422
    with db.engine.connect() as a, db.engine.connect() as b:
        assert a.exec_driver_sql("PRAGMA foreign_keys").scalar() == b.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    for table in ["currency", "customer", "supplier", "warehouse", "work_center"]:
        indexes = inspect(db.engine).get_indexes(table)
        assert any(index["column_names"] == ["created_by"] for index in indexes)
    assert client.post("/api/currencies", json={"code": "CUR", "name": "重複"}).status_code == 409


def legacy_database(path):
    from backend.app.db import Database, make_engine
    from backend.app.models import Category, Unit, Item
    engine = make_engine(path)
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    with engine.connect().execution_options(erp_write=True) as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001_master")
    engine.dispose()
    db = Database(path)
    db.seed_operator()
    with db.write() as session:
        category = Category(code="OLD-CAT", name="原有分類", created_by=DEMO_ACTOR_ID)
        unit = Unit(code="OLD-UNIT", name="原有單位", created_by=DEMO_ACTOR_ID)
        session.add_all([category, unit])
        session.flush()
        session.add_all([Item(code=kind, name="原有"+kind, item_kind=kind, category_id=category.id,
                             base_unit_id=unit.id, lot_tracking=True, is_active=True, created_by=DEMO_ACTOR_ID)
                         for kind in ["RAW", "FINISHED"]])
    db.engine.dispose()


def legacy_rows(path):
    import sqlite3
    with sqlite3.connect(path) as connection:
        return {table: connection.execute("SELECT * FROM " + table + " ORDER BY id").fetchall()
                for table in ["actor", "unit", "item_category", "item"]}


def test_0001_to_0002_preserves_old_raw_finished_rows_and_chain(tmp_path):
    path = tmp_path / "legacy.sqlite"
    legacy_database(path)
    before = legacy_rows(path)
    migrate(path)
    migrate(path)
    assert legacy_rows(path) == before
    with TestClient(create_app(path)) as client:
        assert client.get("/api/raw-materials").json()[0]["code"] == "RAW"
        assert client.get("/api/finished-goods").json()[0]["code"] == "FINISHED"
        for resource in CASES:
            assert client.get("/api/" + resource).json() == []
        with client.app.state.db.read() as session:
            connection = session.connection()
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0020_inventory_transfers"
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []


def test_failed_schema_upgrade_rolls_back_without_partial_new_tables(tmp_path):
    import sqlite3
    from sqlalchemy.exc import OperationalError
    path = tmp_path / "blocked-migration.sqlite"
    legacy_database(path)
    before = legacy_rows(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE customer (legacy_marker TEXT)")
    with pytest.raises(OperationalError):
        migrate(path)
    assert legacy_rows(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0001_master"
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='currency'").fetchone() is None
