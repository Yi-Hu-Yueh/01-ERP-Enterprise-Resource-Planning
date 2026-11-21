"""Guarded no-login organization and reusable approval HTTP endpoints."""
import hmac
import secrets
from typing import Literal, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from .approvals_common import employee, fail, one, receipt, rows
from .approvals_demo import blueprints, initialize_demo
from .approvals_engine import (ActionInput, DefinitionInput, Input, SOURCE_TABLE, SubmitInput,
    action_request, definition_output, request_detail, request_row, save_definition, source_snapshot, submit_request)
from .approvals_org import install_organization_routes
from .sqlite_maintenance import _protect as protect_local_runtime

TOKEN_HEADER = "X-ERP-Approval-Token"


class DemoInput(Input):
    request_key: str


def install_approval_routes(app, references):
    csrf = secrets.token_urlsafe(32)
    def protect(request, mutation=False):
        # Reuse the existing explicit trusted local-factory binding and strict
        # Host/Origin/Referer/fetch-site validation. Plain public apps deny access.
        protect_local_runtime(request)
        if mutation:
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json" and request.method != "DELETE":
                fail(415, "簽核維護要求必須使用 JSON。")
            if not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), csrf):
                fail(403, "本機簽核操作憑證無效，請重新整理後操作。")
    def reply(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})
    install_organization_routes(app, protect, references)

    @app.get("/api/approval/config")
    def config(request: Request):
        protect(request)
        return reply(dict(csrf_token=csrf, blueprints=blueprints(), identity_notice="測試模式／未驗證身分",
            security="PARTIAL — RISK CONTAINED", login_required=False,
            delegation_scope="task_only", withdrawal_rule="無後續關卡人員操作時可撤銷；重新開啟該關新輪次，該關所有簽核重新執行；保留原歷史。"))

    @app.post("/api/approval/demo-init")
    def demo_init(data: DemoInput, request: Request):
        protect(request, mutation=True)
        with request.app.state.db.write() as session:
            return reply(receipt(session, data, "demo-init", lambda: initialize_demo(session), validate_employee=False))

    @app.get("/api/approval/definitions")
    def definitions(request: Request, employee_id: str, all_versions: bool = False):
        protect(request)
        with request.app.state.db.read() as session:
            employee(session, employee_id)
            records = rows(session, "SELECT d.* FROM approval_definition d" +
                ("" if all_versions else " WHERE d.version=(SELECT MAX(version) FROM approval_definition newer WHERE newer.code=d.code)") + " ORDER BY d.code,d.version")
            return reply([definition_output(session, record) for record in records])

    @app.post("/api/approval/definitions")
    def definition_save(data: DefinitionInput, request: Request):
        protect(request, mutation=True)
        with request.app.state.db.write() as session:
            return reply(receipt(session, data, "definition-save", lambda: save_definition(session, data)))

    @app.get("/api/approval/sources")
    def sources(request: Request, employee_id: str, type: Literal["PURCHASE_ORDER", "WORK_ORDER", "SALES_ORDER"], q: str = "", source_id: Optional[str] = None):
        protect(request)
        with request.app.state.db.read() as session:
            employee(session, employee_id)
            table = SOURCE_TABLE[type][0]
            records = rows(session, f"SELECT id,document_no FROM {table} WHERE instr(lower(document_no),lower(:q))>0" +
                (" AND id=:source_id" if source_id else "") + " ORDER BY document_no", q=q, source_id=source_id)
            result = []
            for record in records:
                snapshot, fingerprint = source_snapshot(session, type, record["id"])
                result.append(dict(record, **snapshot, fingerprint=fingerprint))
            return reply(result)

    @app.post("/api/approval/requests")
    def submit(data: SubmitInput, request: Request):
        protect(request, mutation=True)
        with request.app.state.db.write() as session:
            return reply(receipt(session, data, "submit", lambda: submit_request(session, data)))

    @app.get("/api/approval/requests")
    def requests(request: Request, employee_id: str, view: Literal["all", "mine", "inbox"] = "all",
                 q: str = "", status: Optional[str] = None, source_type: Optional[str] = None, source_id: Optional[str] = None):
        protect(request)
        with request.app.state.db.read() as session:
            employee(session, employee_id)
            clauses, params = [], {}
            if view == "mine":
                clauses.append("r.requester_employee_id=:employee")
                params["employee"] = employee_id
            if status:
                clauses.append("r.status=:status")
                params["status"] = status
            if source_type:
                clauses.append("r.source_type=:source_type")
                params["source_type"] = source_type
            if source_id:
                clauses.append("(r.purchase_order_id=:source_id OR r.work_order_id=:source_id OR r.sales_order_id=:source_id)")
                params["source_id"] = source_id
            if q:
                clauses.append("(instr(lower(r.source_document_no),lower(:q))>0 OR instr(lower(d.name),lower(:q))>0 OR instr(lower(e.name),lower(:q))>0)")
                params["q"] = q
            records = rows(session, "SELECT r.* FROM approval_request r JOIN approval_definition d ON d.id=r.definition_id JOIN erp_employee e ON e.id=r.requester_employee_id" +
                           (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY r.created_at DESC,r.id", **params)
            result = [request_detail(session, record, employee_id) for record in records]
            if view == "inbox":
                # An assigned changed-source task remains visible with its warning,
                # even though action eligibility intentionally becomes false.
                from .approvals_engine import current_cycle, task_eligible
                result = [detail for detail in result if detail["status"] == "PENDING" and any(
                    task["cycle"] == detail["cycle"] and task["stage_no"] == detail["current_stage"]
                    and task["status"] == "PENDING" and task_eligible(session, task, employee_id) for task in detail["tasks"])]
            return reply(result)

    @app.get("/api/approval/reminders")
    def reminders(request: Request, employee_id: str):
        protect(request)
        from datetime import datetime, timezone
        from .approvals_engine import task_eligible
        with request.app.state.db.read() as session:
            employee(session, employee_id)
            tasks = rows(session, """SELECT t.*,c.cycle_no cycle FROM approval_task t
                JOIN approval_cycle c ON c.id=t.cycle_id JOIN approval_request r ON r.id=t.request_id
                WHERE r.status='PENDING' AND t.status='PENDING' AND c.cycle_no=r.cycle AND t.stage_no=r.current_stage""")
            pending = [task for task in tasks if task_eligible(session, task, employee_id)]
            current = datetime.now(timezone.utc)
            for task in pending:
                task["waiting_seconds"] = max(0, int((current - datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))).total_seconds()))
            return reply(dict(employee_id=employee_id, count=len(pending), pending_count=len(pending), tasks=pending))

    @app.get("/api/approval/requests/{request_id}")
    def detail(request_id: str, request: Request, employee_id: str):
        protect(request)
        with request.app.state.db.read() as session:
            employee(session, employee_id)
            return reply(request_detail(session, request_row(session, request_id), employee_id))

    @app.post("/api/approval/requests/{request_id}/actions")
    def action(request_id: str, data: ActionInput, request: Request):
        protect(request, mutation=True)
        with request.app.state.db.write() as session:
            return reply(receipt(session, data, "action:" + request_id + ":" + data.action,
                                 lambda: action_request(session, request_id, data)))
