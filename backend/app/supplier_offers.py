"""One atomic offering per editor; reuse the established transaction/version boundary."""
from uuid import UUID

from fastapi import Request
from sqlalchemy import or_, select

from .db import DEMO_ACTOR_ID
from .models import Item, Supplier, SupplierOffer
from .schemas import OfferCreate, OfferOut, OfferUpdate


def install_offer_routes(app, fail, references):
    prefix = "/api/supplier-offers"

    def find(session, record_id):
        row = session.get(SupplierOffer, str(record_id))
        if row is None:
            fail(404, "供料設定不存在，請重新整理。")
        return row

    def output(session, row):
        supplier, item = session.get(Supplier, row.supplier_id), session.get(Item, row.item_id)
        return OfferOut(**{column.name: getattr(row, column.name) for column in SupplierOffer.__table__.columns},
                        supplier_code=supplier.code, supplier_name=supplier.name,
                        item_code=item.code, item_name=item.name)

    def validate(session, data, record_id=None):
        if session.get(Supplier, str(data.supplier_id)) is None:
            fail(422, "供應商不存在，請重新選擇。")
        item = session.get(Item, str(data.item_id))
        if item is None or item.item_kind != "RAW":
            fail(422, "原料不存在或不是原物料，請重新選擇原物料。")
        if data.is_active:
            query = select(SupplierOffer.id).where(SupplierOffer.supplier_id == str(data.supplier_id),
                SupplierOffer.item_id == str(data.item_id), SupplierOffer.is_active.is_(True))
            if record_id is not None:
                query = query.where(SupplierOffer.id != record_id)
            if session.scalar(query):
                fail(409, "此供應商與原料已有啟用的供料設定，請編輯原設定或重新選擇。")

    @app.get(prefix, response_model=list[OfferOut])
    def list_offers(request: Request, q: str = ""):
        statement = select(SupplierOffer).join(Supplier, Supplier.id == SupplierOffer.supplier_id).join(Item, Item.id == SupplierOffer.item_id)
        if q:
            statement = statement.where(or_(*(column.contains(q, autoescape=True)
                for column in [Supplier.code, Supplier.name, Item.code, Item.name])))
        with request.app.state.db.read() as session:
            return [output(session, row) for row in session.scalars(statement.order_by(Supplier.code, Item.code, SupplierOffer.id))]

    @app.get(prefix + "/{record_id}", response_model=OfferOut)
    def read_offer(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return output(session, find(session, record_id))

    @app.post(prefix, response_model=OfferOut, status_code=201)
    def create_offer(data: OfferCreate, request: Request):
        with request.app.state.db.write() as session:
            validate(session, data)
            row = SupplierOffer(**data.model_dump(mode="json"), created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            return output(session, row)

    @app.put(prefix + "/{record_id}", response_model=OfferOut)
    def update_offer(record_id: UUID, data: OfferUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id)
            if row.row_version != data.row_version:
                fail(409, "資料已被另一個視窗更新，請重新讀取，避免覆蓋他人修改。")
            validate(session, data, row.id)
            for field, value in data.model_dump(exclude={"row_version"}, mode="json").items():
                setattr(row, field, value)
            row.row_version += 1
            session.flush()
            return output(session, row)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_offer(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id)
            if row.row_version != row_version:
                fail(409, "資料已更新，請重新讀取後再刪除。")
            if references(session, "supplier_offer", row.id):
                fail(409, "供料設定已被引用，無法刪除；請保留原資料。")
            session.delete(row)
            session.flush()
