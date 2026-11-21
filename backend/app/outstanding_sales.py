"""Snapshot-consistent sales inquiry; returns never reopen original demand."""
from collections import defaultdict
from uuid import UUID
from fastapi import Request
from sqlalchemy import select
from .models import Customer, Item, Unit
from .sales_models import SalesOrder, SalesLine
from .sales_shipments import shipped_quantity
from .customer_returns import returned_quantity
from .decimal_text import exact_product, exact_difference, exact_sum
from .stock_ledger import compare
from .purchase_schemas import calendar_date


def shipment_sources(session, line_id):
    # Same genuine movement/lineage criteria as the authoritative shipment reader.
    return [dict(row) for row in session.connection().exec_driver_sql("""
        SELECT r.id,r.document_no,r.business_date,r.quantity,r.position_id,r.warehouse_id,r.lot_id
        FROM sales_shipment r
        JOIN sales_shipment_posting p ON p.shipment_id=r.id AND p.posting_state='POSTED'
        JOIN sales_shipment_entry e ON e.shipment_id=r.id AND e.posting_id=p.id
        JOIN stock_position s ON s.id=e.position_id
        JOIN sales_order_line l ON l.id=r.sales_order_line_id
        JOIN sales_order o ON o.id=l.sales_order_id
        WHERE l.id=? AND l.sales_order_id=r.sales_order_id AND o.customer_id=r.customer_id
          AND l.item_id=r.product_id AND l.unit_id=r.unit_id
          AND s.id=r.position_id AND s.item_id=r.product_id AND s.warehouse_id=r.warehouse_id
          AND s.lot_id IS r.lot_id AND e.leg_no=1 AND e.qty_base_delta=erp_decimal_neg(r.quantity)
          AND erp_decimal_cmp(r.quantity,'0')>0 AND r.row_version=p.source_version+1
          AND e.created_by=p.created_by
        ORDER BY r.business_date,r.document_no,r.id
    """, (line_id,)).mappings()]


def return_sources(session, shipment_id):
    return [dict(row) for row in session.connection().exec_driver_sql("""
        SELECT r.id,r.document_no,r.business_date,r.quantity,r.restock_eligible,f.finalization_state
        FROM customer_return r JOIN customer_return_finalization f ON f.return_id=r.id
        JOIN sales_shipment s ON s.id=r.shipment_id
        WHERE s.id=? AND r.shipment_version=s.row_version AND r.row_version=f.source_version+1
          AND r.sales_order_line_id=s.sales_order_line_id AND r.sales_order_id=s.sales_order_id
          AND r.customer_id=s.customer_id AND r.product_id=s.product_id AND r.unit_id=s.unit_id
          AND r.position_id=s.position_id AND r.warehouse_id=s.warehouse_id AND r.lot_id IS s.lot_id
          AND erp_decimal_cmp(r.quantity,'0')>0
          AND ((f.finalization_state='RECORDED_NO_RESTOCK' AND r.restock_eligible=0
            AND NOT EXISTS(SELECT 1 FROM customer_return_entry e WHERE e.finalization_id=f.id))
           OR (f.finalization_state='POSTED' AND r.restock_eligible=1
            AND EXISTS(SELECT 1 FROM customer_return_entry e WHERE e.finalization_id=f.id
              AND e.return_id=r.id AND e.position_id=r.position_id AND e.leg_no=1
              AND e.qty_base_delta=r.quantity AND e.created_by=f.created_by)))
        ORDER BY r.business_date,r.document_no,r.id
    """, (shipment_id,)).mappings()]


QUANTITIES = ['ordered_qty_base', 'gross_shipped_qty_base', 'outstanding_qty_base', 'finalized_returned_qty_base']


def group_units(lines):
    groups = defaultdict(list)
    for line in lines: groups[line['unit_id']].append(line)
    return [dict(unit_id=unit, unit_code=rows[0]['unit_code'], unit_name=rows[0]['unit_name'],
        line_count=len(rows), **{key: exact_sum(row[key] for row in rows) for key in QUANTITIES})
        for unit, rows in sorted(groups.items(), key=lambda pair: (pair[1][0]['unit_code'], pair[0]))]


def install_outstanding_sales_routes(app, fail):
    @app.get('/api/outstanding-sales')
    def inquiry(request: Request, document_no: str = '', customer_id: UUID | None = None,
                product_id: UUID | None = None, date_from: str | None = None,
                date_to: str | None = None, outstanding_only: bool = False):
        try:
            start = calendar_date(date_from) if date_from else None
            end = calendar_date(date_to) if date_to else None
        except ValueError: fail(422, '日期格式不正確，請使用有效日曆日期。')
        if start and end and start > end: fail(422, '起始日期不能晚於結束日期。')
        statement = select(SalesOrder).order_by(SalesOrder.order_date.desc(), SalesOrder.document_no, SalesOrder.id)
        term = document_no.strip()
        if term: statement = statement.where(SalesOrder.document_no.contains(term, autoescape=True))
        if customer_id: statement = statement.where(SalesOrder.customer_id == str(customer_id))
        if product_id: statement = statement.where(select(SalesLine.id).where(
            SalesLine.sales_order_id == SalesOrder.id, SalesLine.item_id == str(product_id)).exists())
        if start: statement = statement.where(SalesOrder.order_date >= start)
        if end: statement = statement.where(SalesOrder.order_date <= end)
        # One read transaction: every detail, source, amount and grouped total
        # belongs to the same SQLite snapshot. No writes or authorization grant.
        with request.app.state.db.read() as session:
            if customer_id and session.get(Customer, str(customer_id)) is None:
                fail(422, '客戶不存在，請選擇既有客戶。')
            if product_id:
                product = session.get(Item, str(product_id))
                if not product or product.item_kind != 'FINISHED': fail(422, '成品不存在，請選擇既有成品。')
            orders = []
            for header in session.scalars(statement):
                customer = session.get(Customer, header.customer_id)
                lines = []
                for line in session.scalars(select(SalesLine).where(SalesLine.sales_order_id == header.id).order_by(SalesLine.line_no, SalesLine.id)):
                    item = session.get(Item, line.item_id); unit = session.get(Unit, line.unit_id)
                    shipped = shipped_quantity(session, line.id)
                    shipments = shipment_sources(session, line.id)
                    for shipment in shipments:
                        shipment['finalized_returned_qty_base'] = returned_quantity(session, shipment['id'])
                        shipment['returns'] = return_sources(session, shipment['id'])
                    outstanding = exact_difference(line.ordered_qty, shipped)
                    negative = compare(outstanding, '0') < 0
                    lines.append(dict(id=line.id, sales_order_id=header.id, line_no=line.line_no,
                        row_version=line.row_version, product_id=item.id, product_code=item.code, product_name=item.name,
                        description_snapshot=line.description_snapshot, unit_id=unit.id, unit_code=unit.code, unit_name=unit.name,
                        ordered_qty_base=line.ordered_qty, unit_price=line.unit_price,
                        line_amount=exact_product(line.ordered_qty, line.unit_price), gross_shipped_qty_base=shipped,
                        outstanding_qty_base=outstanding,
                        finalized_returned_qty_base=exact_sum(s['finalized_returned_qty_base'] for s in shipments),
                        has_outstanding=compare(outstanding, '0') > 0, matches_product_filter=not product_id or item.id == str(product_id),
                        diagnostic='未出貨差額為負，請核對原訂單與歷史出貨；未自動歸零或更正資料。' if negative else None,
                        shipments=shipments))
                positive = any(line['has_outstanding'] for line in lines)
                if outstanding_only and not positive: continue
                orders.append(dict(id=header.id, document_no=header.document_no, order_date=header.order_date,
                    state=header.state, row_version=header.row_version, customer_id=customer.id,
                    customer_code=customer.code, customer_name=customer.name, line_count=len(lines),
                    total_amount=exact_sum(line['line_amount'] for line in lines), has_outstanding=positive,
                    quantity_totals=group_units(lines), lines=lines))
            all_lines = [line for order in orders for line in order['lines']]
            return dict(total_orders=len(orders), total_lines=len(all_lines), orders=orders,
                quantity_totals=group_units(all_lines), totals_scope='FULL_MATCHING_ORDERS',
                diagnostics=[dict(order_id=o['id'],line_id=l['id'],message=l['diagnostic'])
                    for o in orders for l in o['lines'] if l['diagnostic']])
