"""Operation 30: grain-preserving inquiries, with no stored reporting facts.

Each source is read once in a query-only SQLite snapshot.  Header money, line
demand, confirmed production and immutable physical legs remain separate facts.
Pagination is applied only after the complete filtered aggregates are computed.
"""
from collections import defaultdict
from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from sqlalchemy import text

from .decimal_text import canonical_decimal, exact_difference, exact_product, exact_sum
from .inventory_traceability import ENRICHED, timestamp
from .mrp import read_snapshot
from .stock_ledger import negate


QUANTITIES = {
    "purchasing": ("ordered_qty_base", "gross_received_qty_base", "posted_return_qty_base",
                   "net_retained_qty_base", "current_gross_received_qty_base", "current_outstanding_qty_base"),
    "sales": ("ordered_qty_base", "gross_shipped_qty_base", "finalized_returned_qty_base",
              "restocked_return_qty_base", "nonrestocked_return_qty_base", "net_physical_shipped_qty_base",
              "current_gross_shipped_qty_base", "current_outstanding_qty_base"),
    "manufacturing": ("planned_qty_base", "confirmed_good_qty_base", "confirmed_defective_qty_base",
                      "confirmed_total_qty_base", "fg_received_qty_base", "material_issued_qty_base",
                      "material_returned_qty_base", "net_material_issued_qty_base"),
}
COUNTS = {
    "purchasing": (("order_count", "採購單數"), ("receipt_count", "已過帳收貨單數"),
                   ("supplier_return_count", "已過帳供應商退貨單數")),
    "sales": (("order_count", "銷售單數"), ("shipment_count", "已過帳出貨單數"),
              ("customer_return_count", "已完成客退單數"), ("nonrestock_return_count", "已完成不回庫客退單數")),
    "manufacturing": (("work_order_count", "工單數"), ("completion_count", "已確認完工回報數"),
                      ("fg_receipt_count", "已過帳成品入庫單數"), ("material_issue_count", "已過帳領料單數"),
                      ("material_return_count", "已過帳退料單數")),
}
SOURCES = {
    "purchasing": (("orders", "採購單"), ("order_lines", "採購明細與目前未交"),
                   ("receipts", "已過帳採購入庫來源"), ("supplier_returns", "已過帳供應商退貨來源")),
    "sales": (("orders", "銷售單"), ("order_lines", "銷售明細與目前未交"),
              ("shipments", "已過帳銷售出貨來源"), ("customer_returns", "已完成客退來源")),
    "manufacturing": (("work_orders", "工單計畫來源"), ("completions", "已確認完工回報來源"),
                      ("fg_receipts", "已過帳成品入庫來源"), ("material_issues", "已過帳領料來源"),
                      ("material_returns", "已過帳退料來源")),
}
MOVEMENTS = {
    "purchasing": ("PURCHASE_RECEIPT", "SUPPLIER_RETURN"),
    "sales": ("SALES_SHIPMENT", "CUSTOMER_RETURN"),
    "manufacturing": ("FINISHED_GOODS_RECEIPT", "MATERIAL_ISSUE", "MATERIAL_RETURN"),
}
MOVEMENT_SOURCE = {
    "PURCHASE_RECEIPT": ("receipts", "receipt_count", "gross_received_qty_base"),
    "SUPPLIER_RETURN": ("supplier_returns", "supplier_return_count", "posted_return_qty_base"),
    "SALES_SHIPMENT": ("shipments", "shipment_count", "gross_shipped_qty_base"),
    "CUSTOMER_RETURN": ("customer_returns", "customer_return_count", "restocked_return_qty_base"),
    "FINISHED_GOODS_RECEIPT": ("fg_receipts", "fg_receipt_count", "fg_received_qty_base"),
    "MATERIAL_ISSUE": ("material_issues", "material_issue_count", "material_issued_qty_base"),
    "MATERIAL_RETURN": ("material_returns", "material_return_count", "material_returned_qty_base"),
}


def rows(session, sql, params=None):
    return [dict(row) for row in session.execute(text(sql), params or {}).mappings()]


def statistics_options(session, area):
    party = {"purchasing": "supplier", "sales": "customer"}.get(area)
    # Identifiers are selected from constants, never supplied by request text.
    parties = rows(session, "SELECT id,code,name FROM " + party + " ORDER BY code COLLATE BINARY,id") if party else []
    kind = {"purchasing": "RAW", "sales": "FINISHED"}.get(area)
    items = rows(session, """SELECT id,code,name,item_kind FROM item
        WHERE item_kind IN ('RAW','FINISHED') AND (:kind IS NULL OR item_kind=:kind)
        ORDER BY code COLLATE BINARY,id""", dict(kind=kind))
    orders = rows(session, "SELECT id,document_no FROM work_order ORDER BY document_no COLLATE BINARY,id") if area == "manufacturing" else []
    return dict(read_only=True, area=area, parties=parties, items=items, work_orders=orders)


def source_row(**values):
    """Stable nullable detail fields make a grain explicit without guessed data."""
    result = dict.fromkeys(("id", "source_id", "source_kind", "document_no", "event_date", "date_basis",
        "business_date", "state", "party_id", "party_code", "party_name", "item_id", "item_code", "item_name",
        "item_kind", "unit_id", "unit_code", "unit_name", "quantity_base", "amount", "currency_id",
        "currency_code", "currency_name", "order_id", "order_no", "order_line_id", "source_line_id",
        "work_order_id", "work_order_no", "posting_id", "warehouse_id", "warehouse_code", "lot_id", "lot_code"))
    result.update(related_documents=[], metrics={}, **values)
    return result


def related(kind, identity, number):
    return dict(kind=kind, id=identity, document_no=number)


def in_period(value, start, end):
    # Persisted UTCText is canonical UTC Z; business dates are ISO calendar days.
    # Prefix comparison includes every instant of the final day, without adding
    # a day (which would overflow at date.max).
    return value is not None and (not start or value[:10] >= start) and (not end or value[:10] <= end)


def matches(row, filters, *, period=True):
    for key in ("party_id", "item_id", "work_order_id"):
        if filters[key] and row.get(key) != filters[key]:
            return False
    term = filters["document_no"].casefold()
    if term and not any(term in (number or "").casefold() for number in
                        [row.get("document_no"), row.get("order_no"), row.get("work_order_no")]
                        + [d["document_no"] for d in row["related_documents"]]):
        return False
    return not period or in_period(row["event_date"], filters["date_from"], filters["date_to"])


def order_sources(session, area, filters):
    """Full headers and matching demand lines; never join a posting fan-out."""
    purchase = area == "purchasing"
    table, line, party = ("purchase_order", "purchase_order_line", "supplier") if purchase else ("sales_order", "sales_order_line", "customer")
    parent, party_fk = table + "_id", party + "_id"
    currency_fields = "h.currency_id,c.code currency_code,c.name currency_name" if purchase else "NULL currency_id,NULL currency_code,NULL currency_name"
    currency_join = "JOIN currency c ON c.id=h.currency_id" if purchase else ""
    state = "h.approval_state" if purchase else "h.state"
    # All-header money is independently reduced before item selection. The item
    # EXISTS chooses a header; it does not turn its amount into a partial total.
    query = f"""WITH amounts AS (
        SELECT {parent} order_id,erp_decimal_sum(erp_decimal_mul(ordered_qty,unit_price)) amount
        FROM {line} GROUP BY {parent}
      ), selected AS (
        SELECT h.id,h.document_no,h.order_date,h.{party_fk} party_id,p.code party_code,p.name party_name,
          {currency_fields},{state} state,COALESCE(a.amount,'0') amount
        FROM {table} h JOIN {party} p ON p.id=h.{party_fk} {currency_join}
        LEFT JOIN amounts a ON a.order_id=h.id
        WHERE (:party_id IS NULL OR h.{party_fk}=:party_id)
          AND (:item_id IS NULL OR EXISTS(SELECT 1 FROM {line} l WHERE l.{parent}=h.id AND l.item_id=:item_id))
      ) """
    headers = []
    for row in rows(session, query + "SELECT * FROM selected ORDER BY order_date,document_no COLLATE BINARY,id", filters):
        row = source_row(**row, source_id=row["id"], source_kind="PURCHASE_ORDER" if purchase else "SALES_ORDER",
            order_id=row["id"], order_no=row["document_no"], event_date=row["order_date"],
            business_date=row["order_date"], date_basis="ORDER_DATE")
        # Item is a matching-header criterion here; line quantities below remain
        # restricted to that item. Header identity itself is not a single item.
        if matches(row, dict(filters, item_id=None)):
            headers.append(row)
    selected_ids = {row["id"] for row in headers}
    factor = "l.base_factor_snapshot" if purchase else "'1'"
    lines = []
    for record in rows(session, query + f"""SELECT l.id,l.{parent} order_id,h.document_no,h.order_date,
        h.party_id,h.party_code,h.party_name,h.currency_id,h.currency_code,h.currency_name,h.state,
        l.line_no,l.item_id,i.code item_code,i.name item_name,i.item_kind,
        u.id unit_id,u.code unit_code,u.name unit_name,l.unit_id order_unit_id,l.ordered_qty,l.unit_price,
        {factor} base_factor_snapshot
        FROM {line} l JOIN selected h ON h.id=l.{parent} JOIN item i ON i.id=l.item_id
        JOIN unit u ON u.id=i.base_unit_id WHERE (:item_id IS NULL OR l.item_id=:item_id)
        ORDER BY h.order_date,h.document_no COLLATE BINARY,h.id,l.line_no,l.id""", filters):
        if record["order_id"] not in selected_ids:
            continue
        quantity = exact_product(record["ordered_qty"], record["base_factor_snapshot"])
        lines.append(source_row(**record, source_id=record["order_id"], source_line_id=record["id"],
            order_line_id=record["id"], order_no=record["document_no"], source_kind="PURCHASE_ORDER_LINE" if purchase else "SALES_ORDER_LINE",
            event_date=record["order_date"], business_date=record["order_date"], date_basis="ORDER_DATE",
            quantity_base=quantity, amount=exact_product(record["ordered_qty"], record["unit_price"])))
    return headers, lines


def physical_sources(session, area, filters):
    """One row per authoritative ledger entry; all enrichment joins target PKs.

    No date/document filter is applied here: the matching-order CURRENT gross
    fulfillment must still include postings outside a selected reporting period.
    """
    kinds = ",".join("'" + k + "'" for k in MOVEMENTS[area])
    sql = ENRICHED + """, sources AS (
      SELECT e.*,COALESCE(r.purchase_order_line_id,ro.purchase_order_line_id,ss.sales_order_line_id,cr.sales_order_line_id) order_line_id,
        CASE e.source_kind WHEN 'PURCHASE_RECEIPT' THEN e.related_id WHEN 'SUPPLIER_RETURN' THEN rd.purchase_order_id
          WHEN 'SALES_SHIPMENT' THEN e.related_id WHEN 'CUSTOMER_RETURN' THEN e.related2_id END order_id,
        COALESCE(po.document_no,so.document_no) order_no,
        COALESCE(po.order_date,so.order_date) order_date,fg.completion_id,
        cr.restock_eligible
      FROM enriched e
      LEFT JOIN stock_document_line r ON e.source_kind='PURCHASE_RECEIPT' AND r.id=e.source_line_id
      LEFT JOIN supplier_return_line sr ON e.source_kind='SUPPLIER_RETURN' AND sr.id=e.source_line_id
      LEFT JOIN stock_document_line ro ON ro.id=sr.origin_line_id
      LEFT JOIN stock_document rd ON rd.id=ro.stock_document_id
      LEFT JOIN sales_shipment ss ON e.source_kind='SALES_SHIPMENT' AND ss.id=e.source_id
      LEFT JOIN customer_return cr ON e.source_kind='CUSTOMER_RETURN' AND cr.id=e.source_id
      LEFT JOIN finished_goods_receipt fg ON e.source_kind='FINISHED_GOODS_RECEIPT' AND fg.id=e.source_id
      LEFT JOIN purchase_order po ON po.id=CASE WHEN e.source_kind='PURCHASE_RECEIPT' THEN e.related_id
                                               WHEN e.source_kind='SUPPLIER_RETURN' THEN rd.purchase_order_id END
      LEFT JOIN sales_order so ON so.id=CASE WHEN e.source_kind='SALES_SHIPMENT' THEN e.related_id
                                           WHEN e.source_kind='CUSTOMER_RETURN' THEN e.related2_id END
    ) SELECT * FROM sources WHERE source_kind IN (""" + kinds + """)
      AND (:party_id IS NULL OR party_id=:party_id) AND (:item_id IS NULL OR item_id=:item_id)
      AND (:work_order_id IS NULL OR work_order_id=:work_order_id)
      ORDER BY posted_at,document_no COLLATE BINARY,source_id,leg_no,id"""
    result = []
    for row in rows(session, sql, filters):
        kind = row["source_kind"]
        quantity = negate(row["qty_base_delta"]) if kind in ("SUPPLIER_RETURN", "SALES_SHIPMENT", "MATERIAL_ISSUE") else canonical_decimal(row["qty_base_delta"])
        detail = source_row(**{k: row[k] for k in ("id", "source_id", "source_kind", "source_line_id", "document_no",
            "business_date", "party_id", "party_code", "party_name", "item_id", "item_code", "item_name", "item_kind",
            "unit_id", "unit_code", "unit_name", "order_id", "order_no", "order_line_id", "work_order_id", "work_order_no",
            "posting_id", "warehouse_id", "warehouse_code", "lot_id", "lot_code")},
            event_date=row["posted_at"], date_basis="FINALIZED_AT_UTC" if kind == "CUSTOMER_RETURN" else "POSTED_AT_UTC",
            state="POSTED", quantity_base=quantity, qty_base_delta=canonical_decimal(row["qty_base_delta"]),
            position_id=row["position_id"], leg_no=row["leg_no"])
        for suffix in ("", "2"):
            if row["related" + suffix + "_id"]:
                detail["related_documents"].append(related(row["related" + suffix + "_kind"],
                    row["related" + suffix + "_id"], row["related" + suffix + "_no"]))
        if row["order_id"]:
            ref = related("PURCHASE_ORDER" if area == "purchasing" else "SALES_ORDER", row["order_id"], row["order_no"])
            if ref not in detail["related_documents"]:
                detail["related_documents"].append(ref)
        if kind == "CUSTOMER_RETURN":
            detail["restock_eligible"] = True
        result.append(detail)
    return result


def no_restock_sources(session, filters):
    """The existing finalized-return eligibility, expressed as one set query.

    A recorded/no-restock outcome is business return context, never a ledger leg.
    Its original shipment must itself exist in the authoritative physical ledger.
    """
    sql = """SELECT r.id,r.document_no,r.business_date,r.quantity,r.customer_id party_id,c.code party_code,c.name party_name,
        r.product_id item_id,i.code item_code,i.name item_name,i.item_kind,r.unit_id,u.code unit_code,u.name unit_name,
        r.sales_order_id order_id,o.document_no order_no,r.sales_order_line_id order_line_id,
        r.shipment_id,s.document_no shipment_no,f.id posting_id,f.finalized_at,
        r.warehouse_id,w.code warehouse_code,r.lot_id,lot.lot_code,r.position_id
      FROM customer_return r JOIN customer_return_finalization f ON f.return_id=r.id
      JOIN sales_shipment s ON s.id=r.shipment_id JOIN sales_order o ON o.id=r.sales_order_id
      JOIN customer c ON c.id=r.customer_id JOIN item i ON i.id=r.product_id JOIN unit u ON u.id=r.unit_id
      JOIN warehouse w ON w.id=r.warehouse_id LEFT JOIN inventory_lot lot ON lot.id=r.lot_id
      WHERE f.finalization_state='RECORDED_NO_RESTOCK' AND r.restock_eligible=0
        AND r.shipment_version=s.row_version AND r.row_version=f.source_version+1
        AND r.sales_order_line_id=s.sales_order_line_id AND r.sales_order_id=s.sales_order_id
        AND r.customer_id=s.customer_id AND r.product_id=s.product_id AND r.unit_id=s.unit_id
        AND r.position_id=s.position_id AND r.warehouse_id=s.warehouse_id AND r.lot_id IS s.lot_id
        AND erp_decimal_cmp(r.quantity,'0')>0
        AND NOT EXISTS(SELECT 1 FROM customer_return_entry e WHERE e.finalization_id=f.id)
        AND EXISTS(SELECT 1 FROM physical_stock_ledger l JOIN sales_shipment_entry se ON se.id=l.entry_id
          WHERE l.domain='SALES_SHIPMENT' AND se.shipment_id=s.id AND se.posting_id=l.posting_id
            AND se.position_id=s.position_id AND se.leg_no=1 AND se.qty_base_delta=erp_decimal_neg(s.quantity))
        AND (:party_id IS NULL OR r.customer_id=:party_id) AND (:item_id IS NULL OR r.product_id=:item_id)
      ORDER BY f.finalized_at,r.document_no COLLATE BINARY,r.id"""
    result = []
    for row in rows(session, sql, filters):
        detail = source_row(**{k: row[k] for k in ("id", "document_no", "business_date", "party_id", "party_code", "party_name",
            "item_id", "item_code", "item_name", "item_kind", "unit_id", "unit_code", "unit_name", "order_id", "order_no",
            "order_line_id", "posting_id", "warehouse_id", "warehouse_code", "lot_id", "lot_code")}, source_id=row["id"],
            source_kind="CUSTOMER_RETURN_NO_RESTOCK", event_date=row["finalized_at"], date_basis="FINALIZED_AT_UTC",
            state="RECORDED_NO_RESTOCK", quantity_base=canonical_decimal(row["quantity"]), restock_eligible=False,
            position_id=row["position_id"], qty_base_delta="0")
        detail["related_documents"] = [related("SALES_SHIPMENT", row["shipment_id"], row["shipment_no"]),
            related("SALES_ORDER", row["order_id"], row["order_no"])]
        result.append(detail)
    return result


def manufacturing_sources(session, filters):
    common = """JOIN item i ON i.id=w.product_id JOIN unit u ON u.id=i.base_unit_id"""
    orders = rows(session, """SELECT w.id,w.document_no,w.product_id item_id,i.code item_code,i.name item_name,i.item_kind,
        u.id unit_id,u.code unit_code,u.name unit_name,w.planned_qty_base,w.planned_start,w.planned_finish,w.approval_state state
        FROM work_order w """ + common + """ WHERE (:item_id IS NULL OR w.product_id=:item_id)
        AND (:work_order_id IS NULL OR w.id=:work_order_id) ORDER BY w.planned_start,w.document_no COLLATE BINARY,w.id""", filters)
    order_result = []
    for row in orders:
        order_result.append(source_row(**row, source_id=row["id"], source_kind="WORK_ORDER", work_order_id=row["id"],
            work_order_no=row["document_no"], business_date=row["planned_start"], event_date=row["planned_start"],
            date_basis="PLANNED_START", quantity_base=canonical_decimal(row["planned_qty_base"])))
    completions = rows(session, """SELECT c.id,c.document_no,c.work_order_id,w.document_no work_order_no,
        c.product_id item_id,i.code item_code,i.name item_name,i.item_kind,c.unit_id,u.code unit_code,u.name unit_name,
        c.good_qty_base,c.defective_qty_base,c.report_date business_date,c.confirmed_at,c.confirmation_id,c.state
        FROM production_completion c JOIN work_order w ON w.id=c.work_order_id
        JOIN item i ON i.id=c.product_id JOIN unit u ON u.id=c.unit_id
        WHERE c.state='CONFIRMED' AND c.confirmed_at IS NOT NULL
          AND (:item_id IS NULL OR c.product_id=:item_id) AND (:work_order_id IS NULL OR c.work_order_id=:work_order_id)
        ORDER BY c.confirmed_at,c.document_no COLLATE BINARY,c.id""", filters)
    completion_result = []
    for row in completions:
        detail = source_row(**row, source_id=row["id"], source_kind="PRODUCTION_COMPLETION", event_date=row["confirmed_at"],
            date_basis="CONFIRMED_AT_UTC", quantity_base=exact_sum([row["good_qty_base"], row["defective_qty_base"]]))
        detail["related_documents"] = [related("WORK_ORDER", row["work_order_id"], row["work_order_no"])]
        completion_result.append(detail)
    return order_result, completion_result


def facts_for(session, area, filters):
    sources = {key: [] for key, _ in SOURCES[area]}
    physical = physical_sources(session, area, filters)
    if area in ("purchasing", "sales"):
        headers, lines = order_sources(session, area, filters)
        gross = defaultdict(list)
        fulfillment = "PURCHASE_RECEIPT" if area == "purchasing" else "SALES_SHIPMENT"
        for row in physical:
            if row["source_kind"] == fulfillment:
                gross[(row["order_line_id"], row["unit_id"])].append(row["quantity_base"])
        for row in headers:
            row["_counts"] = {"order_count": row["id"]}
        for row in lines:
            received = exact_sum(gross[(row["id"], row["unit_id"])])
            row["metrics"] = dict(ordered_qty_base=row["quantity_base"],
                current_outstanding_qty_base=exact_difference(row["quantity_base"], received))
            row["metrics"]["current_gross_received_qty_base" if area == "purchasing" else "current_gross_shipped_qty_base"] = received
        sources["orders"], sources["order_lines"] = headers, lines
    else:
        orders, completions = manufacturing_sources(session, filters)
        for row in orders:
            row["metrics"] = dict(planned_qty_base=row["quantity_base"])
            row["_counts"] = {"work_order_count": row["id"]}
        for row in completions:
            row["metrics"] = dict(confirmed_good_qty_base=canonical_decimal(row["good_qty_base"]),
                confirmed_defective_qty_base=canonical_decimal(row["defective_qty_base"]), confirmed_total_qty_base=row["quantity_base"])
            row["_counts"] = {"completion_count": row["id"]}
        sources["work_orders"] = [r for r in orders if matches(r, filters)]
        sources["completions"] = [r for r in completions if matches(r, filters)]
    for row in physical:
        if not matches(row, filters):
            continue
        key, count, quantity = MOVEMENT_SOURCE[row["source_kind"]]
        row["metrics"] = {quantity: row["quantity_base"]}
        row["_counts"] = {count: row["source_id"]}
        if row["source_kind"] == "CUSTOMER_RETURN":
            row["metrics"]["finalized_returned_qty_base"] = row["quantity_base"]
        sources[key].append(row)
    if area == "sales":
        for row in no_restock_sources(session, filters):
            if matches(row, filters):
                row["metrics"] = dict(finalized_returned_qty_base=row["quantity_base"], nonrestocked_return_qty_base=row["quantity_base"])
                row["_counts"] = dict(customer_return_count=row["id"], nonrestock_return_count=row["id"])
                sources["customer_returns"].append(row)
    return sources


def quantity_groups(records, area):
    grouped = defaultdict(list)
    for row in records:
        if row["unit_id"] and row["metrics"]:
            grouped[row["unit_id"]].append(row)
    result = []
    for unit_id, group in grouped.items():
        entry = {k: group[0][k] for k in ("unit_id", "unit_code", "unit_name")}
        applicable = QUANTITIES[area]
        if area == "manufacturing":
            kinds = {row["item_kind"] for row in group}
            applicable = tuple(key for key in applicable if
                (key in QUANTITIES[area][:5] and "FINISHED" in kinds) or
                (key in QUANTITIES[area][5:] and "RAW" in kinds))
        entry.update({key: exact_sum(row["metrics"].get(key, "0") for row in group) for key in applicable})
        for target, left, right in {
            "purchasing": (("net_retained_qty_base", "gross_received_qty_base", "posted_return_qty_base"),),
            "sales": (("net_physical_shipped_qty_base", "gross_shipped_qty_base", "restocked_return_qty_base"),),
            "manufacturing": (("net_material_issued_qty_base", "material_issued_qty_base", "material_returned_qty_base"),),
        }[area]:
            if target in applicable:
                entry[target] = exact_difference(entry[left], entry[right])
        result.append(entry)
    return sorted(result, key=lambda row: (row["unit_code"], row["unit_id"]))


def amount_groups(records, area, *, line_basis=False):
    money = [row for row in records if row["amount"] is not None
             and row["source_kind"] in (("PURCHASE_ORDER_LINE", "SALES_ORDER_LINE") if line_basis else ("PURCHASE_ORDER", "SALES_ORDER"))]
    if area == "sales":
        # SalesOrder has no persisted currency FK. Customer defaults are not a
        # transaction snapshot. Preserve a separate raw value for EACH order.
        grouped = defaultdict(list)
        for row in money:
            grouped[row["order_id"]].append(row)
        return [dict(order_id=identity, document_no=group[0]["order_no"], currency_id=None, currency_code=None,
            currency_name=None, amount=exact_sum(r["amount"] for r in group), order_count=1,
            label="此單品項明細原值／幣別未記錄" if line_basis else "此單原值／幣別未記錄")
            for identity, group in sorted(grouped.items(), key=lambda pair: (pair[1][0]["order_no"], pair[0]))]
    grouped = defaultdict(list)
    for row in money:
        grouped[row["currency_id"]].append(row)
    return [dict(currency_id=identity, currency_code=group[0]["currency_code"], currency_name=group[0]["currency_name"],
        amount=exact_sum(row["amount"] for row in group), order_count=len({row["order_id"] for row in group}),
        label="品項明細金額" if line_basis else "採購訂單金額")
        for identity, group in sorted(grouped.items(), key=lambda pair: (pair[1][0]["currency_code"] or "", pair[0] or ""))]


def counts(records, area):
    return [dict(key=key, label=label, count=len({row.get("_counts", {}).get(key) for row in records
        if row.get("_counts", {}).get(key) is not None})) for key, label in COUNTS[area]]


def breakdown(records, area, key, label, identity, code, name, *, line_basis=False):
    grouped = defaultdict(list)
    for row in records:
        if row.get(identity) is not None:
            grouped[row[identity]].append(row)
    output = []
    for value, group in grouped.items():
        group_counts = counts(group, area)
        if identity == "item_id" and area in ("purchasing", "sales"):
            # Headers are deliberately itemless. Count distinct matching demand
            # headers from their lines, without allocating full header money.
            next(c for c in group_counts if c["key"] == "order_count")["count"] = len({
                r["order_id"] for r in group if r["source_kind"].endswith("ORDER_LINE")})
        output.append(dict(id=value, code=group[0].get(code) or str(value), name=group[0].get(name) or str(value),
            counts=group_counts, amounts=amount_groups(group, area, line_basis=line_basis), quantities=quantity_groups(group, area)))
    return dict(key=key, label=label, rows=sorted(output, key=lambda row: (row["code"], row["id"])))


def business_statistics(session, *, area="purchasing", date_from=None, date_to=None, party_id=None,
                        item_id=None, document_no="", work_order_id=None, offset=0, limit=50):
    if area not in QUANTITIES:
        raise HTTPException(422, "統計區域不存在。")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "日期起日不可晚於迄日。")
    if area == "manufacturing" and party_id:
        raise HTTPException(422, "製造統計不適用供應商或客戶篩選。")
    if area != "manufacturing" and work_order_id:
        raise HTTPException(422, "工單篩選僅適用製造統計。")
    options = statistics_options(session, area)
    for identity, option, message in ((party_id, "parties", "供應商／客戶不存在，請重新選擇。"),
        (item_id, "items", "品項不存在或不適用此統計區域。"), (work_order_id, "work_orders", "工單不存在，請重新選擇。")):
        if identity is not None and str(identity) not in {row["id"] for row in options[option]}:
            raise HTTPException(422, message)
    filters = dict(date_from=date_from.isoformat() if isinstance(date_from, date) else date_from,
        date_to=date_to.isoformat() if isinstance(date_to, date) else date_to,
        party_id=str(party_id) if party_id else None, item_id=str(item_id) if item_id else None,
        work_order_id=str(work_order_id) if work_order_id else None, document_no=document_no.strip())
    sources = facts_for(session, area, filters)
    records = [row for group in sources.values() for row in group]
    notes = ["各數量依實際基本單位分組，不跨單位相加；主檔代碼與名稱使用目前資料。",
        "日期採各指標自己的事件基準；時間戳記以 UTC 日曆日（含起日與迄日全天）篩選，業務日期原值另列。",
        "各來源明細獨立分頁；摘要與分組均計算完整篩選結果，不受目前頁面影響。",
        "單號以字面子字串比對該來源及其列示的上游單號；每個來源獨立套用日期與單號條件。"]
    if area in ("purchasing", "sales"):
        notes += ["訂單依訂單業務日期篩選；金額為符合條件訂單的完整明細金額加總，每張訂單只計一次。品項篩選只限制數量明細，整單金額仍含其他品項。",
            "目前未交是符合訂單日期／單號條件之訂單明細的目前差額，扣除全部歷史已過帳毛收貨／出貨，退貨不重開未交；不是所選期間期末的歷史餘額。",
            "期間淨收貨／淨實體出貨是同期間各獨立事件數量的差額，可能因原收貨／出貨在較早期間而為負；不代表目前庫存、歷史期末餘額、會計成本、收入或收付款。",
            "品項分組金額僅計該品項明細原值，與整張訂單金額的粒度不同。"]
        if area == "sales":
            notes += ["現有銷售單未保存幣別，逐單列出原始金額，不採用客戶預設幣別，也不跨訂單加總未明幣別金額。",
                "已完成客退包含已回庫與不回庫兩種結果；淨實體出貨只扣實際回庫客退，不回庫結果不產生正庫存。"]
    else:
        notes += ["工單沒有通用業務日期；計畫數量依預計開工日篩選，狀態依原有工單狀態分組。",
            "完工數量僅計已確認回報，日期使用完工確認時間；成品入庫另依真實庫存帳與過帳時間計算，兩者不相加。",
            "品項篩選比對每個來源的實際品項：成品限制計畫／完工／成品入庫，原料限制領退料；需查看整張工單的成品與原料時請使用工單篩選。",
            "期間淨領料只是期間已過帳領料減退料，原領料可能在較早期間，所以差額可為負；不是現有庫存、歷史餘額、每件產品的精確耗用分攤或成本。未建立工時、成本、毛利或利潤指標。"]
    groups = [breakdown(records, area, "items", "依品項（成品與原料分列）" if area == "manufacturing" else "依品項（品項明細金額）", "item_id", "item_code", "item_name", line_basis=True)]
    if area in ("purchasing", "sales"):
        groups.insert(0, breakdown(records, area, "parties", "依供應商" if area == "purchasing" else "依客戶", "party_id", "party_code", "party_name"))
    else:
        groups += [breakdown(records, area, "work_orders", "依工單", "work_order_id", "work_order_no", "work_order_no"),
            breakdown(sources["work_orders"], area, "statuses", "工單目前狀態（原值）", "state", "state", "state")]
    detail = []
    for key, label in SOURCES[area]:
        source = sorted(sources[key], key=lambda row: (row["event_date"] or "", row["document_no"] or "", row["source_id"] or "", row.get("line_no", 0), row["id"]))
        detail.append(dict(key=key, label=label, total=len(source), offset=offset, limit=limit,
            rows=[{k: value for k, value in row.items() if not k.startswith("_")} for row in source[offset:offset+limit]]))
    quantities = quantity_groups(records, area)
    indicators = []
    if area == "manufacturing":
        for unit in quantities:
            denominator = unit.get("confirmed_total_qty_base", "0")
            if denominator != "0" and not denominator.startswith("-"):
                numerator = unit["confirmed_good_qty_base"]
                indicators.append(dict(key="confirmed_good_share", label="確認良品占已確認回報總量（精確比值）",
                    unit_id=unit["unit_id"], unit_code=unit["unit_code"], numerator=numerator, denominator=denominator,
                    expression=numerator + " / " + denominator, note="分子與分母為同單位的已確認回報；未換算百分比或取位。"))
    return dict(read_only=True, area=area, as_of=timestamp(), scope=dict(**filters,
        date_basis="訂單：業務日期；工單計畫：預計開工日；確認完工：確認時間 UTC；實體異動：過帳時間 UTC；客退：完成時間 UTC。",
        amount_basis="符合條件完整訂單；品項分組僅該品項明細；銷售幣別未記錄所以只逐單列示。",
        outstanding_basis="CURRENT_FOR_MATCHING_ORDERS; ORDERED_MINUS_ALL_HISTORY_GROSS_POSTINGS; RETURNS_DO_NOT_REOPEN",
        timezone="UTC", summary_scope="COMPLETE_FILTERED_RESULTS"), notes=notes,
        summary=dict(counts=counts(records, area), amounts=amount_groups(records, area), quantities=quantities, indicators=indicators),
        breakdowns=groups, details=detail)


def install_business_statistics_routes(app):
    @app.get("/api/business-statistics")
    def inquire(request: Request, response: Response,
                area: Literal["purchasing", "manufacturing", "sales"] = "purchasing",
                mode: Literal["report", "options"] = "report", date_from: date | None = None,
                date_to: date | None = None, party_id: UUID | None = None, item_id: UUID | None = None,
                document_no: str = "", work_order_id: UUID | None = None,
                offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        response.headers["Cache-Control"] = "no-store"
        with read_snapshot(request.app.state.db) as session:
            if mode == "options":
                return statistics_options(session, area)
            return business_statistics(session, area=area, date_from=date_from, date_to=date_to,
                party_id=party_id, item_id=item_id, document_no=document_no, work_order_id=work_order_id,
                offset=offset, limit=limit)
