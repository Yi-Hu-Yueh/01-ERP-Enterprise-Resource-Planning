"""API + actual SQLite/Alembic tests; every test owns an isolated database."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from backend.app.db import Database, DEMO_ACTOR_ID, migrate
from backend.app.main import create_app
from backend.app.models import Category, Item, Unit


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "isolated.sqlite"
    with TestClient(create_app(path)) as client:
        yield client, client.app.state.db, path


def resources(client):
    cat = client.post("/api/categories", json={"code": "PAPER", "name": "紙材"}).json()
    unit = client.post("/api/units", json={"code": "SHEET", "name": "張"}).json()
    return cat, unit


def payload(cat, unit, code="RM-A"):
    return {"code": code, "name": "紙張", "specification": "A4", "category_id": cat["id"],
            "base_unit_id": unit["id"], "lot_tracking": True, "is_active": True}


def test_only_authorized_schema_connections_and_stable_operator(setup):
    client, db, path = setup
    assert set(inspect(db.engine).get_table_names()) == {"actor", "unit", "item_category", "item", "alembic_version", "currency", "customer", "supplier", "warehouse", "work_center", "supplier_offer", "purchase_order", "purchase_order_line", "purchase_create_receipt", "inventory_lot", "stock_document", "stock_document_line", "stock_position", "stock_posting", "stock_entry", "stock_create_receipt", "supplier_return", "supplier_return_line", "supplier_return_posting", "supplier_return_entry", "bom", "bom_component", "operation_definition", "routing", "routing_step", "work_order", "material_issue", "material_issue_line", "material_issue_posting", "material_issue_entry", "material_return", "material_return_line", "material_return_posting", "material_return_entry", "production_completion", "finished_goods_receipt", "finished_goods_posting", "finished_goods_entry", "sales_order", "sales_order_line", "sales_create_receipt", "sales_shipment", "sales_shipment_posting", "sales_shipment_entry", "customer_return", "customer_return_finalization", "customer_return_entry", "stocktake", "stocktake_line", "stocktake_exception", "stocktake_posting", "stocktake_entry", "inventory_adjustment", "inventory_adjustment_posting", "inventory_adjustment_entry", "inventory_transfer", "inventory_transfer_posting", "inventory_transfer_entry"} | {"erp_department", "erp_employee", "approval_role", "approval_employee_role", "approval_definition", "approval_stage", "approval_slot", "approval_request", "approval_attempt", "approval_cycle", "approval_task", "approval_event", "approval_carried_decision", "approval_added_signer", "approval_delegation", "approval_operation_receipt"}
    # Simultaneously checked-out, genuinely separate physical connections.
    with db.engine.connect() as a, db.engine.connect() as b:
        assert a.connection.driver_connection is not b.connection.driver_connection
        assert a.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert b.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    assert client.get("/api/health").json()["journal_mode"] == "wal"
    cat, unit = resources(client)
    assert cat["created_by"] == unit["created_by"] == DEMO_ACTOR_ID
    assert unit["quantity_rule"] == "PENDING"


def test_create_read_search_update_stable_audit_and_no_numeric_policy(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    original = client.post("/api/raw-materials", json=payload(cat, unit)).json()
    second = client.post("/api/raw-materials", json=payload(cat, unit, "RM-B"))
    assert second.status_code == 201
    assert client.get("/api/raw-materials?q=RM-A").json()[0]["id"] == original["id"]
    assert len(client.get("/api/raw-materials?q=紙").json()) == 2
    assert client.get("/api/raw-materials?q=%").json() == []  # literal search
    cat2 = client.post("/api/categories", json={"code": "GLUE", "name": "膠材"}).json()
    unit2 = client.post("/api/units", json={"code": "KG", "name": "公斤"}).json()
    edit = payload(cat2, unit2)
    edit.update(name="修訂名稱", specification="新規格", lot_tracking=False, is_active=False, row_version=original["row_version"])
    updated = client.put("/api/raw-materials/" + original["id"], json=edit)
    assert updated.status_code == 200
    result = updated.json()
    assert result["id"] == original["id"]
    assert result["created_at"] == original["created_at"]
    assert result["created_by"] == original["created_by"]
    assert result["row_version"] == original["row_version"] + 1
    assert result["updated_at"] != original["updated_at"]
    assert result["base_unit_id"] == unit2["id"] and result["category_id"] == cat2["id"]
    assert not result["is_active"] and not result["lot_tracking"]
    with db.read() as session:
        assert session.get(Item, original["id"]).name == "修訂名稱"
    columns = {c["name"] for c in inspect(db.engine).get_columns("item")}
    assert columns.isdisjoint({"on_hand", "on_hand_qty", "quantity", "stock_balance"})


def test_duplicate_code_api_and_real_database_constraint(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    data = payload(cat, unit)
    assert client.post("/api/raw-materials", json=data).status_code == 201
    duplicate = client.post("/api/raw-materials", json=data)
    assert duplicate.status_code == 409 and "品號已存在" in duplicate.json()["message"]
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.add(Item(**data, item_kind="RAW", created_by=DEMO_ACTOR_ID))
            session.flush()
    assert len(client.get("/api/raw-materials").json()) == 1


def test_foreign_keys_api_database_and_system_fields(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    invalid = payload(cat, unit)
    invalid["category_id"] = str(uuid4())
    assert client.post("/api/raw-materials", json=invalid).status_code == 422
    invalid["category_id"] = cat["id"]
    invalid["base_unit_id"] = str(uuid4())
    assert client.post("/api/raw-materials", json=invalid).status_code == 422
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.add(Item(**invalid, item_kind="RAW", created_by=DEMO_ACTOR_ID))
            session.flush()
    valid = payload(cat, unit)
    for field, value in {"id": str(uuid4()), "created_by": str(uuid4()), "item_kind": "FINISHED", "row_version": 77}.items():
        assert client.post("/api/raw-materials", json={**valid, field: value}).status_code == 422
    assert client.get("/api/raw-materials").json() == []


def test_reference_protection_and_eligible_deletion(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    row = client.post("/api/raw-materials", json=payload(cat, unit)).json()
    for resource, record in [("categories", cat), ("units", unit)]:
        response = client.delete(f"/api/{resource}/{record['id']}?row_version={record['row_version']}")
        assert response.status_code == 409 and "引用" in response.json()["message"]
    # Verification-only incoming FK. Not part of the application schema or runtime DB.
    with db.write() as session:
        connection = session.connection()
        connection.exec_driver_sql("CREATE TABLE reference_probe (item_id TEXT NOT NULL REFERENCES item(id) ON DELETE RESTRICT)")
        connection.exec_driver_sql("INSERT INTO reference_probe VALUES (?)", (row["id"],))
    blocked = client.delete(f"/api/raw-materials/{row['id']}?row_version={row['row_version']}")
    assert blocked.status_code == 409 and "引用" in blocked.json()["message"]
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.connection().exec_driver_sql("DELETE FROM item WHERE id=?", (row["id"],))
    with db.write() as session:
        session.connection().exec_driver_sql("DROP TABLE reference_probe")
    assert client.delete(f"/api/raw-materials/{row['id']}?row_version={row['row_version']}").status_code == 204
    assert client.get("/api/raw-materials/" + row["id"]).status_code == 404
    assert client.delete(f"/api/categories/{cat['id']}?row_version=1").status_code == 204


def test_stale_update_and_delete_do_not_overwrite(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    data = payload(cat, unit)
    row = client.post("/api/raw-materials", json=data).json()
    edit = {**data, "name": "先儲存", "row_version": row["row_version"]}
    assert client.put("/api/raw-materials/" + row["id"], json=edit).status_code == 200
    stale = client.put("/api/raw-materials/" + row["id"], json={**edit, "name": "過期修改"})
    assert stale.status_code == 409
    assert client.delete(f"/api/raw-materials/{row['id']}?row_version=1").status_code == 409
    assert client.get("/api/raw-materials/" + row["id"]).json()["name"] == "先儲存"


def test_failing_write_rolls_back_all_values_and_version(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    data = payload(cat, unit)
    row = client.post("/api/raw-materials", json=data).json()
    with db.write() as session:
        session.connection().exec_driver_sql("""
          CREATE TRIGGER forced_failure AFTER UPDATE ON item
          WHEN NEW.name='FORCE_FAIL' BEGIN SELECT RAISE(ABORT, 'injected verification failure'); END
        """)
    failed = client.put("/api/raw-materials/" + row["id"],
                        json={**data, "name": "FORCE_FAIL", "specification": "不能留下", "row_version": 1})
    assert failed.status_code == 409
    assert client.get("/api/raw-materials/" + row["id"]).json() == row
    # Also prove a multi-record unit-of-work is atomic, not just request validation.
    with pytest.raises(IntegrityError):
        with db.write() as session:
            session.add(Category(code="ROLLBACK", name="不可部分保留", created_by=DEMO_ACTOR_ID))
            session.flush()
            session.add(Item(**{**data, "base_unit_id": str(uuid4())}, item_kind="RAW", created_by=DEMO_ACTOR_ID))
            session.flush()
    assert all(c["code"] != "ROLLBACK" for c in client.get("/api/categories").json())


def test_migration_and_application_restart_preserve_existing_records(tmp_path):
    path = tmp_path / "restart.sqlite"
    with TestClient(create_app(path)) as client:
        cat, unit = resources(client)
        row = client.post("/api/raw-materials", json=payload(cat, unit)).json()
    migrate(path)
    migrate(path)
    with TestClient(create_app(path)) as restarted:
        assert restarted.get("/api/raw-materials/" + row["id"]).json() == row
        assert len(restarted.get("/api/categories").json()) == 1
        assert restarted.get("/api/health").json()["foreign_keys"] == 1


def test_shared_item_identity_and_raw_view_filter(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    with db.write() as session:
        finished = Item(**payload(cat, unit, "FINISHED-TEST"), item_kind="FINISHED", created_by=DEMO_ACTOR_ID)
        session.add(finished)
        session.flush()
        finished_id = finished.id
    assert client.get("/api/raw-materials").json() == []
    assert client.get("/api/raw-materials/" + finished_id).status_code == 404
    assert client.post("/api/raw-materials", json=payload(cat, unit, "FINISHED-TEST")).status_code == 409


def test_concurrent_versions_commit_only_once(setup):
    client, db, _ = setup
    cat, unit = resources(client)
    data = payload(cat, unit)
    row = client.post("/api/raw-materials", json=data).json()
    def update(name):
        return client.put("/api/raw-materials/" + row["id"], json={**data, "name": name, "row_version": 1}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["並行A", "並行B"]))
    assert sorted(results) == [200, 409]
    assert client.get("/api/raw-materials/" + row["id"]).json()["row_version"] == 2

