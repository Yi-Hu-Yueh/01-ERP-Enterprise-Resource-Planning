"""Read-only, snapshot-consistent purchasing inquiry; independent source aggregates."""
from collections import defaultdict
from uuid import UUID

from fastapi import Query, Request
from pydantic import BaseModel

from .purchase_schemas import calendar_date


class QuantityTotals(BaseModel):
    unit_id: str
    unit_code: str
    unit_name: str
    ordered_qty_base: str
    gross_received_qty_base: str
    outstanding_qty_base: str
    posted_return_qty_base: str
    net_retained_qty_base: str
    line_count: int


class ReceiptSource(BaseModel):
    receipt_id: str
    document_no: str
    business_date: str
    receipt_line_id: str
    receipt_line_no: int
    quantity_base: str
    posted_return_qty_base: str


class InquiryLine(BaseModel):
    id: str
    purchase_order_id: str
    line_no: int
    row_version: int
    item_id: str
    item_code: str
    item_name: str
    description_snapshot: str
    unit_id: str
    unit_code: str
    unit_name: str
    ordered_qty: str
    base_factor_snapshot: str
    ordered_qty_base: str
    unit_price: str
    line_amount: str
    due_date: str
    preferred_warehouse_id: str
    warehouse_code: str
    warehouse_name: str
    gross_received_qty_base: str
    outstanding_qty_base: str
    posted_return_qty_base: str
    net_retained_qty_base: str
    has_outstanding: bool
    receipts: list[ReceiptSource]


class InquiryOrder(BaseModel):
    id: str
    document_no: str
    order_date: str
    row_version: int
    supplier_id: str
    supplier_code: str
    supplier_name: str
    supplier_name_snapshot: str
    currency_id: str
    currency_code: str
    currency_name: str
    total_amount: str
    line_count: int
    has_outstanding: bool
    quantity_totals: list[QuantityTotals]
    lines: list[InquiryLine]


class CurrencyTotals(BaseModel):
    currency_id: str
    currency_code: str
    currency_name: str
    total_amount: str
    order_count: int


class InquiryResult(BaseModel):
    page: int
    page_size: int
    total_orders: int
    total_lines: int
    has_next: bool
    quantity_totals: list[QuantityTotals]
    currency_totals: list[CurrencyTotals]
    orders: list[InquiryOrder]


# Pre-aggregate each origin independently. Never join raw receipt and return fan-outs.
def ctes(where, outstanding_only):
    return f"""WITH candidates AS (
      SELECT p.* FROM purchase_order p JOIN supplier s ON s.id=p.supplier_id WHERE {where}
    ), candidate_lines AS (
      SELECT l.* FROM purchase_order_line l JOIN candidates p ON p.id=l.purchase_order_id
    ), posted_receipts AS (
      SELECT r.id AS receipt_line_id,r.line_no AS receipt_line_no,r.purchase_order_line_id,
        d.id AS receipt_id,d.document_no,d.business_date,d.supplier_id,
        erp_decimal_mul(r.quantity,r.base_factor_snapshot) AS quantity_base
      FROM candidate_lines l JOIN candidates p ON p.id=l.purchase_order_id
      JOIN stock_document_line r ON r.purchase_order_line_id=l.id AND r.item_id=l.item_id AND r.unit_id=l.unit_id
        AND r.base_factor_snapshot=l.base_factor_snapshot
      JOIN stock_document d ON d.id=r.stock_document_id AND d.purchase_order_id=p.id AND d.supplier_id=p.supplier_id
        AND d.document_kind='PURCHASE_RECEIPT'
      JOIN stock_posting sp ON sp.stock_document_id=d.id AND sp.posting_state='POSTED'
    ), received AS (
      SELECT purchase_order_line_id,erp_decimal_sum(quantity_base) AS quantity FROM posted_receipts GROUP BY purchase_order_line_id
    ), returns_by_origin AS (
      SELECT r.origin_line_id,erp_decimal_sum(r.quantity) AS quantity
      FROM posted_receipts source JOIN supplier_return_line r ON r.origin_line_id=source.receipt_line_id
      JOIN supplier_return d ON d.id=r.supplier_return_id AND d.supplier_id=source.supplier_id
      JOIN supplier_return_posting sp ON sp.supplier_return_id=d.id AND sp.posting_state='POSTED'
      GROUP BY r.origin_line_id
    ), returned AS (
      SELECT source.purchase_order_line_id,erp_decimal_sum(r.quantity) AS quantity FROM posted_receipts source
      JOIN returns_by_origin r ON r.origin_line_id=source.receipt_line_id GROUP BY source.purchase_order_line_id
    ), facts AS (
      SELECT l.*,i.code AS item_code,i.name AS item_name,u.code AS unit_code,u.name AS unit_name,
        w.code AS warehouse_code,w.name AS warehouse_name,
        erp_decimal_mul(l.ordered_qty,l.base_factor_snapshot) AS ordered_qty_base,
        erp_decimal_mul(l.ordered_qty,l.unit_price) AS line_amount,
        COALESCE(g.quantity,'0') AS gross_received_qty_base,COALESCE(r.quantity,'0') AS posted_return_qty_base,
        erp_decimal_sub(erp_decimal_mul(l.ordered_qty,l.base_factor_snapshot),COALESCE(g.quantity,'0')) AS outstanding_qty_base,
        erp_decimal_sub(COALESCE(g.quantity,'0'),COALESCE(r.quantity,'0')) AS net_retained_qty_base
      FROM candidate_lines l JOIN item i ON i.id=l.item_id JOIN unit u ON u.id=l.unit_id
      JOIN warehouse w ON w.id=l.preferred_warehouse_id
      LEFT JOIN received g ON g.purchase_order_line_id=l.id LEFT JOIN returned r ON r.purchase_order_line_id=l.id
    ), filtered_orders AS (
      SELECT p.* FROM candidates p WHERE {"EXISTS(SELECT 1 FROM facts f WHERE f.purchase_order_id=p.id AND erp_decimal_cmp(f.outstanding_qty_base,'0')>0)" if outstanding_only else '1=1'}
    ), filtered_facts AS (
      SELECT f.* FROM facts f JOIN filtered_orders p ON p.id=f.purchase_order_id
    ), amounts AS (
      SELECT purchase_order_id,erp_decimal_sum(line_amount) AS total_amount,count(*) AS line_count
      FROM filtered_facts GROUP BY purchase_order_id
    ), paged_orders AS (
      SELECT * FROM filtered_orders ORDER BY order_date DESC,document_no COLLATE BINARY,id LIMIT :limit OFFSET :offset
    ) """


QUANTITY_COLUMNS = ["ordered_qty_base", "gross_received_qty_base", "outstanding_qty_base", "posted_return_qty_base", "net_retained_qty_base"]


def quantity_select():
    return ",".join("erp_decimal_sum("+name+") AS "+name for name in QUANTITY_COLUMNS)


def install_outstanding_routes(app, fail):
    @app.get("/api/purchase-outstanding", response_model=InquiryResult)
    def inquiry(request: Request, q: str = "", document_no: str = "", supplier_id: UUID | None = None,
                item_id: UUID | None = None, date_from: str | None = None, date_to: str | None = None,
                outstanding_only: bool = False, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1)):
        try:
            start = calendar_date(date_from) if date_from is not None else None
            end = calendar_date(date_to) if date_to is not None else None
        except ValueError:
            fail(422, "日期格式不正確，請使用有效的日曆日期。")
        if start and end and start > end:
            fail(422, "起始日期不能晚於結束日期。")
        params = {"limit": page_size, "offset": (page-1)*page_size}
        conditions = []

        def pattern(value):
            return "%"+value.replace("/", "//").replace("%", "/%").replace("_", "/_")+"%"

        if q:
            params["q"] = pattern(q)
            conditions.append("""(p.document_no LIKE :q ESCAPE '/' OR p.supplier_name_snapshot LIKE :q ESCAPE '/'
              OR s.code LIKE :q ESCAPE '/' OR s.name LIKE :q ESCAPE '/' OR EXISTS
              (SELECT 1 FROM purchase_order_line l JOIN item i ON i.id=l.item_id WHERE l.purchase_order_id=p.id
               AND (i.code LIKE :q ESCAPE '/' OR i.name LIKE :q ESCAPE '/' OR l.description_snapshot LIKE :q ESCAPE '/')))""")
        if document_no:
            params["document"] = pattern(document_no)
            conditions.append("p.document_no LIKE :document ESCAPE '/'")
        if supplier_id:
            params["supplier"] = str(supplier_id)
            conditions.append("p.supplier_id=:supplier")
        if item_id:
            params["item"] = str(item_id)
            conditions.append("EXISTS(SELECT 1 FROM purchase_order_line l WHERE l.purchase_order_id=p.id AND l.item_id=:item)")
        if start:
            params["start"] = start
            conditions.append("p.order_date>=:start")
        if end:
            params["end"] = end
            conditions.append("p.order_date<=:end")
        sql = ctes(" AND ".join(conditions) or "1=1", outstanding_only)
        with request.app.state.db.read() as session:
            connection = session.connection()

            def rows(statement):
                return [dict(row) for row in connection.exec_driver_sql(sql+statement, params).mappings()]

            count = rows("SELECT count(*) AS total_orders,(SELECT count(*) FROM filtered_facts) AS total_lines FROM filtered_orders")[0]
            totals = rows("SELECT unit_id,unit_code,unit_name,"+quantity_select()+",count(*) AS line_count FROM filtered_facts GROUP BY unit_id ORDER BY unit_code COLLATE BINARY,unit_id")
            money = rows("""SELECT p.currency_id,c.code AS currency_code,c.name AS currency_name,
                erp_decimal_sum(COALESCE(a.total_amount,'0')) AS total_amount,count(*) AS order_count
                FROM filtered_orders p JOIN currency c ON c.id=p.currency_id LEFT JOIN amounts a ON a.purchase_order_id=p.id
                GROUP BY p.currency_id ORDER BY c.code COLLATE BINARY,p.currency_id""")
            orders = rows("""SELECT p.id,p.document_no,p.order_date,p.row_version,p.supplier_id,s.code AS supplier_code,
                s.name AS supplier_name,p.supplier_name_snapshot,p.currency_id,c.code AS currency_code,c.name AS currency_name,
                COALESCE(a.total_amount,'0') AS total_amount,COALESCE(a.line_count,0) AS line_count
                FROM paged_orders p JOIN supplier s ON s.id=p.supplier_id JOIN currency c ON c.id=p.currency_id
                LEFT JOIN amounts a ON a.purchase_order_id=p.id ORDER BY p.order_date DESC,p.document_no COLLATE BINARY,p.id""")
            lines = rows("SELECT f.* FROM filtered_facts f JOIN paged_orders p ON p.id=f.purchase_order_id ORDER BY f.purchase_order_id,f.line_no,f.id")
            sources = rows("""SELECT r.*,COALESCE(ret.quantity,'0') AS posted_return_qty_base FROM posted_receipts r
                JOIN candidate_lines l ON l.id=r.purchase_order_line_id JOIN paged_orders p ON p.id=l.purchase_order_id
                LEFT JOIN returns_by_origin ret ON ret.origin_line_id=r.receipt_line_id
                ORDER BY r.purchase_order_line_id,r.document_no COLLATE BINARY,r.receipt_line_no,r.receipt_line_id""")
            by_origin, by_order = defaultdict(list), defaultdict(list)
            for row in sources:
                by_origin[row["purchase_order_line_id"]].append(row)
            for row in lines:
                row["has_outstanding"] = not row["outstanding_qty_base"].startswith("-") and row["outstanding_qty_base"] != "0"
                row["receipts"] = by_origin[row["id"]]
                by_order[row["purchase_order_id"]].append(InquiryLine(**row))
            # Only page-sized rows are materialized; full-result totals were computed above in SQL.
            from .decimal_text import exact_sum
            for order in orders:
                detail = by_order[order["id"]]
                groups = defaultdict(list)
                for line in detail:
                    groups[line.unit_id].append(line)
                order["lines"] = detail
                order["has_outstanding"] = any(line.has_outstanding for line in detail)
                order["quantity_totals"] = [QuantityTotals(unit_id=unit,unit_code=group[0].unit_code,unit_name=group[0].unit_name,
                    line_count=len(group),**{key:exact_sum(getattr(line,key) for line in group) for key in QUANTITY_COLUMNS})
                    for unit,group in sorted(groups.items(), key=lambda pair:(pair[1][0].unit_code,pair[0]))]
            return InquiryResult(page=page,page_size=page_size,total_orders=count["total_orders"],total_lines=count["total_lines"],
                has_next=page*page_size<count["total_orders"],quantity_totals=totals,currency_totals=money,orders=orders)
