"""Read-only physical stock inquiry over the existing committed ledger view."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request, Response
from sqlalchemy import column, func, or_, select, table

from .decimal_text import exact_sum
from .models import Item, Unit, Warehouse
from .mrp import read_snapshot
from .receiving_models import InventoryLot, StockPosition


def inventory_inquiry(session, item_kind="ALL", q="", warehouse_id=None, lot_code=""):
    """All quantities share one query-only SQLite snapshot; location filters never
    change company totals. No MRP eligibility or reservation policy is applied.
    """
    warehouse_id = str(warehouse_id) if warehouse_id is not None else None
    warehouses = session.scalars(select(Warehouse).order_by(Warehouse.code, Warehouse.id)).all()
    if warehouse_id is not None and warehouse_id not in {w.id for w in warehouses}:
        raise HTTPException(422, "查詢倉庫不存在，請重新選取。")

    selected = select(Item.id).where(Item.item_kind.in_(["RAW", "FINISHED"]))
    if item_kind != "ALL":
        selected = selected.where(Item.item_kind == item_kind)
    if q:
        selected = selected.where(or_(Item.code.contains(q, autoescape=True),
                                      Item.name.contains(q, autoescape=True)))
    items = session.execute(select(Item, Unit).join(Unit, Unit.id == Item.base_unit_id)
        .where(Item.id.in_(selected)).order_by(Item.item_kind, Item.code, Item.id)).all()
    ledger = table("physical_stock_ledger", column("position_id"), column("qty_base_delta"))
    positions = session.execute(select(StockPosition, InventoryLot,
        func.coalesce(func.erp_decimal_sum(ledger.c.qty_base_delta), "0"))
        .outerjoin(InventoryLot, InventoryLot.id == StockPosition.lot_id)
        .outerjoin(ledger, ledger.c.position_id == StockPosition.id)
        .where(StockPosition.item_id.in_(selected)).group_by(StockPosition.id)
        .order_by(StockPosition.warehouse_id, InventoryLot.lot_code, StockPosition.id)).all()

    by_item = {}
    for position, lot, quantity in positions:
        by_item.setdefault(position.item_id, {}).setdefault(position.warehouse_id, []).append(dict(
            id=position.id, lot_id=position.lot_id, lot_code=lot.lot_code if lot else None,
            origin_kind=lot.origin_kind if lot else None, qty_base=quantity))
    location_filtered = warehouse_id is not None or bool(lot_code)
    result = []
    for item, unit in items:
        item_positions = by_item.get(item.id, {})
        company_qty = exact_sum(p["qty_base"] for rows in item_positions.values() for p in rows)
        groups = []
        for warehouse in warehouses:
            rows = item_positions.get(warehouse.id, [])
            if not rows or (warehouse_id is not None and warehouse.id != warehouse_id):
                continue
            matched = [p for p in rows if not lot_code or
                       lot_code.casefold() in (p["lot_code"] or "").casefold()]
            if not matched:
                continue
            groups.append(dict(id=warehouse.id, code=warehouse.code, name=warehouse.name,
                mrp_included=warehouse.mrp_included,
                warehouse_qty_base=exact_sum(p["qty_base"] for p in rows),
                matched_qty_base=exact_sum(p["qty_base"] for p in matched), positions=matched))
        if location_filtered and not groups:
            continue
        result.append(dict(id=item.id, code=item.code, name=item.name, item_kind=item.item_kind,
            unit_id=unit.id, unit_code=unit.code, unit_name=unit.name, lot_tracking=item.lot_tracking,
            company_qty_base=company_qty, matched_qty_base=exact_sum(g["matched_qty_base"] for g in groups),
            warehouses=groups))
    return dict(read_only=True, quantity_basis="COMMITTED_PHYSICAL_STOCK",
        as_of=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        scope=dict(item_kind=item_kind, q=q, warehouse_id=warehouse_id, lot_code=lot_code,
                   location_filtered=location_filtered),
        warehouses=[dict(id=w.id, code=w.code, name=w.name, is_active=w.is_active,
                         mrp_included=w.mrp_included) for w in warehouses], items=result)


def install_inventory_inquiry_routes(app):
    @app.get("/api/inventory-inquiry")
    def inquire(request: Request, response: Response,
                item_kind: Literal["ALL", "RAW", "FINISHED"] = "ALL", q: str = "",
                warehouse_id: UUID | None = None, lot_code: str = ""):
        response.headers["Cache-Control"] = "no-store"
        with read_snapshot(request.app.state.db) as session:
            return inventory_inquiry(session, item_kind, q, warehouse_id, lot_code)
