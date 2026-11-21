"""Identity-based, directional manufacturing genealogy over persisted records.

Direct FKs, shared-work-order associations and pooled sources are separate facts.
Association/context edges never drive the manufacturing traversal. In particular,
sharing a supplier, customer, item, warehouse or an upstream input lot cannot
silently broaden a selected lot's manufacturing scope.
"""
from collections import defaultdict, deque

from fastapi import HTTPException
from sqlalchemy import text

from .inventory_traceability import all_movements, timestamp
from .decimal_text import exact_sum


EVIDENCE = {"DIRECT": "已驗證直接關聯", "WORK_ORDER": "已驗證工單層級關聯",
            "POOLED": "共同批號來源", "UNPROVEN": "無法由現有紀錄精確確認"}
TABLES = {
    "inventory_lot": ("庫存批號", "item_id,lot_code,origin_kind,supplier_id,supplier_lot_code,manufactured_date,expiry_date,traceability_note"),
    "stock_position": ("倉庫庫存位置", "item_id,warehouse_id,lot_id"),
    "supplier": ("供應商", "code,name"), "customer": ("客戶", "code,name"),
    "purchase_order": ("採購單", "document_no,supplier_id,order_date"),
    "purchase_order_line": ("採購明細", "purchase_order_id,line_no,item_id,unit_id,ordered_qty,base_factor_snapshot"),
    "stock_document": ("採購入庫單", "document_no,purchase_order_id,supplier_id,business_date"),
    "stock_document_line": ("採購入庫明細", "stock_document_id,line_no,purchase_order_line_id,item_id,quantity,unit_id,base_factor_snapshot,warehouse_id,lot_id"),
    "supplier_return": ("供應商退貨單", "document_no,supplier_id,business_date"),
    "supplier_return_line": ("供應商退貨明細", "supplier_return_id,line_no,origin_line_id,quantity"),
    "material_issue": ("生產領料單", "document_no,work_order_id,business_date"),
    "material_issue_line": ("生產領料明細", "material_issue_id,line_no,material_id,warehouse_id,position_id,unit_id,quantity"),
    "material_return": ("生產退料單", "document_no,work_order_id,business_date"),
    "material_return_line": ("生產退料明細", "material_return_id,line_no,origin_line_id,quantity"),
    "work_order": ("製造工單", "document_no,product_id,planned_qty_base,planned_start,planned_finish,approval_state"),
    "production_completion": ("已確認完工回報", "document_no,work_order_id,product_id,unit_id,report_date,good_qty_base,defective_qty_base,state,confirmed_at"),
    "finished_goods_receipt": ("成品入庫單", "document_no,completion_id,warehouse_id,lot_id,business_date,quantity"),
    "sales_shipment": ("已過帳出貨單", "document_no,sales_order_line_id,sales_order_id,customer_id,product_id,unit_id,position_id,warehouse_id,lot_id,business_date,quantity"),
    "sales_order_line": ("銷售明細", "sales_order_id,line_no,item_id,unit_id,ordered_qty"),
    "sales_order": ("銷售訂單", "document_no,customer_id,order_date,state"),
    "customer_return": ("客戶退貨單", "document_no,shipment_id,sales_order_line_id,sales_order_id,customer_id,product_id,unit_id,position_id,warehouse_id,lot_id,business_date,quantity,restock_eligible"),
    "inventory_transfer": ("倉庫調撥單", "document_no,source_position_id,destination_position_id,source_warehouse_id,destination_warehouse_id,item_id,unit_id,lot_id,business_date,quantity,state"),
    "inventory_adjustment": ("庫存調整單", "document_no,position_id,item_id,unit_id,warehouse_id,lot_id,business_date,direction,quantity,state,reason"),
    "stocktake": ("盤點差異單", "document_no,warehouse_id,business_date,state,snapshot_at"),
    "stocktake_line": ("盤點差異明細", "stocktake_id,position_id,item_id,unit_id,warehouse_id,lot_id,book_qty,counted_qty"),
}
SOURCE_TABLES = {"PURCHASE_RECEIPT": ("stock_document", "stock_document_line"),
    "SUPPLIER_RETURN": ("supplier_return", "supplier_return_line"),
    "MATERIAL_ISSUE": ("material_issue", "material_issue_line"),
    "MATERIAL_RETURN": ("material_return", "material_return_line"),
    "FINISHED_GOODS_RECEIPT": ("finished_goods_receipt", None),
    "SALES_SHIPMENT": ("sales_shipment", None), "CUSTOMER_RETURN": ("customer_return", None),
    "INVENTORY_TRANSFER": ("inventory_transfer", None),
    "INVENTORY_ADJUSTMENT": ("inventory_adjustment", None), "STOCKTAKE": ("stocktake", "stocktake_line")}


def evidence_options():
    return [dict(value=k, label=v) for k, v in EVIDENCE.items()]


def trace_options(session):
    lots = [dict(r) for r in session.execute(text("""SELECT l.id,l.lot_code,l.item_id,l.origin_kind,
        i.item_kind,i.code item_code,i.name item_name,u.id unit_id,u.code unit_code,u.name unit_name
        FROM inventory_lot l JOIN item i ON i.id=l.item_id JOIN unit u ON u.id=i.base_unit_id
        WHERE i.item_kind IN ('RAW','FINISHED') ORDER BY i.item_kind,i.code,l.lot_code,l.id""")).mappings()]
    shipments = [dict(r) for r in session.execute(text("""SELECT s.id,s.document_no,s.lot_id,l.lot_code,
        i.code item_code,i.name item_name,c.code customer_code,c.name customer_name,s.quantity,u.code unit_code
        FROM sales_shipment s JOIN item i ON i.id=s.product_id JOIN unit u ON u.id=s.unit_id
        JOIN customer c ON c.id=s.customer_id LEFT JOIN inventory_lot l ON l.id=s.lot_id
        WHERE EXISTS(SELECT 1 FROM physical_stock_ledger e
          WHERE e.domain='SALES_SHIPMENT' AND e.document_line_id=s.id)
        ORDER BY s.document_no,s.id""")).mappings()]
    return dict(read_only=True, as_of=timestamp(), evidence_types=evidence_options(), lots=lots, shipments=shipments)


class TraceGraph:
    def __init__(self, session, movements):
        self.session, self.movements = session, movements
        self.records, self.nodes, self.edges = {}, {}, {}
        self.items = {r["id"]: dict(r) for r in session.execute(text("""SELECT i.id,i.code,i.name,i.item_kind,
            u.id unit_id,u.code unit_code,u.name unit_name FROM item i JOIN unit u ON u.id=i.base_unit_id""")).mappings()}
        self.units = {r["id"]: r["code"] for r in session.execute(text("SELECT id,code FROM unit")).mappings()}
        self.warehouses = {r["id"]: dict(r) for r in session.execute(text("SELECT id,code,name,mrp_included FROM warehouse")).mappings()}

    def record(self, table, identity):
        if identity is None:
            return None
        key = (table, identity)
        if key not in self.records:
            fields = TABLES[table][1]
            row = self.session.execute(text("SELECT id," + fields + " FROM " + table + " WHERE id=:id"), {"id": identity}).mappings().first()
            self.records[key] = dict(row) if row else None
        return self.records[key]

    def node(self, table, identity, movement=None):
        row = self.record(table, identity)
        if row is None:
            return None
        key = table + ":" + identity
        if key not in self.nodes:
            item_id = row.get("item_id") or row.get("material_id") or row.get("product_id")
            item = self.items.get(item_id, {})
            unit = self.units.get(row.get("unit_id")) or item.get("unit_code")
            label = row.get("document_no") or row.get("lot_code") or row.get("code")
            if label is None:
                label = "第 " + str(row["line_no"]) + " 列" if "line_no" in row else identity
            self.nodes[key] = dict(id=key, kind=table.upper(), kind_label=TABLES[table][0],
                source_table=table, source_id=identity, label=label,
                item_code=item.get("code"), item_name=item.get("name"),
                quantity=row.get("quantity"), unit_code=unit, details=dict(row))
            if table == "production_completion":
                self.nodes[key]["details"]["inventory_effect"] = "完工確認本身不產生庫存異動；實體入庫以已過帳成品入庫單為準。"
            if table == "stock_position":
                self.nodes[key]["details"]["warehouse"] = self.warehouses.get(row["warehouse_id"])
        if movement:
            details = self.nodes[key]["details"]
            entries = details.setdefault("ledger_entries", [])
            if movement["id"] not in {e["id"] for e in entries}:
                entries.append({k: movement[k] for k in ("id", "posting_id", "posted_at", "qty_base_delta",
                    "unit_code", "position_id", "warehouse_id", "warehouse_code", "lot_id", "leg_no")})
            self.nodes[key]["unit_code"] = self.nodes[key]["unit_code"] or movement["unit_code"]
        return key

    def edge(self, source, target, relationship, note, *, evidence="DIRECT", lineage=True):
        if not source or not target:
            return
        key = "|".join((source, relationship, target))
        self.edges[key] = dict(id=key, **{"from": source, "to": target}, relationship=relationship,
            evidence=evidence, evidence_label=EVIDENCE[evidence], note=note,
            persisted_relation="DIRECT" if evidence == "DIRECT" else
                ("SHARED_WORK_ORDER" if evidence == "WORK_ORDER" else "SHARED_LOT"),
            lineage=lineage)

    def build(self):
        raw_receipts, raw_issues, fg_receipts, shipments = (defaultdict(set) for _ in range(4))
        issues_by_wo, completions_by_wo = defaultdict(set), defaultdict(set)
        for m in self.movements:
            table, line_table = SOURCE_TABLES[m["source_kind"]]
            document = self.node(table, m["source_id"], m)
            line = self.node(line_table, m["source_line_id"], m) if line_table else document
            row = self.record(table, m["source_id"])
            line_row = self.record(line_table, m["source_line_id"]) if line_table else row
            lot = self.node("inventory_lot", m["lot_id"])
            position = self.node("stock_position", m["position_id"], m)
            self.edge(lot, position, "LOT_POSITION", "stock_position.lot_id；倉庫位置與全球庫存批號是不同識別。", lineage=False)
            self.edge(position, line, "POSITION_MOVEMENT", "實際 physical_stock_ledger.position_id／entry_id。", lineage=False)
            if m["source_kind"] == "PURCHASE_RECEIPT":
                supplier = self.node("supplier", row["supplier_id"])
                po = self.node("purchase_order", row["purchase_order_id"])
                pol = self.node("purchase_order_line", line_row["purchase_order_line_id"])
                self.edge(supplier, po, "SUPPLIER_ORDER", "purchase_order.supplier_id。")
                self.edge(po, document, "ORDER_RECEIPT", "stock_document.purchase_order_id。")
                self.edge(document, line, "RECEIPT_LINE", "stock_document_line.stock_document_id。")
                self.edge(po, pol, "PURCHASE_ORDER_LINE", "purchase_order_line.purchase_order_id。", lineage=False)
                self.edge(pol, line, "ORDER_LINE_RECEIPT", "stock_document_line.purchase_order_line_id。", lineage=False)
                self.edge(supplier, document, "RECEIPT_SUPPLIER", "stock_document.supplier_id。")
                self.edge(line, lot, "RECEIPT_LOT", "stock_document_line.lot_id 與實際帳本位置的全球 lot_id；未證明後續領料來自哪一筆入庫。")
                if m["lot_id"]:
                    raw_receipts[m["lot_id"]].add(line)
            elif m["source_kind"] == "MATERIAL_ISSUE":
                wo = self.node("work_order", row["work_order_id"])
                self.edge(lot, line, "LOT_ISSUE", "material_issue_line.position_id → stock_position.lot_id；數量為真實批號領料，不是入庫來源分攤。")
                self.edge(line, document, "ISSUE_LINE", "material_issue_line.material_issue_id。")
                self.edge(document, wo, "ISSUE_WORK_ORDER", "material_issue.work_order_id；領料量不代表某筆完工的精確耗用量。")
                if m["lot_id"]:
                    raw_issues[m["lot_id"]].add(line)
                issues_by_wo[row["work_order_id"]].add(document)
            elif m["source_kind"] == "FINISHED_GOODS_RECEIPT":
                completion = self.record("production_completion", row["completion_id"])
                comp = self.node("production_completion", row["completion_id"])
                wo = self.node("work_order", completion["work_order_id"])
                self.edge(wo, comp, "WORK_ORDER_COMPLETION", "production_completion.work_order_id；此直接工單外鍵未記錄逐批投入至產出的數量分攤。")
                self.edge(comp, document, "COMPLETION_RECEIPT", "finished_goods_receipt.completion_id；實體數量來自已過帳入庫帳本。")
                self.edge(document, lot, "FINISHED_RECEIPT_LOT", "finished_goods_receipt.lot_id 與實際帳本位置的同一全球 lot_id。")
                completions_by_wo[completion["work_order_id"]].add(comp)
                if m["lot_id"]:
                    fg_receipts[m["lot_id"]].add(document)
            elif m["source_kind"] == "SALES_SHIPMENT":
                so = self.node("sales_order", row["sales_order_id"])
                sol = self.node("sales_order_line", row["sales_order_line_id"])
                customer = self.node("customer", row["customer_id"])
                self.edge(lot, document, "LOT_SHIPMENT", "sales_shipment.lot_id 及 position_id → stock_position.lot_id；未分攤至單一成品入庫來源。")
                self.edge(document, sol, "SHIPMENT_ORDER_LINE", "sales_shipment.sales_order_line_id。")
                self.edge(sol, so, "SALES_LINE_ORDER", "sales_order_line.sales_order_id。")
                self.edge(document, so, "SHIPMENT_ORDER", "sales_shipment.sales_order_id。")
                self.edge(document, customer, "SHIPMENT_CUSTOMER", "sales_shipment.customer_id。")
                self.edge(so, customer, "ORDER_CUSTOMER", "sales_order.customer_id。")
                if m["lot_id"]:
                    shipments[m["lot_id"]].add(document)
            elif m["source_kind"] == "SUPPLIER_RETURN":
                origin = self.node("stock_document_line", line_row["origin_line_id"])
                self.edge(origin, line, "RECEIPT_SUPPLIER_RETURN", "supplier_return_line.origin_line_id；供應商退貨不是生產耗用。", lineage=False)
                self.edge(line, document, "SUPPLIER_RETURN_LINE", "supplier_return_line.supplier_return_id。", lineage=False)
                self.edge(lot, line, "LOT_SUPPLIER_RETURN", "實際供應商退貨帳本位置的全球 lot_id。", lineage=False)
                self.edge(document, self.node("supplier", row["supplier_id"]), "RETURN_SUPPLIER", "supplier_return.supplier_id。", lineage=False)
            elif m["source_kind"] == "MATERIAL_RETURN":
                origin = self.node("material_issue_line", line_row["origin_line_id"])
                self.edge(origin, line, "ISSUE_MATERIAL_RETURN", "material_return_line.origin_line_id；退回原領料來源，不是新採購入庫。", lineage=False)
                self.edge(line, document, "MATERIAL_RETURN_LINE", "material_return_line.material_return_id。", lineage=False)
                self.edge(lot, line, "LOT_MATERIAL_RETURN", "實際生產退料帳本位置的全球 lot_id。", lineage=False)
                self.edge(document, self.node("work_order", row["work_order_id"]), "RETURN_WORK_ORDER", "material_return.work_order_id。", lineage=False)
            elif m["source_kind"] == "CUSTOMER_RETURN":
                self.edge(self.node("sales_shipment", row["shipment_id"]), document, "SHIPMENT_CUSTOMER_RETURN", "customer_return.shipment_id；保留原出貨與客戶來源，正數退庫是獨立異動。", lineage=False)
                self.edge(lot, document, "LOT_CUSTOMER_RETURN", "customer_return.lot_id 與實際退庫位置的同一全球 lot_id。", lineage=False)
            elif m["source_kind"] == "INVENTORY_TRANSFER":
                self.edge(lot, document, "LOT_TRANSFER", "inventory_transfer.lot_id；只改變倉庫位置，不產生新製造批號或成品入庫。", lineage=False)
                self.edge(self.node("stock_position", row["source_position_id"]), document, "TRANSFER_SOURCE", "inventory_transfer.source_position_id；原始負數帳本 leg。", lineage=False)
                self.edge(document, self.node("stock_position", row["destination_position_id"]), "TRANSFER_DESTINATION", "inventory_transfer.destination_position_id；目的正數帳本 leg，仍為同一全球 lot_id。", lineage=False)
            else:
                self.edge(lot, line, "LOT_" + m["source_kind"], "實際帳本位置關聯；此異動不是新的採購或製造來源。", lineage=False)
                if line_table:
                    self.edge(line, document, "STOCKTAKE_LINE", "stocktake_line.stocktake_id；只顯示帳本中真正產生的盤點差異。", lineage=False)

        # Include confirmed reports even when no physical FG receipt was posted.
        for r in self.session.execute(text("SELECT id,work_order_id FROM production_completion WHERE state='CONFIRMED' ORDER BY id")).mappings():
            comp = self.node("production_completion", r["id"])
            self.edge(self.node("work_order", r["work_order_id"]), comp, "WORK_ORDER_COMPLETION",
                "production_completion.work_order_id；直接工單關聯不證明逐批投入產出分攤，完工確認本身不產生庫存。")
            completions_by_wo[r["work_order_id"]].add(comp)
        for wo_id, issues in issues_by_wo.items():
            for issue in sorted(issues):
                for comp in sorted(completions_by_wo[wo_id]):
                    self.edge(issue, comp, "SHARED_WORK_ORDER", "歷史領料與已確認完工共用同一 work_order_id；僅工單層級關聯，不證明時間先後上的投入資格，也不證明較晚領料曾用於較早產出；無逐批、逐單位耗用分攤，未另訂時間分攤政策。",
                              evidence="WORK_ORDER", lineage=False)
        for lot_id, receipts in raw_receipts.items():
            if len(receipts) > 1:
                for receipt in sorted(receipts):
                    for issue in sorted(raw_issues[lot_id]):
                        self.edge(receipt, issue, "POOLED_RAW_PROVENANCE", "多筆歷史入庫共用同一全球批號；此共用批號關聯不證明時間先後上的來源資格，也不證明較晚入庫曾供應較早領料或產出；無法確認此筆領料各取自哪筆入庫，未計算比例或分攤量，未另訂時間分攤政策。",
                                  evidence="POOLED", lineage=False)
        for lot_id, receipts in fg_receipts.items():
            if len(receipts) > 1:
                for receipt in sorted(receipts):
                    for shipment in sorted(shipments[lot_id]):
                        self.edge(receipt, shipment, "POOLED_FINISHED_PROVENANCE", "多筆歷史成品入庫共用同一全球批號；此共用批號關聯不證明時間先後上的來源資格，也不證明較晚入庫曾供應較早出貨；無法確認此筆出貨各取自哪筆入庫，未計算分攤量，未另訂時間分攤政策。",
                                  evidence="POOLED", lineage=False)
        self.raw_receipts, self.raw_issues = raw_receipts, raw_issues
        self.fg_receipts, self.shipments = fg_receipts, shipments
        # Same persisted finalization/source predicates as
        # customer_returns.returned_quantity's RECORDED_NO_RESTOCK branch.
        # These genuine business returns have no physical ledger entry.
        self.nonrestock_returns = []
        no_restock = self.session.execute(text("""
            SELECT r.id,r.shipment_id,r.row_version,f.id finalization_id,
                   f.finalization_state,f.finalized_at,f.source_version
            FROM customer_return r
            JOIN customer_return_finalization f ON f.return_id=r.id
            JOIN sales_shipment s ON s.id=r.shipment_id
            JOIN sales_shipment_posting sp ON sp.shipment_id=s.id AND sp.posting_state='POSTED'
            JOIN sales_shipment_entry se ON se.posting_id=sp.id AND se.shipment_id=s.id
            WHERE s.row_version=sp.source_version+1 AND r.shipment_version=s.row_version
              AND se.position_id=s.position_id AND se.leg_no=1
              AND se.qty_base_delta=erp_decimal_neg(s.quantity) AND se.created_by=sp.created_by
              AND r.sales_order_line_id=s.sales_order_line_id AND r.sales_order_id=s.sales_order_id
              AND r.customer_id=s.customer_id AND r.product_id=s.product_id AND r.unit_id=s.unit_id
              AND r.position_id=s.position_id AND r.warehouse_id=s.warehouse_id AND r.lot_id IS s.lot_id
              AND r.row_version=f.source_version+1 AND erp_decimal_cmp(r.quantity,'0')>0
              AND f.finalization_state='RECORDED_NO_RESTOCK' AND r.restock_eligible=0
              AND NOT EXISTS(SELECT 1 FROM customer_return_entry e WHERE e.finalization_id=f.id)
            ORDER BY r.id
        """)).mappings()
        for finalized in no_restock:
            document = self.node("customer_return", finalized["id"])
            row = self.record("customer_return", finalized["id"])
            self.nodes[document]["details"].update({k: finalized[k] for k in
                ("finalization_id", "finalization_state", "finalized_at", "source_version", "row_version")})
            self.nodes[document]["details"]["inventory_effect"] = "已完成退貨登記／未退庫／無實體庫存異動"
            shipment = self.node("sales_shipment", finalized["shipment_id"])
            self.nonrestock_returns.append((shipment, document))
            self.edge(shipment, document, "SHIPMENT_CUSTOMER_RETURN",
                "customer_return.shipment_id 與真實完成紀錄 RECORDED_NO_RESTOCK；已完成退貨登記但未退庫，沒有實體庫存異動或庫存分錄。", lineage=False)
            for table, field, relation, note in (
                ("customer", "customer_id", "RETURN_CUSTOMER", "customer_return.customer_id 與原出貨相同；未退庫退貨不改變原客戶來源。"),
                ("sales_order", "sales_order_id", "RETURN_SALES_ORDER", "customer_return.sales_order_id 與原出貨相同；只保留既有訂單關聯。"),
                ("sales_order_line", "sales_order_line_id", "RETURN_SALES_LINE", "customer_return.sales_order_line_id 與原出貨相同；只保留既有明細關聯。")):
                self.edge(document, self.node(table, row[field]), relation, note, lineage=False)

    def closure(self, roots, reverse=False):
        adjacency = defaultdict(set)
        for e in self.edges.values():
            if e["lineage"]:
                source, target = (e["to"], e["from"]) if reverse else (e["from"], e["to"])
                adjacency[source].add(target)
        visited, pending = set(roots), deque(sorted(roots))
        while pending:
            for target in sorted(adjacency[pending.popleft()]):
                if target not in visited:
                    visited.add(target)
                    pending.append(target)
        return visited


def lot_traceability(session, direction, lot_id=None, shipment_id=None):
    movements = all_movements(session)
    graph = TraceGraph(session, movements)
    if lot_id:
        row = graph.record("inventory_lot", lot_id)
        if row is None or graph.items[row["item_id"]]["item_kind"] not in ("RAW", "FINISHED"):
            raise HTTPException(404, "真實庫存批號不存在，請重新選取；倉庫庫存位置識別不可代替批號識別。")
        if direction == "forward" and graph.items[row["item_id"]]["item_kind"] != "RAW":
            raise HTTPException(422, "正向製造追溯請選取原料庫存批號；成品請使用反向追溯。")
        root = graph.node("inventory_lot", lot_id)
    else:
        if not any(m["source_kind"] == "SALES_SHIPMENT" and m["source_id"] == shipment_id for m in movements):
            raise HTTPException(404, "已過帳出貨單不存在，請重新選取。")
        root = graph.node("sales_shipment", shipment_id)
    graph.build()
    selected = graph.closure([root], reverse=direction == "reverse")
    if direction == "forward":
        # Upstream provenance belongs to the selected input, never to every
        # supplier/party descendant. This cannot pull in unrelated work orders.
        selected |= graph.closure([root], reverse=True)
    else:
        if lot_id and graph.items[graph.record("inventory_lot", lot_id)["item_id"]]["item_kind"] == "FINISHED":
            # A selected FG lot also exposes its genuine shipments/customers.
            # Forward closure here starts only at this FG lot, not its raw-input
            # ancestors, so other WOs sharing an input cannot enter the result.
            selected |= graph.closure([root])
        # Customer and order facts of the selected/reached shipments are useful
        # reverse-trace context; do not traverse out of their customer nodes.
        for key in list(selected):
            if graph.nodes[key]["source_table"] == "sales_shipment":
                row = graph.record("sales_shipment", graph.nodes[key]["source_id"])
                for table, field in (("sales_order", "sales_order_id"), ("sales_order_line", "sales_order_line_id"), ("customer", "customer_id")):
                    selected.add(graph.node(table, row[field]))

    # No-restock returns are visible solely through an already reached original
    # shipment. Their customer/order endpoints cannot broaden the selected set,
    # and the physical movement collection remains entirely ledger-derived.
    selected |= {document for shipment, document in graph.nonrestock_returns if shipment in selected}

    # Real side effects are attached to reached lots/documents. Their endpoints
    # cannot restart a production traversal or spread through shared parties.
    reached_lots = {graph.nodes[k]["source_id"] for k in selected if graph.nodes[k]["source_table"] == "inventory_lot"}
    lot_movements = [m for m in movements if m["lot_id"] in reached_lots]
    reached_wo_ids = {graph.nodes[k]["source_id"] for k in selected if graph.nodes[k]["source_table"] == "work_order"}
    allowed_context = set()
    for m in lot_movements:
        table, line_table = SOURCE_TABLES[m["source_kind"]]
        if m["source_kind"] in ("SUPPLIER_RETURN", "MATERIAL_RETURN", "CUSTOMER_RETURN", "INVENTORY_TRANSFER", "INVENTORY_ADJUSTMENT", "STOCKTAKE"):
            if m["source_kind"] == "MATERIAL_RETURN" and m["work_order_id"] not in reached_wo_ids:
                continue
            allowed_context.add(table + ":" + m["source_id"])
            if line_table:
                allowed_context.add(line_table + ":" + m["source_line_id"])
        allowed_context.add("stock_position:" + m["position_id"])
    selected |= allowed_context
    # Bound context enrichment: only direct edges within a real context document
    # or to its already-known origin; no undirected connected-component search.
    for e in graph.edges.values():
        if not e["lineage"] and e["evidence"] == "DIRECT" and (e["from"] in allowed_context or e["to"] in allowed_context):
            for key in (e["from"], e["to"]):
                table = graph.nodes[key]["source_table"]
                if table in ("supplier", "stock_position"):
                    selected.add(key)
                elif table in ("material_issue_line", "stock_document_line", "sales_shipment"):
                    # Only preserve origins that refer to an already reached lot.
                    row = graph.record(table, graph.nodes[key]["source_id"])
                    origin_lot = row.get("lot_id")
                    if table == "material_issue_line":
                        position = graph.record("stock_position", row["position_id"])
                        origin_lot = position["lot_id"] if position else None
                    if origin_lot in reached_lots:
                        selected.add(key)

    # Lot/position balances always include every committed entry. The movement
    # details below contain only selected genealogy or bounded context documents;
    # an unrelated WO sharing a raw input cannot appear as traced production.
    for key in selected:
        node = graph.nodes[key]
        if node["source_table"] in ("inventory_lot", "stock_position"):
            rows = [m for m in lot_movements if
                (m["lot_id"] == node["source_id"] if node["source_table"] == "inventory_lot" else m["position_id"] == node["source_id"])]
            node["details"]["on_hand_qty_base"] = exact_sum(m["qty_base_delta"] for m in rows)
    relevant_movements = [m for m in lot_movements
        if SOURCE_TABLES[m["source_kind"]][0] + ":" + m["source_id"] in selected]
    limitations = []
    def limitation(code, message, node_ids, evidence="UNPROVEN"):
        limitations.append(dict(code=code, evidence=evidence, evidence_label=EVIDENCE[evidence], message=message, node_ids=sorted(node_ids)))
    work_orders = {k for k in selected if graph.nodes[k]["source_table"] == "work_order"}
    if work_orders:
        limitation("NO_INPUT_OUTPUT_ALLOCATION", "無法由現有紀錄精確確認：歷史已過帳領料與完工／成品入庫可由真實工單關聯，但共用工單不證明時間先後上的投入資格，也不證明較晚領料曾用於較早產出。現有紀錄未記錄原料逐批至產出逐批或逐單位耗用分攤；領退料量不是某成品批號的精確消耗量，未另訂時間分攤政策。", work_orders)
    for identity in sorted(reached_lots):
        key = "inventory_lot:" + identity
        item_kind = graph.items[graph.record("inventory_lot", identity)["item_id"]]["item_kind"]
        sources = graph.raw_receipts[identity] if item_kind == "RAW" else graph.fg_receipts[identity]
        if len(sources) > 1:
            limitation("POOLED_LOT_SOURCES", "同一全球庫存批號含多筆歷史入庫來源；共用批號不證明時間先後上的來源資格，也不證明較晚入庫曾供應較早領料、產出或出貨。無法由現有紀錄精確確認各次領料／出貨各取自哪一筆來源；各單據數量是各自實際紀錄，不是來源分攤，未另訂時間分攤政策。", {key} | sources, evidence="POOLED")
        if not sources:
            limitation("NO_POSTED_LOT_SOURCE", "無法由現有紀錄精確確認：此全球批號未找到已過帳採購或成品入庫來源；未由批號文字推測或補造關聯。", {key})
    if direction == "forward" and not any(graph.nodes[k]["source_table"] == "material_issue" for k in selected):
        limitation("NO_POSTED_ISSUE", "此原料批號沒有可驗證的已過帳生產領料，無法由現有紀錄精確確認其製造去向。", {root})
    if shipment_id and graph.record("sales_shipment", shipment_id)["lot_id"] is None:
        limitation("SHIPMENT_WITHOUT_LOT", "此出貨未記錄全球庫存批號，無法由現有紀錄精確確認製造批號；不以品名或批號文字猜測。", {root})
    visible_edges = [e for e in graph.edges.values() if e["from"] in selected and e["to"] in selected]
    return dict(read_only=True, as_of=timestamp(), direction=direction,
        selection=dict(lot_id=lot_id, shipment_id=shipment_id), root_node_ids=[root], evidence_types=evidence_options(),
        nodes=sorted((graph.nodes[k] for k in selected), key=lambda n: (n["source_table"], n["label"], n["id"])),
        edges=sorted(visible_edges, key=lambda e: e["id"]), limitations=limitations,
        movements=relevant_movements)
