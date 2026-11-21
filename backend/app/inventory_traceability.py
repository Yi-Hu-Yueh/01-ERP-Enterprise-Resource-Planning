"""Read-only movement inquiry; quantities come only from physical_stock_ledger.

The metadata UNION supplies document identities, never stock quantities or posting
eligibility. The existing ledger remains the sole authority for both.
"""
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from sqlalchemy import text

from .decimal_text import canonical_decimal
from .mrp import read_snapshot


MOVEMENT_TYPES = {
    "PURCHASE_RECEIPT": "採購入庫", "SUPPLIER_RETURN": "供應商退貨",
    "MATERIAL_ISSUE": "生產領料", "MATERIAL_RETURN": "生產退料",
    "FINISHED_GOODS_RECEIPT": "成品入庫", "SALES_SHIPMENT": "銷售出貨",
    "CUSTOMER_RETURN": "客戶退貨", "STOCKTAKE": "盤點差異",
    "INVENTORY_ADJUSTMENT": "庫存調整", "INVENTORY_TRANSFER": "倉庫調撥",
}

# All SQL identifiers below are constants from the verified schema. User input
# enters only bound parameters. The UNION deliberately contains no quantity.
SOURCE_METADATA = """
 SELECT e.id entry_id,'PURCHASE_RECEIPT' source_kind,d.id source_id,l.id source_line_id,
 d.document_no,p.posted_at,d.business_date,e.leg_no,'SUPPLIER' party_kind,d.supplier_id party_id,
 'PURCHASE_ORDER' related_kind,d.purchase_order_id related_id,NULL related2_kind,NULL related2_id
 FROM stock_entry e JOIN stock_posting p ON p.id=e.posting_id
 JOIN stock_document_line l ON l.id=e.document_line_id JOIN stock_document d ON d.id=l.stock_document_id
 UNION ALL
 SELECT e.id,'SUPPLIER_RETURN',d.id,l.id,d.document_no,p.posted_at,d.business_date,e.leg_no,
 'SUPPLIER',d.supplier_id,'PURCHASE_RECEIPT',o.stock_document_id,NULL,NULL
 FROM supplier_return_entry e JOIN supplier_return_posting p ON p.id=e.posting_id
 JOIN supplier_return_line l ON l.id=e.return_line_id JOIN supplier_return d ON d.id=l.supplier_return_id
 JOIN stock_document_line o ON o.id=l.origin_line_id
 UNION ALL
 SELECT e.id,'MATERIAL_ISSUE',d.id,l.id,d.document_no,p.posted_at,d.business_date,e.leg_no,
 NULL,NULL,'WORK_ORDER',d.work_order_id,NULL,NULL
 FROM material_issue_entry e JOIN material_issue_posting p ON p.id=e.posting_id
 JOIN material_issue_line l ON l.id=e.issue_line_id JOIN material_issue d ON d.id=l.material_issue_id
 UNION ALL
 SELECT e.id,'MATERIAL_RETURN',d.id,l.id,d.document_no,p.posted_at,d.business_date,e.leg_no,
 NULL,NULL,'MATERIAL_ISSUE',o.material_issue_id,'WORK_ORDER',d.work_order_id
 FROM material_return_entry e JOIN material_return_posting p ON p.id=e.posting_id
 JOIN material_return_line l ON l.id=e.return_line_id JOIN material_return d ON d.id=l.material_return_id
 JOIN material_issue_line o ON o.id=l.origin_line_id
 UNION ALL
 SELECT e.id,'FINISHED_GOODS_RECEIPT',d.id,NULL,d.document_no,p.posted_at,d.business_date,e.leg_no,
 NULL,NULL,'PRODUCTION_COMPLETION',d.completion_id,NULL,NULL
 FROM finished_goods_entry e JOIN finished_goods_posting p ON p.id=e.posting_id
 JOIN finished_goods_receipt d ON d.id=e.receipt_id
 UNION ALL
 SELECT e.id,'SALES_SHIPMENT',d.id,NULL,d.document_no,p.posted_at,d.business_date,e.leg_no,
 'CUSTOMER',d.customer_id,'SALES_ORDER',d.sales_order_id,NULL,NULL
 FROM sales_shipment_entry e JOIN sales_shipment_posting p ON p.id=e.posting_id
 JOIN sales_shipment d ON d.id=e.shipment_id
 UNION ALL
 SELECT e.id,'CUSTOMER_RETURN',d.id,NULL,d.document_no,p.finalized_at,d.business_date,e.leg_no,
 'CUSTOMER',d.customer_id,'SALES_SHIPMENT',d.shipment_id,'SALES_ORDER',d.sales_order_id
 FROM customer_return_entry e JOIN customer_return_finalization p ON p.id=e.finalization_id
 JOIN customer_return d ON d.id=e.return_id
 UNION ALL
 SELECT e.id,'STOCKTAKE',d.id,e.stocktake_line_id,d.document_no,p.posted_at,d.business_date,e.leg_no,
 NULL,NULL,NULL,NULL,NULL,NULL
 FROM stocktake_entry e JOIN stocktake_posting p ON p.id=e.posting_id JOIN stocktake d ON d.id=e.stocktake_id
 UNION ALL
 SELECT e.id,'INVENTORY_ADJUSTMENT',d.id,NULL,d.document_no,p.posted_at,d.business_date,e.leg_no,
 NULL,NULL,NULL,NULL,NULL,NULL
 FROM inventory_adjustment_entry e JOIN inventory_adjustment_posting p ON p.id=e.posting_id
 JOIN inventory_adjustment d ON d.id=e.adjustment_id
 UNION ALL
 SELECT e.id,'INVENTORY_TRANSFER',d.id,NULL,d.document_no,p.posted_at,d.business_date,e.leg_no,
 NULL,NULL,NULL,NULL,NULL,NULL
 FROM inventory_transfer_entry e JOIN inventory_transfer_posting p ON p.id=e.posting_id
 JOIN inventory_transfer d ON d.id=e.transfer_id
"""

ENRICHED = """WITH metadata AS (""" + SOURCE_METADATA + """), enriched AS (
 SELECT ledger.entry_id id,ledger.posting_id,metadata.*,ledger.position_id,
 ledger.qty_base_delta,ledger.work_order_id,wo.document_no work_order_no,
 i.id item_id,i.item_kind,i.code item_code,i.name item_name,
 w.id warehouse_id,w.code warehouse_code,w.name warehouse_name,
 lot.id lot_id,lot.lot_code,u.id unit_id,u.code unit_code,u.name unit_name,
 COALESCE(s.code,c.code) party_code,COALESCE(s.name,c.name) party_name,
 COALESCE(po.document_no,rcv.document_no,mi.document_no,pc.document_no,so.document_no,rwo.document_no,ss.document_no) related_no,
 COALESCE(so2.document_no,wo2.document_no) related2_no
 FROM physical_stock_ledger ledger
 JOIN metadata ON metadata.entry_id=ledger.entry_id AND metadata.source_kind=ledger.domain
 JOIN stock_position pos ON pos.id=ledger.position_id
 JOIN item i ON i.id=pos.item_id JOIN unit u ON u.id=i.base_unit_id
 JOIN warehouse w ON w.id=pos.warehouse_id LEFT JOIN inventory_lot lot ON lot.id=pos.lot_id
 LEFT JOIN work_order wo ON wo.id=ledger.work_order_id
 LEFT JOIN supplier s ON metadata.party_kind='SUPPLIER' AND s.id=metadata.party_id
 LEFT JOIN customer c ON metadata.party_kind='CUSTOMER' AND c.id=metadata.party_id
 LEFT JOIN purchase_order po ON metadata.related_kind='PURCHASE_ORDER' AND po.id=metadata.related_id
 LEFT JOIN stock_document rcv ON metadata.related_kind='PURCHASE_RECEIPT' AND rcv.id=metadata.related_id
 LEFT JOIN material_issue mi ON metadata.related_kind='MATERIAL_ISSUE' AND mi.id=metadata.related_id
 LEFT JOIN production_completion pc ON metadata.related_kind='PRODUCTION_COMPLETION' AND pc.id=metadata.related_id
 LEFT JOIN sales_order so ON metadata.related_kind='SALES_ORDER' AND so.id=metadata.related_id
 LEFT JOIN work_order rwo ON metadata.related_kind='WORK_ORDER' AND rwo.id=metadata.related_id
 LEFT JOIN sales_shipment ss ON metadata.related_kind='SALES_SHIPMENT' AND ss.id=metadata.related_id
 LEFT JOIN sales_order so2 ON metadata.related2_kind='SALES_ORDER' AND so2.id=metadata.related2_id
 LEFT JOIN work_order wo2 ON metadata.related2_kind='WORK_ORDER' AND wo2.id=metadata.related2_id
 WHERE i.item_kind IN ('RAW','FINISHED')
) """


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def warehouse_options(session):
    return [dict(r, mrp_included=bool(r["mrp_included"]), is_active=bool(r["is_active"]))
            for r in session.execute(text("SELECT id,code,name,mrp_included,is_active FROM warehouse ORDER BY code,id")).mappings()]


def movement_output(row):
    result = dict(row)
    result["qty_base_delta"] = canonical_decimal(result["qty_base_delta"])
    result["source_label"] = MOVEMENT_TYPES[result["source_kind"]]
    result["related_documents"] = []
    for suffix in ("", "2"):
        kind, identity, number = (result.pop("related" + suffix + key) for key in ("_kind", "_id", "_no"))
        if identity:
            result["related_documents"].append(dict(kind=kind, id=identity, document_no=number))
    result.pop("entry_id", None)
    return result


def all_movements(session):
    """Internal graph input, also from the single authoritative ledger."""
    return [movement_output(r) for r in session.execute(text(ENRICHED +
        "SELECT * FROM enriched ORDER BY posted_at,posting_id,leg_no,id,source_kind")).mappings()]


def inventory_movements(session, *, date_from=None, date_to=None, item_kind="ALL", q="",
                        warehouse_id=None, lot_code="", movement_type="ALL", document_no="",
                        offset=0, limit=100):
    warehouses = warehouse_options(session)
    warehouse_id = str(warehouse_id) if warehouse_id is not None else None
    if warehouse_id is not None and warehouse_id not in {w["id"] for w in warehouses}:
        raise HTTPException(422, "查詢倉庫不存在，請重新選取。")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "過帳日期起日不可晚於迄日。")
    if movement_type != "ALL" and movement_type not in MOVEMENT_TYPES:
        raise HTTPException(422, "庫存異動類型不存在，請重新選取。")
    clauses, params = [], {}
    for name, value in (("item_kind", item_kind if item_kind != "ALL" else None),
                        ("warehouse_id", warehouse_id),
                        ("source_kind", movement_type if movement_type != "ALL" else None)):
        if value is not None:
            clauses.append(name + "=:" + name)
            params[name] = value
    # instr treats %, _, quotes and backslashes literally rather than as SQL.
    for name, fields, value in (("q", ("item_code", "item_name"), q),
                                ("lot_code", ("lot_code",), lot_code),
                                ("document_no", ("document_no",), document_no)):
        if value:
            clauses.append("(" + " OR ".join("instr(lower(COALESCE(" + field + ",'')),lower(:" + name + "))>0" for field in fields) + ")")
            params[name] = value
    if date_from:
        clauses.append("posted_at>=:date_from")
        params["date_from"] = date_from.isoformat() + "T00:00:00"
    if date_to:
        # date.max cannot add one day. ISO timestamp's full calendar-date prefix
        # gives the same inclusive UTC-day semantics without overflowing it.
        clauses.append("substr(posted_at,1,10)<=:date_to")
        params["date_to"] = date_to.isoformat()
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = session.execute(text(ENRICHED + "SELECT count(*) FROM enriched" + where), params).scalar_one()
    rows = session.execute(text(ENRICHED + "SELECT * FROM enriched" + where +
        " ORDER BY posted_at,posting_id,leg_no,id,source_kind LIMIT :limit OFFSET :offset"),
        dict(params, limit=limit, offset=offset)).mappings()
    return dict(read_only=True, quantity_basis="COMMITTED_PHYSICAL_STOCK", as_of=timestamp(),
        scope=dict(date_from=date_from, date_to=date_to, item_kind=item_kind, q=q,
            warehouse_id=warehouse_id, lot_code=lot_code, movement_type=movement_type,
            document_no=document_no, date_basis="POSTED_AT_UTC"), warehouses=warehouses,
        movement_types=[dict(value=k, label=v) for k, v in MOVEMENT_TYPES.items()],
        total=total, offset=offset, limit=limit, rows=[movement_output(r) for r in rows])


def install_inventory_traceability_routes(app):
    @app.get("/api/inventory-movements")
    def inquire(request: Request, response: Response, date_from: date | None = None,
                date_to: date | None = None, item_kind: Literal["ALL", "RAW", "FINISHED"] = "ALL",
                q: str = "", warehouse_id: UUID | None = None, lot_code: str = "",
                movement_type: str = "ALL", document_no: str = "",
                offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        response.headers["Cache-Control"] = "no-store"
        with read_snapshot(request.app.state.db) as session:
            return inventory_movements(session, date_from=date_from, date_to=date_to,
                item_kind=item_kind, q=q, warehouse_id=warehouse_id, lot_code=lot_code,
                movement_type=movement_type, document_no=document_no, offset=offset, limit=limit)

    @app.get("/api/lot-traceability")
    def trace(request: Request, response: Response, direction: Literal["forward", "reverse"] = "forward",
              lot_id: UUID | None = None, shipment_id: UUID | None = None,
              mode: Literal["trace", "options"] = "trace"):
        from .inventory_trace_graph import lot_traceability, trace_options
        response.headers["Cache-Control"] = "no-store"
        if lot_id and shipment_id:
            raise HTTPException(422, "請擇一選取真實庫存批號或已過帳出貨單。")
        if shipment_id and direction != "reverse":
            raise HTTPException(422, "出貨單請使用反向追溯。")
        with read_snapshot(request.app.state.db) as session:
            if mode == "options" or (lot_id is None and shipment_id is None):
                return trace_options(session)
            return lot_traceability(session, direction, str(lot_id) if lot_id else None,
                                    str(shipment_id) if shipment_id else None)
