"""Single-snapshot, query-only MRP preview. No persistent run or allocation."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, Inexact, Rounded, localcontext, MAX_EMAX, MIN_EMIN
from fractions import Fraction
import json

from fastapi import Request
from sqlalchemy import select, text
from .models import Item, Unit, Warehouse
from .bom_models import Bom, BomComponent
from .routing_models import Routing
from .work_order_models import WorkOrder
from .decimal_text import canonical_decimal, exact_sum, exact_difference
from .stock_ledger import compare
from .mrp_schemas import PreviewInput, PreviewOut

DRAFT_LABEL = "草稿試算／非正式生產需求"
DRAFT_BOM = "來源 BOM 尚未正式核准，試算結果僅供規劃參考"
WARNINGS = [
    "本試算仍使用工單毛需求，尚未扣抵已領料量或依已退料量重算剩餘工單需求；毛需求減在手量不是已核准的剩餘補料義務，剩餘需求政策待核定。",
    "共同庫存僅供試算；D05／D07 工單優先序、分配與保留政策尚未核准，未分配給任何個別工單。",
    "未到採購量不是實體庫存，本試算未扣抵未到採購；已過帳退貨已包含在帳本，草稿退貨不扣庫。",
    "範圍為明確選取倉庫的實體在手量；未核定品質、效期、選批、跨倉可互換與承諾供應政策。",
    "D04／D06／D10 數值、核准、有效版本、耗損與替代料政策仍待核定；本試算不自動選版、發放或建立採購。",
]


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def exact_requirement(planned, usage, basis):
    """Reduce a rational first; only return a decimal if it terminates exactly."""
    planned, usage, basis = map(canonical_decimal, (planned, usage, basis))
    if compare(planned, "0") < 0 or compare(usage, "0") < 0:
        raise ValueError("負數計畫量或標準用量的業務語義待核定；不列為供應或可行缺料結論。")
    if compare(basis, "0") <= 0:
        raise ValueError("BOM 產出基準量無法支持除法。")
    value = Fraction(Decimal(planned)) * Fraction(Decimal(usage)) / Fraction(Decimal(basis))
    remainder = value.denominator
    twos = (remainder & -remainder).bit_length()-1
    remainder >>= twos
    fives = 0
    while remainder % 5 == 0:
        fives += 1
        remainder //= 5
    if remainder != 1:
        raise ValueError("計算為非有限小數；D04 尚未核准取位，需求及缺料未定。")
    # Decimal(int) avoids Python's integer-to-string digit limit. Precision is
    # derived from the actual numerator and decimal scale, never a business cap.
    with localcontext() as context:
        context.prec = len(Decimal(value.numerator).as_tuple().digits) + max(twos, fives) + 2
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        context.traps[Inexact] = context.traps[Rounded] = True
        return canonical_decimal(format(Decimal(value.numerator) / Decimal(value.denominator), "f"))


@contextmanager
def read_snapshot(db):
    """BEGIN is supplied by Database; enforce no DML and restore pooled PRAGMA."""
    with db.read() as session:
        driver = session.connection().connection.driver_connection
        previous = driver.execute("PRAGMA query_only").fetchone()[0]
        driver.execute("PRAGMA query_only=ON")
        try:
            yield session
        finally:
            driver.execute("PRAGMA query_only="+str(previous))


def chosen(session, model, column, ids):
    # A single JSON bind avoids an artificial SQLite bind-count selection cap.
    return session.scalars(select(model).where(
        text(column+" IN (SELECT value FROM json_each(:ids))")).params(ids=json.dumps(ids))).all()


def identity(item, units):
    unit = units[item.base_unit_id]
    return dict(id=item.id, code=item.code, name=item.name, row_version=item.row_version,
                unit_id=unit.id, unit_code=unit.code, unit_name=unit.name, unit_version=unit.row_version)


def scoped_stock(session, material_ids, warehouse_ids):
    """One row per position. Ledger origins are independently unioned, never joined."""
    return session.connection().exec_driver_sql("""
        WITH scoped AS (
            SELECT p.* FROM stock_position p
            WHERE p.item_id IN (SELECT value FROM json_each(?))
              AND p.warehouse_id IN (SELECT value FROM json_each(?))
        ), ledger AS (
            SELECT e.position_id,e.qty_base_delta FROM physical_stock_ledger e JOIN scoped s ON s.id=e.position_id
        ), balances AS (
            SELECT position_id,erp_decimal_sum(qty_base_delta) AS quantity FROM ledger GROUP BY position_id
        )
        SELECT s.item_id,s.id AS position_id,s.warehouse_id,w.code AS warehouse_code,
            w.name AS warehouse_name,s.lot_id,l.lot_code,COALESCE(b.quantity,'0') AS on_hand_qty_base
        FROM scoped s JOIN warehouse w ON w.id=s.warehouse_id
        LEFT JOIN inventory_lot l ON l.id=s.lot_id
        LEFT JOIN balances b ON b.position_id=s.id ORDER BY s.item_id,w.code,l.lot_code,s.id
    """, (json.dumps(material_ids), json.dumps(warehouse_ids))).mappings().all()


def calculate(session, data, fail):
    started = timestamp()
    order_ids = sorted({str(x) for x in data.work_order_ids})
    warehouse_ids = sorted({str(x) for x in data.warehouse_ids})
    orders = chosen(session, WorkOrder, "work_order.id", order_ids)
    warehouses = chosen(session, Warehouse, "warehouse.id", warehouse_ids)
    if len(orders) != len(order_ids) or len(warehouses) != len(warehouse_ids):
        fail(422, "選取的工單或倉庫不存在，請重新讀取後明確選取。")
    boms = {b.id:b for b in chosen(session, Bom, "bom.id", sorted({o.bom_id for o in orders if o.bom_id}))}
    routes = {r.id:r for r in chosen(session, Routing, "routing.id", sorted({o.routing_id for o in orders if o.routing_id}))}
    for versions, sources in [(data.expected_work_order_versions,{o.id:o for o in orders}),
                              (data.expected_bom_versions,boms)]:
        for key, version in versions.items():
            if str(key) not in sources:
                fail(422, "版次檢查包含未選取的來源。")
            if sources[str(key)].row_version != version:
                fail(409, "工單或 BOM 已更新；請重新讀取來源，再重新試算。")
    components = chosen(session, BomComponent, "bom_component.bom_id", sorted(boms))
    by_bom = defaultdict(list)
    for component in sorted(components,key=lambda c:(c.bom_id,c.line_no,c.id)):
        by_bom[component.bom_id].append(component)
    item_ids = sorted({o.product_id for o in orders}|{c.material_id for c in components})
    items = {i.id:i for i in chosen(session,Item,"item.id",item_ids)}
    units = {u.id:u for u in chosen(session,Unit,"unit.id",sorted({i.base_unit_id for i in items.values()}))}
    results = []
    demands = defaultdict(list)
    unresolved = set()
    unknown_materials = False
    for order in sorted(orders,key=lambda o:(o.document_no,o.id)):
        bom = boms.get(order.bom_id)
        product = items[order.product_id]
        warnings = [DRAFT_LABEL] if order.approval_state == "DRAFT" else []
        if not order.routing_id:
            warnings.append("未選取途程；僅試算 BOM 用料，不代表製程已備妥。")
        elif order.routing_id not in routes:
            warnings.append("來源途程不存在；未影響可辨識的 BOM 用料。")
        order_lines = []
        complete = True
        if bom is None or not by_bom[bom.id]:
            warnings.append("缺少選定 BOM 或 BOM 無用料明細；無法計算需求，不能視為零。")
            unknown_materials = True
            complete = False
        if bom:
            if bom.release_state == "DRAFT":
                warnings.append(DRAFT_BOM)
            if not bom.is_active:
                warnings.append("選定 BOM 已停用；此為明確來源的初步試算。")
            for component in by_bom[bom.id]:
                material = items[component.material_id]
                line_warnings = []
                required = None
                try:
                    if bom.product_id != order.product_id or product.item_kind != "FINISHED" or material.item_kind != "RAW":
                        raise ValueError("來源產品或原料識別不一致；需要核對來源及基本單位，未執行換算。")
                    required = exact_requirement(order.planned_qty_base,component.usage_qty_base,bom.output_basis_qty)
                    demands[material.id].append(required)
                except ValueError as exc:
                    line_warnings.append(str(exc))
                    unresolved.add(material.id)
                    complete = False
                order_lines.append(dict(component_id=component.id,component_version=component.row_version,
                    line_no=component.line_no,purpose_note=component.purpose_note,material=identity(material,units),
                    usage_qty_base=component.usage_qty_base,required_qty_base=required,warnings=line_warnings))
        results.append(dict(id=order.id,document_no=order.document_no,row_version=order.row_version,
            approval_state=order.approval_state,product=identity(product,units),planned_qty_base=order.planned_qty_base,
            planned_start=order.planned_start,planned_finish=order.planned_finish,
            bom=dict(id=bom.id,revision=bom.revision,row_version=bom.row_version,release_state=bom.release_state,
                     output_basis_qty=bom.output_basis_qty) if bom else None,
            routing_id=order.routing_id,routing_version=routes[order.routing_id].row_version if order.routing_id in routes else None,
            complete=complete,warnings=warnings,components=order_lines))
    material_ids = sorted({c.material_id for c in components})
    positions = defaultdict(list)
    for row in scoped_stock(session,material_ids,warehouse_ids):
        positions[row["item_id"]].append({k:v for k,v in row.items() if k!="item_id"})
    aggregates = []
    for material_id in sorted(material_ids,key=lambda i:(items[i].code,i)):
        warnings = []
        stock = positions[material_id]
        physical = exact_sum([p["on_hand_qty_base"] for p in stock])
        known_demand = exact_sum(demands[material_id])
        demand_complete = material_id not in unresolved and not unknown_materials
        if not demand_complete:
            warnings.append("所選需求含未定用料；已算明細小計不是完整需求，缺料與補料建議未定。")
        supply = physical
        if any(compare(p["on_hand_qty_base"],"0")<0 for p in stock):
            supply = None
            warnings.append("範圍含負庫存位置；保留帳面原值，D04／D05 政策未定，不作可用供應結論。")
        shortage = None
        if demand_complete and supply is not None:
            difference = exact_difference(known_demand,supply)
            shortage = difference if compare(difference,"0")>0 else "0"
        aggregates.append(dict(material=identity(items[material_id],units),
            calculated_demand_qty_base=known_demand,gross_demand_qty_base=known_demand if demand_complete else None,
            demand_complete=demand_complete,physical_stock_qty_base=physical,planning_supply_qty_base=supply,
            shortage_qty_base=shortage,suggested_replenishment_qty_base=shortage,positions=stock,
            warehouses=[dict(warehouse_id=w,on_hand_qty_base=exact_sum([p["on_hand_qty_base"] for p in stock if p["warehouse_id"]==w]))
                        for w in warehouse_ids],warnings=warnings))
    return PreviewOut(calculated_at=timestamp(),snapshot_started_at=started,preliminary=True,
        complete=all(o["complete"] for o in results) and all(m["shortage_qty_base"] is not None for m in aggregates),
        label=DRAFT_LABEL,formula="planned_qty_base × usage_qty_base ÷ output_basis_qty",
        normalized_work_order_ids=order_ids,normalized_warehouse_ids=warehouse_ids,
        duplicate_work_order_ids_removed=len(data.work_order_ids)-len(order_ids),
        duplicate_warehouse_ids_removed=len(data.warehouse_ids)-len(warehouse_ids),
        warehouses=[dict(id=w.id,code=w.code,name=w.name,row_version=w.row_version,is_active=w.is_active,mrp_included=w.mrp_included)
                    for w in sorted(warehouses,key=lambda w:(w.code,w.id))],
        orders=results,materials=aggregates,warnings=WARNINGS)


def install_mrp_routes(app, fail):
    @app.post("/api/mrp/preview",response_model=PreviewOut,tags=["MRP preview"],
              description="Read-only, explicit warehouse scope; exact decimal strings; no stock allocation or writes.")
    def preview(data:PreviewInput,request:Request):
        with read_snapshot(request.app.state.db) as session:
            return calculate(session,data,fail)
