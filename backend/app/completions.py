"""Factual production output, independent from every physical inventory ledger."""
from decimal import Decimal, Inexact, Rounded, localcontext, MAX_EMAX, MIN_EMIN
from fractions import Fraction
import hashlib
from uuid import UUID
from fastapi import Request, Response
from sqlalchemy import or_, select
from .db import DEMO_ACTOR_ID
from .models import Item, Unit
from .work_order_models import WorkOrder
from .work_orders import output as order_output
from .receiving_models import StockCreateReceipt
from .completion_models import ProductionCompletion as Completion
from .completion_schemas import CompletionCreate, CompletionUpdate, CompletionConfirm
from .completion_authorization import policy, review_token, confirmation_grant
from .local_demo import canonical
from .decimal_text import canonical_decimal, exact_sum, exact_difference, exact_product
from .stock_ledger import compare

REQUEST_FIELDS = {"work_order_version", "request_key", "row_version"}
NOTICE = "完工回報僅記錄生產結果，尚未執行成品入庫，成品庫存不會因此增加。"


def progress_values(planned, good, defective, count):
    result = dict(planned_qty_base=planned, confirmed_good_qty_base=good,
                  confirmed_defective_qty_base=defective, total_reported_qty_base=exact_sum([good, defective]),
                  remaining_planned_good_qty_base=exact_difference(planned, good), confirmed_report_count=count,
                  progress_percent=None, progress_percent_fraction=None, progress_warning=None,
                  over_plan=compare(good, planned) > 0)
    if compare(planned, "0") <= 0:
        result["progress_warning"] = "計畫量不大於零，良品達成率未定義；原始計畫量與實際回報量保留。"
        return result
    ratio = Fraction(Decimal(good)) * 100 / Fraction(Decimal(planned))
    # Exact rational percentage remains available even for recurring decimals.
    integer_text = lambda n: format(Decimal(n), "f")
    result["progress_percent_fraction"] = integer_text(ratio.numerator) + "/" + integer_text(ratio.denominator)
    remainder = ratio.denominator
    twos = (remainder & -remainder).bit_length() - 1
    remainder >>= twos
    fives = 0
    while remainder % 5 == 0:
        remainder //= 5
        fives += 1
    if remainder != 1:
        result["progress_warning"] = "良品達成率為非有限小數，顯示精確分數百分比；未擅自取位。"
    else:
        with localcontext() as context:
            context.prec = len(Decimal(ratio.numerator).as_tuple().digits) + max(twos, fives) + 2
            context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
            context.traps[Inexact] = context.traps[Rounded] = True
            result["progress_percent"] = canonical_decimal(format(Decimal(ratio.numerator) / Decimal(ratio.denominator), "f"))
    return result


def progress(session, order, proposal=None):
    rows = session.scalars(select(Completion).where(Completion.work_order_id == order.id,
        Completion.state == "CONFIRMED").order_by(Completion.id)).all()
    good = exact_sum([r.good_qty_base for r in rows])
    defective = exact_sum([r.defective_qty_base for r in rows])
    if proposal:
        good = exact_sum([good, proposal.good_qty_base])
        defective = exact_sum([defective, proposal.defective_qty_base])
    return progress_values(order.planned_qty_base, good, defective, len(rows) + int(proposal is not None))


def output(session, row):
    return dict(**{c.name: getattr(row, c.name) for c in Completion.__table__.columns
                   if c.name not in {"confirmation_fingerprint", "confirmation_snapshot"}},
                work_order=order_output(session, session.get(WorkOrder, row.work_order_id)),
                inventory_notice=NOTICE)


def snapshot(session, row):
    order = session.get(WorkOrder, row.work_order_id)
    item = session.get(Item, row.product_id)
    unit = session.get(Unit, row.unit_id)
    fields = lambda r: {c.name: getattr(r, c.name) for c in r.__table__.columns}
    confirmed = session.scalars(select(Completion).where(Completion.work_order_id == order.id,
        Completion.state == "CONFIRMED").order_by(Completion.id)).all()
    # JSON-normalize timestamps once so token equality never depends on Python types.
    import json
    return json.loads(canonical(dict(report=fields(row), work_order=fields(order), product=fields(item),
        unit=fields(unit), confirmed_reports=[dict(id=r.id,confirmation_id=r.confirmation_id,
        good_qty_base=r.good_qty_base,defective_qty_base=r.defective_qty_base) for r in confirmed])))


def install_completion_routes(app, fail, references):
    prefix = "/api/production-completions"

    def find(session, record_id, version=None):
        row = session.get(Completion, str(record_id))
        if not row:
            fail(404, "完工回報不存在，請重新讀取。")
        if version is not None:
            if row.row_version != version:
                fail(409, "完工回報已被另一個視窗更新，請重新讀取。")
            if row.state != "DRAFT" or references(session, "production_completion", row.id):
                fail(409, "完工回報已確認或被引用，無法修改或刪除；請保留原資料。")
        return row

    def validate(session, data, record_id=None):
        order = session.get(WorkOrder, str(data.work_order_id))
        if not order:
            fail(422, "請選擇既有製造工單。")
        if order.row_version != data.work_order_version:
            fail(409, "工單來源已更新，請重新讀取並明確選取。")
        item = session.get(Item, order.product_id)
        if item.item_kind != "FINISHED":
            fail(422, "工單必須對應既有成品及其基本單位。")
        duplicate = select(Completion.id).where(Completion.document_no == data.document_no)
        if record_id:
            duplicate = duplicate.where(Completion.id != record_id)
        if session.scalar(duplicate):
            fail(409, "完工回報單號已存在，請使用不同單號。")
        return order, item

    def eligible(row):
        if compare(row.good_qty_base, "0") < 0 or compare(row.defective_qty_base, "0") < 0:
            fail(422, "確認回報的良品與不良品量不得為負數；更正與沖回流程尚未實作。")
        if compare(exact_sum([row.good_qty_base, row.defective_qty_base]), "0") <= 0:
            fail(422, "確認回報須有實際產出：良品或不良品至少一項大於零。")

    @app.get(prefix + "/policy")
    def read_policy(request: Request):
        with request.app.state.db.read() as session:
            return policy(session)

    @app.get(prefix + "/work-orders/{order_id}/progress")
    def read_progress(order_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            order = session.get(WorkOrder, str(order_id))
            if not order:
                fail(404, "製造工單不存在。")
            return dict(work_order=order_output(session, order), **progress(session, order), inventory_notice=NOTICE)

    @app.get(prefix)
    def list_reports(request: Request, q: str = ""):
        statement = select(Completion).join(WorkOrder, Completion.work_order_id == WorkOrder.id).join(Item, Completion.product_id == Item.id)
        if q:
            statement = statement.where(or_(*(col.contains(q, autoescape=True) for col in [
                Completion.document_no, Completion.note, WorkOrder.document_no, Item.code, Item.name])))
        with request.app.state.db.read() as session:
            return [output(session, row) for row in session.scalars(statement.order_by(Completion.document_no))]

    @app.get(prefix + "/{record_id}")
    def read_report(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            return output(session, find(session, record_id))

    @app.post(prefix, status_code=201)
    def create_report(data: CompletionCreate, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest = hashlib.sha256(canonical(dict(domain="completion-create", data=data.model_dump(mode="json", exclude={"request_key"}))).encode()).hexdigest()
            receipt = session.get(StockCreateReceipt, str(data.request_key))
            if receipt:
                if receipt.resource_kind != "header" or receipt.payload_hash != digest:
                    fail(409, "新增請求識別已用於不同內容，請重新讀取。")
                row = session.get(Completion, receipt.record_id)
                if not row:
                    fail(410, "此新增請求的回報已刪除，請開始新的新增操作。")
                response.status_code = 200
                return output(session, row)
            order, item = validate(session, data)
            row = Completion(**data.model_dump(mode="json", exclude=REQUEST_FIELDS), product_id=item.id,
                             unit_id=item.base_unit_id, created_by=DEMO_ACTOR_ID)
            session.add(row)
            session.flush()
            session.add(StockCreateReceipt(request_key=str(data.request_key), resource_kind="header",
                record_id=row.id, payload_hash=digest, created_by=DEMO_ACTOR_ID))
            session.flush()
            return output(session, row)

    @app.put(prefix + "/{record_id}")
    def update_report(record_id: UUID, data: CompletionUpdate, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, data.row_version)
            order, item = validate(session, data, row.id)
            for key, value in data.model_dump(mode="json", exclude=REQUEST_FIELDS).items():
                setattr(row, key, value)
            row.product_id, row.unit_id = item.id, item.base_unit_id
            row.row_version += 1
            session.flush()
            return output(session, row)

    @app.delete(prefix + "/{record_id}", status_code=204)
    def delete_report(record_id: UUID, row_version: int, request: Request):
        with request.app.state.db.write() as session:
            row = find(session, record_id, row_version)
            session.delete(row)
            session.flush()

    @app.get(prefix + "/{record_id}/confirmation-review")
    def review(record_id: UUID, request: Request):
        with request.app.state.db.read() as session:
            row = find(session, record_id)
            if row.state != "DRAFT":
                fail(409, "此完工回報已確認；不會再次計入產出。")
            eligible(row)
            order = session.get(WorkOrder, row.work_order_id)
            return dict(report=output(session, row), current_progress=progress(session, order),
                resulting_progress=progress(session, order, row), policy=policy(session),
                confirmation_token=review_token(session, snapshot(session, row)), inventory_notice=NOTICE)

    @app.post(prefix + "/{record_id}/confirm", status_code=201)
    def confirm(record_id: UUID, data: CompletionConfirm, request: Request, response: Response):
        with request.app.state.db.write() as session:
            digest = hashlib.sha256(canonical(dict(domain="completion-confirm", report_id=str(record_id),
                data=data.model_dump(mode="json"))).encode()).hexdigest()
            used = session.scalar(select(Completion).where(Completion.confirmation_id == str(data.request_key)))
            if used:
                if used.id != str(record_id) or used.confirmation_fingerprint != digest:
                    fail(409, "確認請求識別已用於不同內容。")
                response.status_code = 200
                return output(session, used)
            row = find(session, record_id, data.row_version)
            eligible(row)
            current = snapshot(session, row)
            with confirmation_grant(session, row, data, current, digest, fail) as (time, stored_snapshot):
                row.state = "CONFIRMED"
                row.confirmation_id = str(data.request_key)
                row.confirmation_fingerprint = digest
                row.confirmation_snapshot = stored_snapshot
                row.confirmed_at, row.confirmed_by = time, DEMO_ACTOR_ID
                row.authorization_basis = "LOCAL_DEMO_MANUAL"
                row.row_version += 1
                session.flush()
            return output(session, row)
