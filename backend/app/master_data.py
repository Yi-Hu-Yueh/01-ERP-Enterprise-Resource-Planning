"""Shared transactional maintenance for independent business identities."""
from uuid import UUID

from fastapi import Request
from sqlalchemy import or_, select

from .db import DEMO_ACTOR_ID
from .models import Currency, Customer, Supplier, Warehouse, WorkCenter
from .routing_models import OperationDefinition
from .routing_schemas import OperationCreate, OperationUpdate, OperationOut
from .schemas import (CurrencyCreate, CurrencyOut, CustomerCreate, CustomerUpdate, CustomerOut,
                      SupplierCreate, SupplierUpdate, SupplierOut, WarehouseCreate, WarehouseUpdate,
                      WarehouseOut, WorkCenterCreate, WorkCenterUpdate, WorkCenterOut)


def install_master_routes(app, fail, references):
    def register(resource, model, create_schema, update_schema, output, label, search_fields):
        prefix = "/api/" + resource
        def database(request):
            return request.app.state.db

        def find(session, record_id):
            row = session.get(model, str(record_id))
            if row is None:
                fail(404, label + "不存在，請重新整理。")
            return row

        def validate(session, data):
            if hasattr(data, "currency_id") and session.get(Currency, str(data.currency_id)) is None:
                fail(422, "幣別不存在，請重新選擇。")

        def unique(session, code, record_id=None):
            query = select(model.id).where(model.code == code)
            if record_id is not None:
                query = query.where(model.id != record_id)
            if session.scalar(query):
                fail(409, label + "代碼已存在，請使用不同代碼。")

        def list_records(request, q=""):
            statement = select(model)
            if q:
                statement = statement.where(or_(*(getattr(model, field).contains(q, autoescape=True) for field in search_fields)))
            with database(request).read() as session:
                return list(session.scalars(statement.order_by(model.code)))

        def read_record(record_id, request):
            with database(request).read() as session:
                return output.model_validate(find(session, record_id))

        def create_record(data, request):
            with database(request).write() as session:
                validate(session, data)
                unique(session, data.code)
                row = model(**data.model_dump(mode="json"), created_by=DEMO_ACTOR_ID)
                session.add(row)
                session.flush()
                return output.model_validate(row)

        def update_record(record_id, data, request):
            with database(request).write() as session:
                row = find(session, record_id)
                if row.row_version != data.row_version:
                    fail(409, "資料已被另一個視窗更新，請重新讀取，避免覆蓋他人修改。")
                validate(session, data)
                unique(session, data.code, row.id)
                for field, value in data.model_dump(exclude={"row_version"}, mode="json").items():
                    setattr(row, field, value)
                row.row_version += 1
                session.flush()
                return output.model_validate(row)

        def delete_record(record_id, row_version, request):
            with database(request).write() as session:
                row = find(session, record_id)
                if row.row_version != row_version:
                    fail(409, "資料已更新，請重新讀取後再操作。")
                if references(session, model.__tablename__, row.id):
                    fail(409, label + "已被引用，無法刪除；請保留原資料。")
                session.delete(row)
                session.flush()

        # Explicit annotations keep this small route factory visible to FastAPI/OpenAPI.
        for function, annotations in [
            (list_records, {"request": Request, "q": str}),
            (read_record, {"record_id": UUID, "request": Request}),
            (create_record, {"data": create_schema, "request": Request}),
            (update_record, {"record_id": UUID, "data": update_schema, "request": Request}),
            (delete_record, {"record_id": UUID, "row_version": int, "request": Request}),
        ]:
            function.__annotations__ = annotations
            function.__name__ = resource.replace("-", "_") + "_" + function.__name__
        app.get(prefix, response_model=list[output])(list_records)
        app.get(prefix + "/{record_id}", response_model=output)(read_record)
        app.post(prefix, response_model=output, status_code=201)(create_record)
        if update_schema is not None:
            app.put(prefix + "/{record_id}", response_model=output)(update_record)
        app.delete(prefix + "/{record_id}", status_code=204)(delete_record)

    register("currencies", Currency, CurrencyCreate, None, CurrencyOut, "幣別", ["code", "name"])
    party_search = ["code", "name", "tax_identifier", "contact_name", "phone", "email"]
    register("customers", Customer, CustomerCreate, CustomerUpdate, CustomerOut, "客戶", party_search)
    register("suppliers", Supplier, SupplierCreate, SupplierUpdate, SupplierOut, "供應商", party_search)
    register("warehouses", Warehouse, WarehouseCreate, WarehouseUpdate, WarehouseOut, "倉庫", ["code", "name", "address"])
    register("work-centers", WorkCenter, WorkCenterCreate, WorkCenterUpdate, WorkCenterOut, "工作中心", ["code", "name", "description"])

    register("operation-definitions", OperationDefinition, OperationCreate, OperationUpdate, OperationOut, "製程作業", ["code", "name", "instruction"])
