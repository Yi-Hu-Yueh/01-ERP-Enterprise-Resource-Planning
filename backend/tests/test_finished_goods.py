"""Finished-goods API and physical shared-item integrity; isolated databases only."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from backend.app.db import DEMO_ACTOR_ID, migrate
from backend.app.main import create_app
from backend.app.models import Item
from backend.tests.test_foundation import setup, resources, payload


def create(client, path, data):
    response = client.post("/api/" + path, json=data)
    assert response.status_code == 201, response.text
    return response.json()


def test_finished_crud_uses_shared_table_and_isolated_search(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    raw = create(client, "raw-materials", payload(cat, unit, "RAW-KEEP"))
    data = {**payload(cat, unit, "FG-A"), "name": "筆記本", "specification": "A5"}
    fg = create(client, "finished-goods", data)
    second = create(client, "finished-goods", {**data, "code": "FG-B"})
    assert fg["item_kind"] == second["item_kind"] == "FINISHED"
    assert len({fg["id"], second["id"], raw["id"]}) == 3
    assert client.get("/api/raw-materials").json() == [raw]
    assert {r["id"] for r in client.get("/api/finished-goods").json()} == {fg["id"], second["id"]}
    assert client.get("/api/finished-goods?q=RAW-KEEP").json() == []
    assert client.get("/api/raw-materials?q=FG-A").json() == []
    assert client.get("/api/raw-materials?item_kind=FINISHED").json() == [raw]
    assert len(client.get("/api/finished-goods?q=筆記").json()) == 2
    assert client.get("/api/finished-goods?q=%").json() == []
    cat2 = client.post("/api/categories", json={"code": "BOOK", "name": "簿本"}).json()
    unit2 = client.post("/api/units", json={"code": "BOOK", "name": "本"}).json()
    edit = {**payload(cat2, unit2, "FG-A-REVISED"), "name": "精裝筆記本",
            "specification": "B5", "lot_tracking": False, "is_active": False, "row_version": 1}
    updated = client.put("/api/finished-goods/" + fg["id"], json=edit).json()
    assert updated["item_kind"] == "FINISHED" and updated["row_version"] == 2
    assert all(updated[k] == fg[k] for k in ["id", "created_by", "created_at"])
    assert updated["updated_at"] != fg["updated_at"]
    for k, v in edit.items():
        if k != "row_version":
            assert updated[k] == v
    with db.read() as session:
        rows = session.scalars(select(Item)).all()
        assert {(r.id, r.item_kind) for r in rows} == {
            (raw["id"], "RAW"), (fg["id"], "FINISHED"), (second["id"], "FINISHED")}
    assert client.delete(f"/api/finished-goods/{second['id']}?row_version=1").status_code == 204
    assert client.get("/api/finished-goods/" + second["id"]).status_code == 404
    assert client.get("/api/raw-materials").json() == [raw]


@pytest.mark.parametrize("target,other", [("raw-materials", "finished-goods"), ("finished-goods", "raw-materials")])
def test_cross_kind_requests_and_system_fields_are_rejected(setup, target, other):
    client, _, _ = setup
    cat, unit = resources(client)
    own = create(client, target, payload(cat, unit, "OWN"))
    foreign = create(client, other, payload(cat, unit, "OTHER"))
    before = {path: client.get("/api/" + path).json() for path in [target, other]}
    edit = {**payload(cat, unit, "MUTATED"), "row_version": 1}
    assert client.get(f"/api/{target}/{foreign['id']}").status_code == 404
    assert client.put(f"/api/{target}/{foreign['id']}", json=edit).status_code == 404
    assert client.delete(f"/api/{target}/{foreign['id']}?row_version=1").status_code == 404
    for field, value in {"item_kind": "RAW" if target == "finished-goods" else "FINISHED",
                         "id": str(uuid4()), "created_by": str(uuid4()),
                         "created_at": "2000-01-01T00:00:00Z", "updated_at": "2000-01-01T00:00:00Z"}.items():
        assert client.post("/api/" + target, json={**payload(cat, unit, "NEW"), field: value}).status_code == 422
        assert client.put(f"/api/{target}/{own['id']}", json={**edit, field: value}).status_code == 422
    assert client.post("/api/" + target, json={**payload(cat, unit, "NEW"), "row_version": 77}).status_code == 422
    assert {path: client.get("/api/" + path).json() for path in before} == before


def test_global_uniqueness_on_create_update_and_direct_database_write(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    raw = create(client, "raw-materials", payload(cat, unit, "RAW"))
    fg = create(client, "finished-goods", payload(cat, unit, "FG"))
    before = client.get("/api/finished-goods").json()
    for code in ["FG", "RAW"]:
        assert client.post("/api/finished-goods", json=payload(cat, unit, code)).status_code == 409
    assert client.post("/api/raw-materials", json=payload(cat, unit, "FG")).status_code == 409
    for path, row, code in [("finished-goods", fg, "RAW"), ("raw-materials", raw, "FG")]:
        assert client.put(f"/api/{path}/{row['id']}", json={**payload(cat, unit, code), "row_version": 1}).status_code == 409
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.add(Item(**payload(cat, unit, "RAW"), item_kind="FINISHED", created_by=DEMO_ACTOR_ID))
            session.flush()
    assert client.get("/api/finished-goods").json() == before
    assert client.get("/api/raw-materials").json() == [raw]


def test_finished_invalid_foreign_keys_and_reference_restrictions(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    data = payload(cat, unit, "FG")
    row = create(client, "finished-goods", data)
    for field in ["category_id", "base_unit_id"]:
        invalid = {**data, field: str(uuid4())}
        assert client.post("/api/finished-goods", json={**invalid, "code": "BAD"}).status_code == 422
        assert client.put("/api/finished-goods/" + row["id"], json={**invalid, "row_version": 1}).status_code == 422
    for path, support in [("categories", cat), ("units", unit)]:
        assert client.delete(f"/api/{path}/{support['id']}?row_version=1").status_code == 409
    with db.write() as session:
        connection = session.connection()
        # Only a test FK, never an application business table.
        connection.exec_driver_sql("CREATE TABLE reference_probe (item_id TEXT NOT NULL REFERENCES item(id) ON DELETE RESTRICT)")
        connection.exec_driver_sql("INSERT INTO reference_probe VALUES (?)", (row["id"],))
    assert client.delete(f"/api/finished-goods/{row['id']}?row_version=1").status_code == 409
    assert client.put("/api/finished-goods/" + row["id"], json={**data, "code": "CHANGED", "row_version": 1}).status_code == 409
    unit2 = client.post("/api/units", json={"code": "BOX", "name": "盒"}).json()
    assert client.put("/api/finished-goods/" + row["id"], json={**data, "base_unit_id": unit2["id"], "row_version": 1}).status_code == 409
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.connection().exec_driver_sql("DELETE FROM item WHERE id=?", (row["id"],))
    assert client.get("/api/finished-goods").json() == [row]
    with db.write() as session:
        session.connection().exec_driver_sql("DROP TABLE reference_probe")
    assert client.delete(f"/api/finished-goods/{row['id']}?row_version=1").status_code == 204
    assert set(inspect(db.engine).get_table_names()) == {"actor", "unit", "item_category", "item", "alembic_version", "currency", "customer", "supplier", "warehouse", "work_center", "supplier_offer", "purchase_order", "purchase_order_line", "purchase_create_receipt", "inventory_lot", "stock_document", "stock_document_line", "stock_position", "stock_posting", "stock_entry", "stock_create_receipt", "supplier_return", "supplier_return_line", "supplier_return_posting", "supplier_return_entry", "bom", "bom_component", "operation_definition", "routing", "routing_step", "work_order", "material_issue", "material_issue_line", "material_issue_posting", "material_issue_entry", "material_return", "material_return_line", "material_return_posting", "material_return_entry", "production_completion", "finished_goods_receipt", "finished_goods_posting", "finished_goods_entry", "sales_order", "sales_order_line", "sales_create_receipt", "sales_shipment", "sales_shipment_posting", "sales_shipment_entry", "customer_return", "customer_return_finalization", "customer_return_entry", "stocktake", "stocktake_line", "stocktake_exception", "stocktake_posting", "stocktake_entry", "inventory_adjustment", "inventory_adjustment_posting", "inventory_adjustment_entry", "inventory_transfer", "inventory_transfer_posting", "inventory_transfer_entry"}


def test_finished_concurrency_and_failed_write_rollback(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    data = payload(cat, unit, "FG")
    row = create(client, "finished-goods", data)
    def update(name):
        return client.put("/api/finished-goods/" + row["id"],
                          json={**data, "name": name, "row_version": 1}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["第一個儲存", "第二個儲存"])) == [200, 409]
    current = client.get("/api/finished-goods/" + row["id"]).json()
    assert current["row_version"] == 2
    assert client.delete(f"/api/finished-goods/{row['id']}?row_version=1").status_code == 409
    with db.write() as session:
        session.connection().exec_driver_sql("""
          CREATE TRIGGER forced_finished_failure AFTER UPDATE ON item
          WHEN NEW.name='FORCE_FAIL' BEGIN SELECT RAISE(ABORT, 'verification failure'); END
        """)
    assert client.put("/api/finished-goods/" + row["id"], json={
        **data, "name": "FORCE_FAIL", "specification": "不可部分留下", "row_version": 2}).status_code == 409
    assert client.get("/api/finished-goods/" + row["id"]).json() == current
    assert client.post("/api/finished-goods", json={**data, "code": "BAD-BOOL", "lot_tracking": "false"}).status_code == 422
    assert client.get("/api/finished-goods").json() == [current]


def test_existing_raw_database_gains_finished_api_without_schema_change(tmp_path):
    path = tmp_path / "preexisting-raw.sqlite"
    with TestClient(create_app(path)) as client:
        cat, unit = resources(client)
        raw = create(client, "raw-materials", payload(cat, unit, "RAW-EXISTING"))
        with client.app.state.db.read() as session:
            schema_before = list(session.connection().exec_driver_sql("SELECT type,name,sql FROM sqlite_master ORDER BY type,name"))
    migrate(path)
    with TestClient(create_app(path)) as client:
        fg = create(client, "finished-goods", payload(cat, unit, "FG-NEW"))
        assert client.get("/api/raw-materials").json() == [raw]
    migrate(path)
    with TestClient(create_app(path)) as client:
        assert client.get("/api/raw-materials/" + raw["id"]).json() == raw
        assert client.get("/api/finished-goods/" + fg["id"]).json() == fg
        with client.app.state.db.read() as session:
            connection = session.connection()
            assert list(connection.exec_driver_sql("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")) == schema_before
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []
