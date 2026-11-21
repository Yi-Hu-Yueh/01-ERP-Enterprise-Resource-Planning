"""Independent additive-migration/DDL rollback checks using synthetic local data."""
from contextlib import closing
from pathlib import Path
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import event

from backend.app import db
from backend.app.stock_ledger import register_exact_sql
from backend.tests.transfer_migration_assertions import NEW_TABLES, assert_transfer_upgrade_objects


def snapshot(path):
    with closing(sqlite3.connect(path)) as con:
        register_exact_sql(con)
        objects = con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
        tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return dict(objects=objects, rows={table: con.execute('SELECT rowid,* FROM "' + table + '" ORDER BY rowid').fetchall() for table in tables})


def copy_database(source, target):
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as destination:
        original.backup(destination)


@pytest.fixture(scope="module")
def legacy_database(tmp_path_factory):
    path = tmp_path_factory.mktemp("transfer-legacy") / "legacy.sqlite"
    engine = db.make_engine(path)
    config = Config(str(db.ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(db.ROOT / "backend/migrations"))
    with engine.connect().execution_options(erp_write=True) as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0019_inventory_adjustments")
    engine.dispose()
    database = db.Database(path)
    database.seed_operator()
    from backend.app.models import Unit, Warehouse, Category, Item
    from backend.app.receiving_models import StockPosition
    from backend.app.adjustment_models import InventoryAdjustment
    with database.write() as session:
        unit = Unit(code="SYNTHETIC-UNIT", name="件", created_by=db.DEMO_ACTOR_ID)
        warehouse = Warehouse(code="SYNTHETIC-SOURCE", name="隔離來源倉", mrp_included=False, created_by=db.DEMO_ACTOR_ID)
        category = Category(code="SYNTHETIC-CATEGORY", name="隔離分類", created_by=db.DEMO_ACTOR_ID)
        session.add_all([unit, warehouse, category]); session.flush()
        item = Item(code="SYNTHETIC-RAW", name="隔離原料", item_kind="RAW", category_id=category.id,
            base_unit_id=unit.id, lot_tracking=True, created_by=db.DEMO_ACTOR_ID)
        session.add(item); session.flush()
        # An untracked position is sufficient to prove legacy row identity preservation.
        item.lot_tracking = False; session.flush()
        position = StockPosition(item_id=item.id, warehouse_id=warehouse.id, lot_id=None, created_by=db.DEMO_ACTOR_ID)
        session.add(position); session.flush()
        session.add(InventoryAdjustment(document_no="SYNTHETIC-OLD-DRAFT", business_date="2026-11-21", position_id=position.id,
            item_id=item.id, item_kind="RAW", unit_id=unit.id, warehouse_id=warehouse.id,
            lot_id=None, direction="DECREASE", quantity="0.25", reason="隔離舊資料保留驗證", created_by=db.DEMO_ACTOR_ID))
    database.engine.dispose()
    return path


def test_transfer_upgrade_preserves_every_old_row_rowid_and_original_object(legacy_database, tmp_path):
    path = tmp_path / "upgrade.sqlite"; copy_database(legacy_database, path)
    before = snapshot(path); db.migrate(path); after = snapshot(path)
    assert_transfer_upgrade_objects(before, after)
    assert all(after["rows"][table] == [] for table in NEW_TABLES)


def test_transfer_fresh_schema_exact_types_scoping_and_integrity(tmp_path):
    path = tmp_path / "fresh.sqlite"; db.migrate(path)
    engine = db.make_engine(path)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0020_inventory_transfers"
        tables = list(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
        assert len(tables) == 63
        assert sum(len(list(connection.exec_driver_sql('PRAGMA table_info("' + table + '")'))) for table in tables) == 752
        for table, column in [("inventory_transfer", "quantity"), ("inventory_transfer_entry", "qty_base_delta")]:
            assert next(row[2] for row in connection.exec_driver_sql('PRAGMA table_info("' + table + '")') if row[1] == column) == "TEXT"
        lot_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(inventory_lot)")}
        assert "warehouse_id" not in lot_columns
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert list(connection.exec_driver_sql("PRAGMA foreign_key_check")) == []
    engine.dispose()


@pytest.mark.parametrize("stage", ["new_tables", "ledger_replaced", "position_guard_replaced"])
def test_transfer_ddl_failures_restore_original_schema_and_all_rows(legacy_database, tmp_path, monkeypatch, stage):
    path = tmp_path / (stage + ".sqlite"); copy_database(legacy_database, path)
    before = snapshot(path); real_engine = db.make_engine; fired = []
    def fault_engine(database_path):
        engine = real_engine(database_path)
        def after(connection, cursor, statement, parameters, context, many):
            normalized = " ".join(statement.split()).lower()
            matched = ((stage == "new_tables" and normalized.startswith("create table inventory_transfer_entry"))
                or (stage == "ledger_replaced" and normalized.startswith("create view physical_stock_ledger"))
                or (stage == "position_guard_replaced" and normalized.startswith("create trigger stock_position_identity")))
            if matched:
                fired.append(statement)
                raise RuntimeError("injected transfer DDL failure: " + stage)
        event.listen(engine, "after_cursor_execute", after)
        return engine
    monkeypatch.setattr(db, "make_engine", fault_engine)
    with pytest.raises(RuntimeError, match="injected transfer DDL failure"):
        db.migrate(path)
    assert len(fired) == 1
    assert snapshot(path) == before


def test_extended_position_guard_keeps_untracked_raw_allowed_and_finished_denied(legacy_database, tmp_path):
    path = tmp_path / "guard.sqlite"; copy_database(legacy_database, path); db.migrate(path)
    engine = db.make_engine(path)
    with engine.connect().execution_options(erp_write=True) as connection:
        # Ordinary connections have no posting grant. Existing raw identity behavior survives.
        source = connection.exec_driver_sql("SELECT item_id,warehouse_id,created_at,created_by FROM stock_position LIMIT 1").one()
        warehouse = str(uuid4())
        connection.exec_driver_sql("INSERT INTO warehouse SELECT ?, 'SYNTHETIC-DESTINATION', name,is_active,created_at,created_by,updated_at,row_version,address,mrp_included FROM warehouse LIMIT 1", (warehouse,))
        connection.exec_driver_sql("INSERT INTO stock_position(id,item_id,warehouse_id,lot_id,created_at,created_by) VALUES(?,?,?,NULL,?,?)", (str(uuid4()), source[0], warehouse, source[2], source[3]))
        # A FINISHED item is not sufficient authority to create a manufactured destination.
        columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(item)")]
        original = dict(zip(columns, connection.exec_driver_sql("SELECT * FROM item LIMIT 1").one()))
        original.update(id=str(uuid4()), code="SYNTHETIC-FINISHED", item_kind="FINISHED")
        connection.exec_driver_sql("INSERT INTO item (" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(original[column] for column in columns))
        with pytest.raises(Exception, match="finished-goods grant denied"):
            connection.exec_driver_sql("INSERT INTO stock_position(id,item_id,warehouse_id,lot_id,created_at,created_by) VALUES(?,?,?,NULL,?,?)", (str(uuid4()), original["id"], warehouse, source[2], source[3]))
        connection.rollback()
    engine.dispose()
