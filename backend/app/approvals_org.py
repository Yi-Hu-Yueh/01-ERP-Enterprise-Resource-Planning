"""Organization maintenance; an employee is a master identity, never a login."""
from typing import Optional

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import text

from .approvals_common import employee, fail, insert, now, one, required, rows, uid, update


class MasterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    name: str
    is_active: StrictBool = True
    notes: Optional[str] = None
    row_version: Optional[int] = Field(default=None, ge=1)


class DepartmentInput(MasterInput):
    parent_department_id: Optional[str] = None
    manager_employee_id: Optional[str] = None


class EmployeeInput(MasterInput):
    department_id: str
    title: Optional[str] = None
    manager_employee_id: Optional[str] = None
    role_ids: list[str] = Field(default_factory=list)


def master_output(session, table, record):
    record = dict(record)
    record["is_active"] = bool(record["is_active"])
    record["is_demo"] = bool(record["is_demo"])
    if table == "erp_department":
        record["parent_department_id"] = record.pop("parent_id")
    if table == "erp_employee":
        record["role_ids"] = [r["role_id"] for r in rows(session,
            "SELECT role_id FROM approval_employee_role WHERE employee_id=:id ORDER BY role_id", id=record["id"])]
    return record


def validate_master(session, table, payload, record_id):
    for field in ("code", "name"):
        payload[field] = required(payload[field], "代碼" if field == "code" else "名稱")
    if one(session, f"SELECT id FROM {table} WHERE code=:code AND id<>:id", code=payload["code"], id=record_id):
        fail(409, "代碼已存在，請使用不同代碼。")
    if table == "erp_department":
        payload["parent_id"] = payload.pop("parent_department_id")
        relation = payload["parent_id"]
        if relation and not one(session, "SELECT id FROM erp_department WHERE id=:id", id=relation):
            fail(422, "上層部門不存在。")
        chain_table, chain_field = "erp_department", "parent_id"
    elif table == "erp_employee":
        if not one(session, "SELECT id FROM erp_department WHERE id=:id", id=payload["department_id"]):
            fail(422, "主要部門不存在。")
        for role in payload["role_ids"]:
            if not one(session, "SELECT id FROM approval_role WHERE id=:id", id=role):
                fail(422, "簽核角色不存在。")
        chain_table, chain_field, relation = "erp_employee", "manager_employee_id", payload["manager_employee_id"]
    else:
        return
    manager = payload.get("manager_employee_id")
    if manager:
        employee(session, manager, active=False)
    seen = {record_id}
    while relation:
        if relation in seen:
            fail(422, "部門階層或員工主管不可形成循環。")
        seen.add(relation)
        parent = one(session, f"SELECT {chain_field} value FROM {chain_table} WHERE id=:id", id=relation)
        relation = parent["value"] if parent else None


def save_master(session, table, data, record_id=None):
    current = one(session, f"SELECT * FROM {table} WHERE id=:id", id=record_id) if record_id else None
    if record_id and not current:
        fail(404, "資料不存在，請重新讀取。")
    if current and current["row_version"] != data.row_version:
        fail(409, "資料已更新，請重新讀取後再儲存。")
    record_id = record_id or uid()
    payload = data.model_dump(exclude={"row_version"})
    validate_master(session, table, payload, record_id)
    roles = payload.pop("role_ids", [])
    payload.update(updated_at=now(), row_version=(current["row_version"] + 1 if current else 1))
    if current:
        update(session, table, record_id, payload)
    else:
        payload.update(id=record_id, created_at=payload["updated_at"], is_demo=0)
        insert(session, table, payload)
    if table == "erp_employee":
        session.execute(text("DELETE FROM approval_employee_role WHERE employee_id=:id"), dict(id=record_id))
        for role in sorted(set(roles)):
            insert(session, "approval_employee_role", dict(employee_id=record_id, role_id=role))
    return master_output(session, table, one(session, f"SELECT * FROM {table} WHERE id=:id", id=record_id))


def install_organization_routes(app, protect, references):
    def register(path, table, schema):
        def listing(request, q="", department_id=None, is_active=None):
            protect(request)
            conditions, params = [], {}
            if q:
                conditions.append("(instr(lower(code),lower(:q))>0 OR instr(lower(name),lower(:q))>0)")
                params["q"] = q
            if department_id and table == "erp_employee":
                conditions.append("department_id=:department_id")
                params["department_id"] = department_id
            if is_active is not None:
                conditions.append("is_active=:active")
                params["active"] = is_active
            with request.app.state.db.read() as session:
                found = rows(session, f"SELECT * FROM {table}" + (" WHERE " + " AND ".join(conditions) if conditions else "") + " ORDER BY code", **params)
                return [master_output(session, table, record) for record in found]

        def create(data, request):
            protect(request, mutation=True)
            with request.app.state.db.write() as session:
                return save_master(session, table, data)

        def edit(record_id, data, request):
            protect(request, mutation=True)
            with request.app.state.db.write() as session:
                return save_master(session, table, data, record_id)

        def remove(record_id, row_version, request):
            protect(request, mutation=True)
            with request.app.state.db.write() as session:
                existing = one(session, f"SELECT * FROM {table} WHERE id=:id", id=record_id)
                if not existing:
                    fail(404, "資料不存在。")
                if existing["row_version"] != row_version:
                    fail(409, "資料已更新，請重新讀取。")
                excluded = ("approval_employee_role",) if table == "erp_employee" else ()
                if references(session, table, record_id, exclude_tables=excluded):
                    fail(409, "資料已被部門、員工、流程設定或簽核歷史引用，請改為停用並保留原資料。")
                if table == "erp_employee":
                    session.execute(text("DELETE FROM approval_employee_role WHERE employee_id=:id"), dict(id=record_id))
                session.execute(text(f"DELETE FROM {table} WHERE id=:id"), dict(id=record_id))

        for function, annotations in (
            (listing, dict(request=Request, q=str, department_id=Optional[str], is_active=Optional[bool])),
            (create, dict(data=schema, request=Request)),
            (edit, dict(record_id=str, data=schema, request=Request)),
            (remove, dict(record_id=str, row_version=int, request=Request)),
        ):
            function.__annotations__ = annotations
            function.__name__ = table + "_" + function.__name__
        app.get(path)(listing)
        app.post(path, status_code=201)(create)
        app.put(path + "/{record_id}")(edit)
        app.delete(path + "/{record_id}", status_code=204)(remove)

    register("/api/departments", "erp_department", DepartmentInput)
    register("/api/employees", "erp_employee", EmployeeInput)
    register("/api/approval/roles", "approval_role", MasterInput)
