from contextlib import asynccontextmanager
import os
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError

from .db import Database, DEMO_ACTOR_ID, DEMO_LABEL, ROOT, migrate
from .models import Category, Item, Unit
from .master_data import install_master_routes
from .supplier_offers import install_offer_routes
from .purchasing import install_purchase_routes
from .sales import install_sales_routes
from .sales_shipments import install_sales_shipment_routes
from .customer_returns import install_customer_return_routes
from .stocktakes import install_stocktake_routes
from .inventory_adjustments import install_adjustment_routes
from .inventory_transfers import install_transfer_routes
from .inventory_inquiry import install_inventory_inquiry_routes
from .inventory_traceability import install_inventory_traceability_routes
from .business_statistics import install_business_statistics_routes
from .receiving import install_receiving_routes
from .supplier_returns import install_return_routes
from .outstanding import install_outstanding_routes
from .outstanding_sales import install_outstanding_sales_routes
from .bom import install_bom_routes
from .routing import install_routing_routes
from .work_orders import install_work_order_routes
from .mrp import install_mrp_routes
from .material_issues import install_issue_routes
from .material_returns import install_material_return_routes
from .completions import install_completion_routes
from .finished_goods_receipts import install_finished_goods_routes
from .standalone_export import install_standalone_export_routes
from .sqlite_maintenance import install_sqlite_maintenance_routes
from .approvals import install_approval_routes
from .schemas import (CategoryCreate, CategoryOut, FinishedCreate, FinishedOut, FinishedUpdate,
                      RawCreate, RawOut, RawUpdate, UnitCreate, UnitOut)


def fail(status, message):
    raise HTTPException(status, detail=message)


def references(session, table, record_id, exclude_tables=()):
    """Honor actual incoming SQLite FKs, with explicit child exclusions for parent editing."""
    connection = session.connection()
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    for name in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars():
        if name in exclude_tables:
            continue
        for fk in connection.exec_driver_sql("PRAGMA foreign_key_list(" + quote(name) + ")").mappings():
            if fk["table"] == table and fk["to"] == "id":
                sql = "SELECT 1 FROM " + quote(name) + " WHERE " + quote(fk["from"]) + "=? LIMIT 1"
                if connection.exec_driver_sql(sql, (record_id,)).first():
                    return True
    return False


def create_app(database_path=None):
    path = Path(database_path or os.environ.get("ERP_DB_PATH", str(ROOT / "data" / "erp.sqlite"))).resolve()

    @asynccontextmanager
    async def lifespan(app):
        migrate(path)
        app.state.db = Database(path)
        app.state.db.seed_operator()
        try:
            yield
        finally:
            app.state.db.engine.dispose()

    app = FastAPI(title="Tiger ERP — Local Foundation", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"message": str(exc.detail)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        labels = {"code": "代碼", "name": "名稱", "category_id": "分類", "base_unit_id": "基本單位",
                  "row_version": "資料版次", "lot_tracking": "批次管理", "is_active": "啟用狀態", "currency_id": "預設幣別", "email": "電子郵件", "mrp_included": "納入MRP",
                  "supplier_id": "供應商", "item_id": "原料", "unit_price": "供料單價", "document_no": "採購單號",
                  "order_date": "採購日期", "due_date": "預計到貨日", "supplier_name_snapshot": "供應商名稱快照",
                  "description_snapshot": "品名規格快照", "preferred_warehouse_id": "預計收貨倉", "ordered_qty": "訂購數量",
                  "unit_id": "下單單位", "order_version": "採購單版次", "request_key": "請求識別",
                  "purchase_order_id": "來源採購單", "purchase_order_line_id": "來源採購明細",
                  "business_date": "收貨日期", "quantity": "實際接受量", "warehouse_id": "收貨倉庫",
                  "document_version": "入庫單版次", "lot_code": "庫存批號", "manufactured_date": "製造日期", "expiry_date": "失效日期"}
        if request.url.path.startswith("/api/business-statistics"):
            labels.update(area="統計領域", mode="查詢模式", party_id="供應商／客戶", item_id="原料或成品",
                          date_from="查詢日期起", date_to="查詢日期迄", work_order_id="製造工單",
                          document_no="單據號碼", offset="明細起始位置", limit="每頁筆數")
        if request.url.path.startswith(("/api/inventory-movements", "/api/lot-traceability")):
            labels.update(item_kind="品項類別", warehouse_id="查詢倉庫", q="品號或品名", lot_code="庫存批號",
                          date_from="過帳日期起", date_to="過帳日期迄", movement_type="異動類型", document_no="來源單號",
                          offset="起始筆數", limit="每頁筆數", lot_id="全域庫存批號", shipment_id="銷售出貨單", direction="追溯方向")
        if request.url.path.startswith("/api/inventory-inquiry"):
            labels.update(item_kind="品項類別", warehouse_id="查詢倉庫", q="品號或品名", lot_code="庫存批號")
        if request.url.path.startswith("/api/purchase-orders"):
            labels["unit_price"] = "採購單價"
        if request.url.path.startswith("/api/sales-orders"):
            labels.update(document_no="銷售單號", customer_id="客戶", order_date="銷售日期",
                          item_id="成品", unit_id="基本單位", ordered_qty="數量", unit_price="單價",
                          order_version="銷售單版次", body="銷售訂單資料")
        if request.url.path.startswith("/api/sales-shipments"):
            labels.update(document_no="銷售出貨單號",sales_order_line_id="來源銷售訂單明細",
                line_version="來源明細版次",order_version="來源訂單版次",position_id="出貨倉庫與批號",
                business_date="出貨日期",quantity="本次出貨數量",confirmation_token="出貨確認內容")
        if request.url.path.startswith("/api/customer-returns"):
            labels.update(document_no="客戶退貨單號",shipment_id="來源已過帳出貨單",shipment_version="來源出貨版次",
                business_date="退貨日期",quantity="本次退貨數量",restock_eligible="是否確認可回庫",
                reason="退貨原因",confirmation_token="退貨確認內容")
        if request.url.path.startswith("/api/supplier-returns"):
            labels.update(document_no="退貨單號", business_date="退貨日期", quantity="退貨量",
                          document_version="退貨單版次", origin_line_id="原收貨明細")
        if request.url.path.startswith("/api/boms"):
            labels.update(body="BOM 表頭與有效日期範圍", product_id="成品", revision="BOM 版號", output_basis_qty="產出基準量",
                          valid_from="有效起日", valid_to="有效迄日", material_id="原物料",
                          usage_qty_base="標準用量", purpose_note="用途說明", bom_version="BOM 版次")
        if request.url.path.startswith("/api/routings") or request.url.path.startswith("/api/operation-definitions"):
            labels.update(body="途程資料", product_id="成品", revision="途程版號", valid_from="有效起日",
                valid_to="有效迄日", sequence_no="步序（整數）", operation_id="作業",
                work_center_id="工作中心", setup_minutes="每批整備分鐘", run_minutes_per_base="每成品基本單位分鐘",
                routing_version="途程版次", instruction="作業說明")
        if request.url.path.startswith("/api/work-orders"):
            labels.update(body="工單及來源資料版次", document_no="製造工單號", product_id="成品",
                planned_qty_base="計畫產出量", planned_start="預計開工日", planned_finish="預計完工日",
                bom_id="BOM 來源", routing_id="途程來源", bom_version="BOM 資料版次", routing_version="途程資料版次")
        if request.url.path.startswith("/api/material-issues"):
            labels.update(document_no="領料單號",work_order_id="製造工單",work_order_version="工單版次",position_id="來源批次位置",material_id="原物料",quantity="領料量",document_version="領料單版次",bom_component_id="BOM 用料來源",bom_component_version="BOM 用料版次")
        if request.url.path.startswith("/api/material-returns"):
            labels.update(body="退料單資料",document_no="退料單號",business_date="退料日期",quantity="退料量",
                document_version="退料單版次",origin_line_id="原領料明細",origin_line_version="原領料明細版次",
                work_order_id="製造工單",work_order_version="工單版次",confirmation_token="退料確認內容")
        if request.url.path.startswith("/api/production-completions"):
            labels.update(body="完工回報資料",document_no="完工回報單號",report_date="實際回報日期",
                good_qty_base="良品量",defective_qty_base="不良品量",work_order_id="製造工單",
                work_order_version="工單版次",confirmation_token="完工確認內容")
        if request.url.path.startswith("/api/finished-goods-receipts"):
            labels.update(body="成品入庫資料",document_no="成品入庫單號",completion_id="已確認完工來源",
                completion_version="完工來源版次",warehouse_id="收貨倉庫",lot_id="製造成品批次",
                lot_code="成品庫存批號",business_date="入庫日期",quantity="本次入庫量",confirmation_token="成品入庫確認內容")
        if request.url.path.startswith("/api/stocktakes"):
            labels.update(document_no="盤點單號",warehouse_id="盤點倉庫",business_date="盤點日期",
                counted_qty="實際盤點數量（非負十進位文字或空白）",stocktake_version="盤點單版次",
                description="未登錄實物描述",confirmation_token="盤點結案確認內容")
        if request.url.path.startswith("/api/inventory-adjustments"):
            labels.update(document_no="庫存調整單號",business_date="調整日期",position_id="既有倉庫與批號位置",
                direction="調整方向",quantity="調整數量（十進位文字）",reason="調整原因",confirmation_token="調整過帳確認內容")
        if request.url.path.startswith("/api/inventory-transfers"):
            labels.update(document_no="倉庫調撥單號",business_date="調撥日期",source_position_id="來源倉庫與批號",
                destination_warehouse_id="目的倉庫",quantity="調撥數量（正值十進位文字）",reason="調撥原因",confirmation_token="調撥過帳確認內容")
        fields = [labels.get(str(e["loc"][-1]), str(e["loc"][-1])) for e in exc.errors()]
        return JSONResponse(status_code=422, content={"message": "資料格式不正確，請檢查：" + "、".join(fields)})

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        if "倉庫已凍結" in str(exc.orig) or "warehouse frozen" in str(exc.orig):
            message = "倉庫已凍結，請完成或明確取消整倉盤點後再執行庫存異動；本次操作已全部回滾。"
        elif request.url.path.startswith("/api/inventory-transfers"):
            message = "調撥單號、來源與目的倉庫、批號、數量、版次或授權不正確，或已有不可變過帳；本次兩側庫存異動已全部回滾，請重新讀取。"
        elif request.url.path.startswith("/api/inventory-adjustments"): 
            message = "調整單號、來源、方向、原因、版次或庫存不正確，或已有不可變過帳紀錄；本次操作已全部回滾，未儲存部分庫存異動。"
        elif request.url.path.startswith("/api/stocktakes"):
            message = "盤點單號、快照、盤數、版次或狀態不一致，或已結案而不可變更；本次操作已全部取消，凍結與庫存保留原狀。"
        elif request.url.path.startswith("/api/finished-goods-receipts"):
            message = "成品入庫來源、批號、版次或剩餘可入庫良品量不正確，或已有不可變過帳紀錄；操作已取消，沒有儲存部分庫存異動。"
        elif request.url.path.startswith("/api/production-completions"):
            message = "完工回報單號、工單、基本單位、數量或版次不正確，或已有不可變確認紀錄；操作已取消，未儲存部分內容。"
        elif request.url.path.startswith("/api/material-returns"):
            message = "退料單號、原領料來源、可退量、版次或過帳狀態不正確，或已有不可變紀錄；操作已取消，沒有儲存部分內容或入庫。"
        elif request.url.path.startswith("/api/material-issues") or "material_issue" in str(exc.orig):
            message = "領料單號、工單、原料、倉庫、批次或版次不一致，或已過帳／被引用；操作已取消，沒有儲存部分內容或扣庫。"
        elif request.url.path.startswith("/api/work-orders") or "work_order" in str(exc.orig):
            message = "工單號、日期、計畫量或來源關聯不正確，或資料已被工單引用；操作已取消，沒有儲存部分內容。"
        elif request.url.path.startswith("/api/routings") or request.url.path.startswith("/api/operation-definitions") or "routing " in str(exc.orig) or "routing_" in str(exc.orig):
            message = "途程版號、步序、日期、工時或作業／工作中心引用不正確，或資料已被引用；操作已取消，沒有儲存部分內容。"
        elif request.url.path.startswith("/api/boms") or "bom " in str(exc.orig) or "bom_" in str(exc.orig):
            message = "BOM 版號、用量、日期、產品或用料關聯不正確，或資料已被引用／封存；操作已取消，沒有儲存部分內容。"
        elif "UNIQUE constraint failed: supplier_return.document_no" in str(exc.orig):
            message = "退貨單號已存在，操作已取消。"
        elif "supplier_return" in str(exc.orig):
            message = "退貨來源、可退量、實際庫存或過帳狀態不正確；操作已取消，沒有儲存部分扣庫內容。"
        elif "UNIQUE constraint failed: stock_document.document_no" in str(exc.orig):
            message = "入庫單號已存在，操作已取消。"
        elif "stock_" in str(exc.orig) or "inventory_lot" in str(exc.orig):
            message = "入庫或庫存資料格式、來源關聯或過帳狀態不正確，或資料已被引用；操作已取消，沒有儲存部分內容。"
        elif "UNIQUE constraint failed: purchase_order.document_no" in str(exc.orig):
            message = "採購單號已存在，操作已取消。"
        elif "purchase_" in str(exc.orig):
            message = "採購資料格式或關聯不正確，或資料已被引用；操作已取消，沒有儲存部分內容。"
        elif "UNIQUE constraint failed: supplier_offer." in str(exc.orig):
            message = "此供應商與原料已有啟用的供料設定，操作已取消。"
        elif "supplier_offer requires" in str(exc.orig):
            message = "供料設定必須引用既有原物料，不能使用成品；操作已取消。"
        elif "ck_supplier_offer_decimal_text" in str(exc.orig):
            message = "供料單價格式不正確，操作已取消。"
        elif "UNIQUE constraint" in str(exc.orig):
            message = "代碼已存在，請使用不同代碼。"
        elif "FOREIGN KEY constraint" in str(exc.orig):
            message = "資料已被引用，無法刪除或變更；請保留原資料，或確認分類與單位有效。"
        else:
            message = "資料不符合完整性規則，操作已取消，沒有儲存部分內容。"
        return JSONResponse(status_code=409, content={"message": message})

    @app.exception_handler(OperationalError)
    async def database_busy(request, exc):
        # A failed unit of work has already rolled back. Never replay a write blindly.
        if getattr(exc.orig, "sqlite_errorcode", 0) & 255 == 5:
            return JSONResponse(status_code=503, content={"message": "資料庫暫時忙碌，操作未完成；請重新讀取後再試。"},
                                headers={"Retry-After": "1"})
        return JSONResponse(status_code=500, content={"message": "資料庫操作失敗，變更未儲存；請檢查服務紀錄。"})

    def db(request):
        return request.app.state.db

    @app.get("/api/health")
    def health(request: Request):
        with db(request).read() as session:
            return {"status": "ok", "foreign_keys": session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar(),
                    "journal_mode": session.connection().exec_driver_sql("PRAGMA journal_mode").scalar(),
                    "demo_mode": True, "operator_label": DEMO_LABEL}

    @app.get("/api/categories", response_model=list[CategoryOut])
    def categories(request: Request):
        with db(request).read() as session:
            return list(session.scalars(select(Category).order_by(Category.code)))

    @app.post("/api/categories", response_model=CategoryOut, status_code=201)
    def add_category(data: CategoryCreate, request: Request):
        with db(request).write() as session:
            if data.parent_id and session.get(Category, str(data.parent_id)) is None:
                fail(422, "上層分類不存在，請重新選擇。")
            if session.scalar(select(Category.id).where(Category.code == data.code)):
                fail(409, "分類代碼已存在。")
            row = Category(code=data.code, name=data.name, parent_id=str(data.parent_id) if data.parent_id else None,
                           created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            return CategoryOut.model_validate(row)

    @app.get("/api/units", response_model=list[UnitOut])
    def units(request: Request):
        with db(request).read() as session:
            return list(session.scalars(select(Unit).order_by(Unit.code)))

    @app.post("/api/units", response_model=UnitOut, status_code=201)
    def add_unit(data: UnitCreate, request: Request):
        with db(request).write() as session:
            if session.scalar(select(Unit.id).where(Unit.code == data.code)):
                fail(409, "單位代碼已存在。")
            row = Unit(code=data.code, name=data.name, quantity_rule="PENDING", created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            return UnitOut.model_validate(row)

    @app.delete("/api/categories/{record_id}", status_code=204)
    def delete_category(record_id: UUID, row_version: int, request: Request):
        return delete_support(Category, str(record_id), row_version, request)

    @app.delete("/api/units/{record_id}", status_code=204)
    def delete_unit(record_id: UUID, row_version: int, request: Request):
        return delete_support(Unit, str(record_id), row_version, request)

    def delete_support(model, record_id, version, request):
        with db(request).write() as session:
            row = session.get(model, record_id)
            if not row:
                fail(404, "資料不存在，請重新整理。")
            if row.row_version != version:
                fail(409, "資料已更新，請重新讀取後再操作。")
            if references(session, model.__tablename__, record_id):
                fail(409, "資料已被原料、成品或分類引用，無法刪除。")
            session.delete(row)
            session.flush()

    def list_items(request, kind, q):
        statement = select(Item).where(Item.item_kind == kind)
        if q:
            statement = statement.where(or_(Item.code.contains(q, autoescape=True), Item.name.contains(q, autoescape=True)))
        with db(request).read() as session:
            return list(session.scalars(statement.order_by(Item.code)))

    def typed_item(session, record_id, kind, label):
        row = session.get(Item, str(record_id))
        if not row or row.item_kind != kind:
            fail(404, label + "不存在，請重新整理。")
        return row

    def read_item(record_id, request, kind, label, output):
        with db(request).read() as session:
            return output.model_validate(typed_item(session, record_id, kind, label))

    def validate_refs(session, data):
        if session.get(Category, str(data.category_id)) is None:
            fail(422, "分類不存在，請重新選擇。")
        if session.get(Unit, str(data.base_unit_id)) is None:
            fail(422, "基本單位不存在，請重新選擇。")

    def create_item(data, request, kind, output):
        with db(request).write() as session:
            validate_refs(session, data)
            # Business-code uniqueness spans both maintenance views.
            if session.scalar(select(Item.id).where(Item.code == data.code)):
                fail(409, "品號已存在，請使用不同品號。")
            values = data.model_dump()
            values.update(category_id=str(data.category_id), base_unit_id=str(data.base_unit_id))
            row = Item(**values, item_kind=kind, created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            return output.model_validate(row)

    def update_item(record_id, data, request, kind, label, output):
        with db(request).write() as session:
            row = typed_item(session, record_id, kind, label)
            if row.row_version != data.row_version:
                fail(409, "資料已被另一個視窗更新，請重新讀取，避免覆蓋他人修改。")
            validate_refs(session, data)
            if session.scalar(select(Item.id).where(Item.code == data.code, Item.id != row.id)):
                fail(409, "品號已存在，請使用不同品號。")
            if (row.code != data.code or row.base_unit_id != str(data.base_unit_id)) and references(session, "item", row.id):
                fail(409, label + "已被引用，無法變更品號或基本單位。")
            # Input schemas forbid item_kind, UUID and audit-field overrides.
            for key, value in data.model_dump(exclude={"row_version"}).items():
                setattr(row, key, str(value) if key in {"category_id", "base_unit_id"} else value)
            row.row_version += 1
            session.flush()
            return output.model_validate(row)

    def delete_item(record_id, row_version, request, kind, label):
        with db(request).write() as session:
            row = typed_item(session, record_id, kind, label)
            if row.row_version != row_version:
                fail(409, "資料已更新，請重新讀取後再刪除。")
            if references(session, "item", row.id):
                fail(409, label + "已被其他資料引用，無法刪除。")
            session.delete(row)
            session.flush()

    @app.get("/api/raw-materials", response_model=list[RawOut])
    def raw_materials(request: Request, q: str = ""):
        return list_items(request, "RAW", q)

    @app.get("/api/raw-materials/{record_id}", response_model=RawOut)
    def raw_material(record_id: UUID, request: Request):
        return read_item(record_id, request, "RAW", "原料", RawOut)

    @app.post("/api/raw-materials", response_model=RawOut, status_code=201)
    def add_raw(data: RawCreate, request: Request):
        return create_item(data, request, "RAW", RawOut)

    @app.put("/api/raw-materials/{record_id}", response_model=RawOut)
    def edit_raw(record_id: UUID, data: RawUpdate, request: Request):
        return update_item(record_id, data, request, "RAW", "原料", RawOut)

    @app.delete("/api/raw-materials/{record_id}", status_code=204)
    def delete_raw(record_id: UUID, row_version: int, request: Request):
        return delete_item(record_id, row_version, request, "RAW", "原料")

    @app.get("/api/finished-goods", response_model=list[FinishedOut])
    def finished_goods(request: Request, q: str = ""):
        return list_items(request, "FINISHED", q)

    @app.get("/api/finished-goods/{record_id}", response_model=FinishedOut)
    def finished_good(record_id: UUID, request: Request):
        return read_item(record_id, request, "FINISHED", "成品", FinishedOut)

    @app.post("/api/finished-goods", response_model=FinishedOut, status_code=201)
    def add_finished(data: FinishedCreate, request: Request):
        return create_item(data, request, "FINISHED", FinishedOut)

    @app.put("/api/finished-goods/{record_id}", response_model=FinishedOut)
    def edit_finished(record_id: UUID, data: FinishedUpdate, request: Request):
        return update_item(record_id, data, request, "FINISHED", "成品", FinishedOut)

    @app.delete("/api/finished-goods/{record_id}", status_code=204)
    def delete_finished(record_id: UUID, row_version: int, request: Request):
        return delete_item(record_id, row_version, request, "FINISHED", "成品")

    install_master_routes(app, fail, references)
    install_offer_routes(app, fail, references)
    install_purchase_routes(app, fail, references)
    install_sales_routes(app, fail, references)
    install_sales_shipment_routes(app, fail, references)
    install_customer_return_routes(app, fail, references)
    install_stocktake_routes(app, fail, references)
    install_adjustment_routes(app, fail, references)
    install_transfer_routes(app, fail, references)
    install_inventory_inquiry_routes(app)
    install_inventory_traceability_routes(app)
    install_business_statistics_routes(app)
    install_receiving_routes(app, fail, references)
    install_return_routes(app, fail, references)
    install_outstanding_routes(app, fail)
    install_outstanding_sales_routes(app, fail)
    install_bom_routes(app, fail, references)
    install_routing_routes(app, fail, references)
    install_work_order_routes(app, fail, references)
    install_mrp_routes(app, fail)
    install_issue_routes(app, fail, references)
    install_material_return_routes(app, fail, references)
    install_completion_routes(app, fail, references)
    install_finished_goods_routes(app, fail, references)
    install_sqlite_maintenance_routes(app)
    install_approval_routes(app, references)
    install_standalone_export_routes(app)
    return app


app = create_app()
