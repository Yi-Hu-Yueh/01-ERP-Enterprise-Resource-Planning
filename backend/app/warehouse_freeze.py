"""Shared warehouse barrier for ordinary inventory writers, backed by SQL guards."""
import sqlite3
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ENTRY_TABLES = frozenset({"stock_entry", "supplier_return_entry", "material_issue_entry",
    "material_return_entry", "finished_goods_entry", "sales_shipment_entry", "customer_return_entry", "inventory_adjustment_entry", "inventory_transfer_entry"})
POSTING_ENTRIES = {"stock_posting": ("stock_entry", "posting_id"),
    "supplier_return_posting": ("supplier_return_entry", "posting_id"),
    "material_issue_posting": ("material_issue_entry", "posting_id"),
    "material_return_posting": ("material_return_entry", "posting_id"),
    "finished_goods_posting": ("finished_goods_entry", "posting_id"),
    "sales_shipment_posting": ("sales_shipment_entry", "posting_id"),
    "customer_return_finalization": ("customer_return_entry", "finalization_id"),
    "inventory_adjustment_posting": ("inventory_adjustment_entry", "posting_id"),
    "inventory_transfer_posting": ("inventory_transfer_entry", "posting_id")}
FROZEN_MESSAGE = "倉庫已凍結，請完成或明確取消整倉盤點後再執行庫存異動。"


def assert_warehouses_unfrozen(session, warehouse_ids, fail=None):
    """Reusable for every actual source/destination warehouse of a future writer."""
    from .stocktake_models import Stocktake
    ids = {str(value) for value in warehouse_ids if value is not None}
    if not ids:
        return
    if session.scalar(select(Stocktake.id).where(Stocktake.freeze_active.is_(True), Stocktake.warehouse_id.in_(ids))):
        if fail:
            fail(409, FROZEN_MESSAGE)
        raise IntegrityError("warehouse frozen", {}, sqlite3.IntegrityError(FROZEN_MESSAGE))


def _before_flush(session, context, instances):
    from .receiving_models import StockPosition
    relevant = [row for row in session.new.union(session.dirty).union(session.deleted)
        if getattr(getattr(row, "__table__", None), "name", None) in ENTRY_TABLES | POSTING_ENTRIES.keys() | {"stock_position", "inventory_lot"}]
    if not relevant:
        return
    connection = session.connection()
    # Older revision fixtures deliberately exercise schemas predating stocktaking.
    if not connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stocktake'").first():
        return
    warehouses = set()
    for row in relevant:
        table = row.__table__.name
        if table == "stock_position":
            warehouses.add(row.warehouse_id)
            warehouses.update(inspect(row).attrs.warehouse_id.history.deleted)
        elif table == "inventory_lot":
            if row not in session.new:
                warehouses.update(session.scalars(select(StockPosition.warehouse_id).where(StockPosition.lot_id == row.id)))
        elif table in POSTING_ENTRIES:
            if table == "inventory_transfer_posting":
                warehouses.update(connection.exec_driver_sql(
                    "SELECT source_warehouse_id FROM inventory_transfer WHERE id=? UNION SELECT destination_warehouse_id FROM inventory_transfer WHERE id=?",
                    (row.transfer_id, row.transfer_id)).scalars())
            entry_table, foreign_key = POSTING_ENTRIES[table]
            warehouses.update(connection.exec_driver_sql(
                f"SELECT p.warehouse_id FROM {entry_table} e JOIN stock_position p ON p.id=e.position_id WHERE e.{foreign_key}=?",
                (row.id,)).scalars())
        else:
            if table == "inventory_transfer_entry":
                warehouses.update(connection.exec_driver_sql(
                    "SELECT source_warehouse_id FROM inventory_transfer WHERE id=? UNION SELECT destination_warehouse_id FROM inventory_transfer WHERE id=?",
                    (row.transfer_id, row.transfer_id)).scalars())
            positions = {row.position_id, *inspect(row).attrs.position_id.history.deleted}
            for position_id in positions:
                position = session.get(StockPosition, position_id)
                if position:
                    warehouses.add(position.warehouse_id)
    assert_warehouses_unfrozen(session, warehouses)


def install_freeze_checks():
    if not event.contains(Session, "before_flush", _before_flush):
        event.listen(Session, "before_flush", _before_flush)
