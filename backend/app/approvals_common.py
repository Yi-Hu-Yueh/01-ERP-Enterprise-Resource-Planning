"""Small SQL helpers for the local simulated-identity approval module."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def uid():
    return str(uuid4())


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fail(status, message):
    raise HTTPException(status, message)


def rows(session, sql, **params):
    return [dict(row) for row in session.execute(text(sql), params).mappings()]


def one(session, sql, **params):
    result = rows(session, sql, **params)
    return result[0] if result else None


def insert(session, table, data):
    columns = ",".join(data)
    values = ",".join(":" + key for key in data)
    session.execute(text(f"INSERT INTO {table}({columns}) VALUES({values})"), data)


def update(session, table, record_id, data):
    statement = ",".join(key + "=:" + key for key in data)
    session.execute(text(f"UPDATE {table} SET {statement} WHERE id=:_id"), dict(data, _id=record_id))


def employee(session, employee_id, active=True):
    result = one(session, "SELECT * FROM erp_employee WHERE id=:id", id=employee_id)
    if not result:
        fail(422, "請選擇存在的目前模擬員工。")
    if active and not result["is_active"]:
        fail(409, "目前模擬員工已停用，請切換啟用的員工。")
    return result


def required(value, label):
    if not value or not str(value).strip():
        fail(422, label + "不可空白。")
    return str(value).strip()


def receipt(session, data, operation, function, validate_employee=True):
    """The caller owns one BEGIN IMMEDIATE transaction, including response receipt."""
    payload = data.model_dump(mode="json") if hasattr(data, "model_dump") else dict(data)
    # Preserve receipt hashes for legacy clients predating optional preview CAS.
    if payload.get("expected_fingerprint") is None:
        payload.pop("expected_fingerprint", None)
    key = required(payload.get("request_key"), "請求識別")
    actor = payload.get("employee_id")
    if validate_employee:
        employee(session, actor)
    fingerprint = digest(dict(operation=operation, payload=payload))
    previous = one(session, "SELECT * FROM approval_operation_receipt WHERE request_key=:key", key=key)
    if previous:
        if previous["operation"] != operation or previous["employee_id"] != actor or previous["payload_hash"] != fingerprint:
            fail(409, "請求識別已用於不同操作、員工或內容；請重新讀取後使用新的請求識別。")
        return json.loads(previous["response_json"])
    result = function()
    insert(session, "approval_operation_receipt", dict(request_key=key, operation=operation,
           employee_id=actor, payload_hash=fingerprint, response_json=canonical(result), created_at=now()))
    return result
