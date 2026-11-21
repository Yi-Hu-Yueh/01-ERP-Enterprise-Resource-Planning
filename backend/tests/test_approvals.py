"""Focused approval safety checks against fresh synthetic online-backup clones.

No fixture opens, copies or mutates Owner data. Existing business APIs seed the
source documents and genuine prior ledger; approval actions must leave every
baseline business row (including source row versions) unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend.app.local_demo import create_local_demo_app
from backend.app.stock_ledger import register_exact_sql
from backend.tests.seed_approval_browser import (
    ARTIFACTS, PORT, UI_PORT, business_rows, clone, fingerprint, isolated, seed,
)
from backend.tests.test_purchasing import header_edit as purchase_edit
from backend.tests.test_sales import header_edit as sales_edit
from backend.tests.test_work_orders import edit as work_edit

BASE = "/api/approval"
ORIGIN = f"http://127.0.0.1:{UI_PORT}"


def read_sql(path, sql, params=()):
    with closing(sqlite3.connect(isolated(path).as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(sql, params)]


@pytest.fixture(scope="session")
def approval_template():
    path = ARTIFACTS / "source-template-final.sqlite"
    metadata_path = ARTIFACTS / "source-template-final.json"
    if not path.exists():
        metadata = seed(path)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    metadata = json.loads(metadata_path.read_text(encoding="utf8"))
    assert metadata["synthetic_only"] is True
    assert fingerprint(business_rows(path, metadata["business_tables"])) == metadata["expected_business_fingerprint"]
    return path, metadata


def new_client(path):
    return TestClient(create_local_demo_app(path, backend_port=PORT, frontend_port=UI_PORT),
                      base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 51000))


def csrf(client):
    response = client.get(BASE + "/config")
    assert response.status_code == 200, response.text
    return {"X-ERP-Approval-Token": response.json()["csrf_token"], "Origin": ORIGIN}


def post(client, path, body, status=200):
    response = client.post(path, json=body, headers=csrf(client))
    assert response.status_code in ({status} if isinstance(status, int) else set(status)), response.text
    return response.json()


@pytest.fixture
def qa(approval_template):
    source, metadata = approval_template
    path = ARTIFACTS / "backend-cases" / str(uuid4()) / "isolated.sqlite"
    metadata = clone(source, path, metadata)
    with new_client(path) as client:
        yield dict(client=client, path=path, metadata=metadata)


def initialize(r):
    body = {"request_key": str(uuid4())}
    result = post(r["client"], BASE + "/demo-init", body, (200, 201))
    counts = read_sql(r["path"], "SELECT (SELECT count(*) FROM erp_department) departments,(SELECT count(*) FROM erp_employee) employees,(SELECT count(*) FROM approval_definition) definitions")
    assert post(r["client"], BASE + "/demo-init", body, (200, 201)) == result
    post(r["client"], BASE + "/demo-init", {"request_key": str(uuid4())}, (200, 201))
    assert read_sql(r["path"], "SELECT (SELECT count(*) FROM erp_department) departments,(SELECT count(*) FROM erp_employee) employees,(SELECT count(*) FROM approval_definition) definitions") == counts
    r["requester"] = read_sql(r["path"], "SELECT id FROM erp_employee WHERE is_demo=1 AND manager_employee_id IS NOT NULL ORDER BY code")[0]["id"]
    r["definitions"] = {row["source_type"]: row for row in read_sql(r["path"], "SELECT * FROM approval_definition ORDER BY version")}
    assert set(r["definitions"]) == {"PURCHASE_ORDER", "WORK_ORDER", "SALES_ORDER"}
    return r


def submit_body(r, source_type="PURCHASE_ORDER", **patch):
    return dict(employee_id=r["requester"], request_key=str(uuid4()), definition_id=r["definitions"][source_type]["id"],
                source_type=source_type, source_id=r["metadata"]["sources"][source_type]["id"], **patch)


def submit(r, source_type="PURCHASE_ORDER", **patch):
    return post(r["client"], BASE + "/requests", submit_body(r, source_type, **patch), (200, 201))


def current(r, request):
    response = r["client"].get(BASE + "/requests/" + request["id"], params={"employee_id": r["requester"]})
    assert response.status_code == 200, response.text
    return response.json()


def tasks(r, request, stage=None, status="PENDING"):
    row = current(r, request)
    stage = row["current_stage"] if stage is None else stage
    return read_sql(r["path"], "SELECT t.* FROM approval_task t JOIN approval_cycle c ON c.id=t.cycle_id WHERE t.request_id=? AND c.cycle_no=? AND t.stage_no=? AND t.status=? ORDER BY t.assigned_employee_id", (row["id"], row["cycle"], stage, status))


def action_body(r, request, action="approve", task=None, employee_id=None, **patch):
    row = current(r, request)
    body = dict(employee_id=employee_id or (task["assigned_employee_id"] if task else r["requester"]),
                request_key=str(uuid4()), row_version=row["row_version"], cycle=row["cycle"], action=action)
    if task:
        body["task_id"] = task["id"]
    return {**body, **patch}


def act(r, request, action="approve", task=None, employee_id=None, **patch):
    return post(r["client"], BASE + "/requests/" + request["id"] + "/actions",
                action_body(r, request, action, task, employee_id, **patch), (200, 201))


def advance(r, request):
    pending = tasks(r, request)
    assert pending
    for task in pending:
        request = act(r, request, task=task)
    return current(r, request)


def events(r, request):
    return read_sql(r["path"], "SELECT * FROM approval_event WHERE request_id=? ORDER BY occurred_at,id", (request["id"],))


def assert_business_unchanged(r):
    assert fingerprint(business_rows(r["path"], r["metadata"]["business_tables"])) == r["metadata"]["expected_business_fingerprint"]


def edit_master(client, path, row, **patch):
    keys = ["code", "name", "is_active", "notes", "row_version"]
    keys += ["parent_department_id", "manager_employee_id"] if path == "/api/departments" else ["department_id", "title", "manager_employee_id", "role_ids"]
    body = {key: row[key] for key in keys}
    return client.put(path + "/" + row["id"], json={**body, **patch}, headers=csrf(client))


def test_organization_relations_roles_cycles_references_and_inactive_actor(qa):
    r = initialize(qa); c = r["client"]
    parent = post(c, "/api/departments", dict(code="QA-PARENT", name="合成上層"), 201)
    child = post(c, "/api/departments", dict(code="QA-CHILD", name="合成子部門", parent_department_id=parent["id"]), 201)
    assert edit_master(c, "/api/departments", parent, parent_department_id=child["id"]).status_code == 422
    role = post(c, BASE + "/roles", dict(code="QA-ROLE", name="合成角色"), 201)
    employee = post(c, "/api/employees", dict(code="QA-EMP", name="合成員工", department_id=child["id"], title="測試", role_ids=[role["id"]]), 201)
    assert c.get("/api/employees", params={"department_id": child["id"]}).json() == [employee]
    assert c.get("/api/employees", params={"q": "QA-EMP"}).json() == [employee]
    assert edit_master(c, "/api/employees", employee, manager_employee_id=employee["id"]).status_code == 422
    assert c.delete("/api/departments/" + child["id"], params={"row_version": child["row_version"]}, headers=csrf(c)).status_code == 409
    assert c.delete(BASE + "/roles/" + role["id"], params={"row_version": role["row_version"]}, headers=csrf(c)).status_code == 409
    employee = edit_master(c, "/api/employees", employee, is_active=False).json()
    body = submit_body(r); body["employee_id"] = employee["id"]
    assert c.post(BASE + "/requests", json=body, headers=csrf(c)).status_code == 409
    assert c.delete("/api/employees/" + employee["id"], params={"row_version": employee["row_version"]}, headers=csrf(c)).status_code == 204
    assert c.delete("/api/departments/" + child["id"], params={"row_version": child["row_version"]}, headers=csrf(c)).status_code == 204
    assert_business_unchanged(r)


def test_three_real_four_stage_workflows_require_distinct_cosigners_and_preserve_sources(qa):
    r = initialize(qa)
    saved = []
    for source_type in ("PURCHASE_ORDER", "WORK_ORDER", "SALES_ORDER"):
        request = submit(r, source_type)
        assert request["status"] == "PENDING" and request["current_stage"] == 1
        stages = read_sql(r["path"], "SELECT stage_no FROM approval_stage WHERE definition_id=? ORDER BY stage_no", (request["definition_id"],))
        assert [row["stage_no"] for row in stages] == [1, 2, 3, 4]
        for expected in (2, 3):
            request = advance(r, request)
            assert request["current_stage"] == expected
        co = tasks(r, request)
        assert len(co) == 2 and len({row["assigned_employee_id"] for row in co}) == 2
        first = act(r, request, task=co[0])
        assert first["status"] == "PENDING" and first["current_stage"] == 3
        assert not tasks(r, first, stage=4)
        assert len(tasks(r, first)) == 1
        request = act(r, first, task=co[1])
        assert request["current_stage"] == 4 and request["status"] == "PENDING"
        request = advance(r, request)
        assert request["status"] == "APPROVED" and request["current_stage"] == 4
        assert len([e for e in events(r, request) if e["action"] == "approve"]) == 5
        saved.append((request, events(r, request)))
    assert_business_unchanged(r)
    # A new complete application instance reopens the same committed SQLite.
    with new_client(r["path"]) as reopened:
        previous = r["client"]; r["client"] = reopened
        for request, history in saved:
            assert current(r, request) == request
            assert events(r, request) == history
        r["client"] = previous


def test_return_from_parallel_stage_starts_fresh_cycle_and_preserves_prior_decisions(qa):
    r = initialize(qa); request = advance(r, advance(r, submit(r)))
    co = tasks(r, request)
    request = act(r, request, task=co[0], comment="First cycle approval")
    old_events = events(r, request)
    old_cycle = request["cycle"]
    request = act(r, request, "return", task=co[1], reason="Recheck previous stage")
    assert request["current_stage"] == 2 and request["cycle"] > old_cycle
    assert {e["id"] for e in old_events} <= {e["id"] for e in events(r, request)}
    stale = action_body(r, request, task=co[0])
    assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=stale, headers=csrf(r["client"])).status_code == 409
    request = advance(r, request)
    assert request["current_stage"] == 3
    fresh = tasks(r, request)
    assert len(fresh) == 2 and not ({t["id"] for t in fresh} & {t["id"] for t in co})
    request = act(r, request, task=fresh[0])
    assert request["current_stage"] == 3
    request = act(r, request, task=fresh[1])
    assert request["current_stage"] == 4
    assert any(e["action"] == "return" and e["reason"] == "Recheck previous stage" for e in events(r, request))
    assert_business_unchanged(r)


def test_requester_withdrawal_and_stage_one_return_require_new_attempts(qa):
    r = initialize(qa); request = submit(r)
    task = tasks(r, request)[0]
    before = events(r, request)
    no_reason = action_body(r, request, "reject", task=task, reason=" ")
    assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=no_reason, headers=csrf(r["client"])).status_code == 422
    wrong_approver = action_body(r, request, task=task, employee_id=r["requester"])
    assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=wrong_approver, headers=csrf(r["client"])).status_code == 409
    assert events(r, request) == before
    wrong = action_body(r, request, "withdraw", employee_id=task["assigned_employee_id"])
    assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=wrong, headers=csrf(r["client"])).status_code == 403
    withdrawn = act(r, request, "withdraw", comment="Requester stops draft review")
    assert withdrawn["status"] == "WITHDRAWN"
    stale = action_body(r, withdrawn, task=task)
    assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=stale, headers=csrf(r["client"])).status_code == 409
    competing = submit(r)
    resubmit = action_body(r, withdrawn, "resubmit")
    assert r["client"].post(BASE + "/requests/" + withdrawn["id"] + "/actions", json=resubmit, headers=csrf(r["client"])).status_code == 409
    assert len(read_sql(r["path"], "SELECT id FROM approval_request WHERE purchase_order_id=? AND status='PENDING'", (r["metadata"]["sources"]["PURCHASE_ORDER"]["id"],))) == 1
    act(r, competing, "withdraw")
    request = act(r, withdrawn, "resubmit")
    assert request["attempt"] > withdrawn["attempt"] and request["current_stage"] == 1
    request = act(r, request, "return", task=tasks(r, request)[0], reason="Applicant clarification")
    assert request["status"] == "RETURNED" and request["current_stage"] == 1
    request = act(r, request, "resubmit")
    assert request["attempt"] == 3 and request["status"] == "PENDING"
    actions = [e["action"] for e in events(r, request)]
    assert "withdraw" in actions and "return" in actions and actions.count("resubmit") == 2
    request = act(r, request, "reject", task=tasks(r, request)[0], reason="Cannot approve submitted content")
    assert request["status"] == "REJECTED" and not tasks(r, request)
    request = act(r, request, "resubmit")
    assert request["attempt"] == 4 and request["status"] == "PENDING"
    assert any(e["action"] == "reject" and e["reason"] == "Cannot approve submitted content" for e in events(r, request))
    assert_business_unchanged(r)


def test_own_approval_withdrawal_allows_safe_undo_but_blocks_downstream_activity(qa):
    r = initialize(qa); request = submit(r); original = tasks(r, request)[0]
    request = act(r, request, task=original)
    request = act(r, request, "withdraw_approval", task=original)
    assert request["current_stage"] == 1 and len(tasks(r, request)) == 1
    request = advance(r, request)
    safe_task = tasks(r, request, stage=1, status="APPROVED")[0]
    request = advance(r, request)
    before = events(r, request)
    body = action_body(r, request, "withdraw_approval", task=safe_task)
    response = r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=body, headers=csrf(r["client"]))
    assert response.status_code == 409 and response.json()
    assert events(r, request) == before and current(r, request) == request
    assert_business_unchanged(r)


def test_added_signer_remains_required_and_cannot_replace_original_task(qa):
    r = initialize(qa); request = submit(r); required = tasks(r, request)[0]
    extra = read_sql(r["path"], "SELECT id FROM erp_employee WHERE is_active=1 AND id<>? AND id<>? ORDER BY code", (required["assigned_employee_id"], r["requester"]))[0]["id"]
    request = act(r, request, "add_signer", task=required, target_employee_id=extra, reason="Independent additional review")
    pending = tasks(r, request)
    assert len(pending) == 2 and any(t["assigned_employee_id"] == extra and t["is_additional"] for t in pending)
    request = act(r, request, task=required)
    assert request["current_stage"] == 1
    added = tasks(r, request)[0]
    assert added["assigned_employee_id"] == extra
    request = act(r, request, task=added)
    assert request["current_stage"] == 2
    assert any(e["action"] == "add_signer" and e["actor_employee_id"] == required["assigned_employee_id"] for e in events(r, request))
    assert_business_unchanged(r)


def utc(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def test_task_scoped_delegation_period_and_distinct_actual_cosign_actors(qa):
    r = initialize(qa); request = submit(r); original = tasks(r, request)[0]
    delegate = read_sql(r["path"], "SELECT id FROM erp_employee WHERE is_active=1 AND id<>? AND id<>? ORDER BY code", (original["assigned_employee_id"], r["requester"]))[0]["id"]
    now = datetime.now(timezone.utc)
    request = act(r, request, "delegate", task=original, target_employee_id=delegate,
                  effective_from=utc(now-timedelta(days=2)), effective_to=utc(now-timedelta(days=1)), comment="Expired delegation")
    expired = action_body(r, request, task=original, employee_id=delegate)
    assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=expired, headers=csrf(r["client"])).status_code == 409
    request = act(r, request, "delegate", task=original, target_employee_id=delegate,
                  effective_from=utc(now-timedelta(hours=1)), effective_to=utc(now+timedelta(hours=1)), comment="Specific task delegation")
    request = act(r, request, task=original, employee_id=delegate)
    decision = [e for e in events(r, request) if e["action"] == "approve"][-1]
    assert decision["actor_employee_id"] == delegate
    assert decision["represented_employee_id"] == decision["assigned_employee_id"] == original["assigned_employee_id"]
    stage_two = tasks(r, request)[0]
    unauthorized = action_body(r, request, task=stage_two, employee_id=delegate)
    # Delegating Stage 1 never grants a role or authority over later tasks.
    if stage_two["assigned_employee_id"] != delegate:
        assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=unauthorized, headers=csrf(r["client"])).status_code == 409
    request = advance(r, request)
    co = tasks(r, request)
    body = action_body(r, request, "delegate", task=co[0], target_employee_id=co[1]["assigned_employee_id"],
                       effective_from=utc(now-timedelta(hours=1)), effective_to=utc(now+timedelta(hours=1)))
    response = r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=body, headers=csrf(r["client"]))
    if response.status_code == 200:
        request = response.json()
        request = act(r, request, task=co[0], employee_id=co[1]["assigned_employee_id"])
        duplicate_actor = action_body(r, request, task=co[1])
        assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=duplicate_actor, headers=csrf(r["client"])).status_code == 409
        assert request["current_stage"] == 3
    else:
        assert response.status_code == 409, response.text
        assert len(tasks(r, request)) == 2
    assert_business_unchanged(r)


def test_material_source_edit_requires_new_review_and_snapshots_events_and_versions_are_sql_immutable(qa):
    r = initialize(qa); request = submit(r)
    attempts = read_sql(r["path"], "SELECT * FROM approval_attempt WHERE request_id=?", (request["id"],))
    definition = r["definitions"]["PURCHASE_ORDER"]
    configuration = []
    for stage in read_sql(r["path"], "SELECT * FROM approval_stage WHERE definition_id=? ORDER BY stage_no", (definition["id"],)):
        slots = read_sql(r["path"], "SELECT kind,employee_id,role_id FROM approval_slot WHERE stage_id=? ORDER BY slot_no", (stage["id"],))
        configuration.append(dict(name=stage["name"] + " 新版", assignees=slots))
    revised = post(r["client"], BASE + "/definitions", dict(employee_id=r["requester"], request_key=str(uuid4()),
                   code=definition["code"], name=definition["name"] + " 新版", source_type="PURCHASE_ORDER", stages=configuration,
                   row_version=definition["version"]), (200, 201))
    assert revised["version"] == 2 and revised["id"] != definition["id"]
    frozen = current(r, request)
    assert frozen["definition_id"] == definition["id"] and frozen["definition_version"] == 1
    assert not any(stage["name"].endswith(" 新版") for stage in frozen["stages"])
    source = r["metadata"]["sources"]["PURCHASE_ORDER"]
    response = r["client"].put("/api/purchase-orders/" + source["id"], json=purchase_edit(source, note="Material change after submission"))
    assert response.status_code == 200, response.text
    expected_business = business_rows(r["path"], r["metadata"]["business_tables"])
    old_task = tasks(r, request)[0]
    body = action_body(r, request, task=old_task)
    assert r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=body, headers=csrf(r["client"])).status_code == 409
    request = act(r, request, "resubmit", comment="New document revision review")
    assert request["attempt"] == 2 and request["current_stage"] == 1
    fresh = read_sql(r["path"], "SELECT * FROM approval_attempt WHERE request_id=? ORDER BY attempt_no", (request["id"],))
    assert fresh[0] == attempts[0] and fresh[1]["fingerprint"] != fresh[0]["fingerprint"]
    assert fresh[1]["snapshot_json"] != fresh[0]["snapshot_json"]
    request = advance(r, request)
    assert request["current_stage"] == 2
    with closing(sqlite3.connect(isolated(r["path"]))) as con:
        for table, column in (("approval_event", "comment"), ("approval_attempt", "fingerprint"),
                              ("approval_definition", "name"), ("approval_stage", "name"),
                              ("approval_slot", "kind"), ("approval_cycle", "reason")):
            before = con.execute("SELECT * FROM " + table).fetchall()
            assert before
            for statement in (f"UPDATE {table} SET {column}={column}", f"DELETE FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    con.execute(statement)
                con.rollback()
                assert con.execute("SELECT * FROM " + table).fetchall() == before
    assert business_rows(r["path"], r["metadata"]["business_tables"]) == expected_business

    # Existing source-reference protection may itself prohibit editing a linked
    # manufacturing basis. Preserve that rule, then change the editable WO's
    # own persisted header through its genuine API to verify version detection.
    work = submit(r, "WORK_ORDER")
    source_work = r["metadata"]["sources"]["WORK_ORDER"]
    bom = r["client"].get("/api/boms/" + source_work["bom_id"]).json()
    component = bom["components"][0]
    body = {key: component[key] for key in ("material_id", "usage_qty_base", "purpose_note", "row_version")}
    body.update(bom_version=bom["row_version"], purpose_note="Material requirement changed after WO review submission")
    response = r["client"].put("/api/boms/" + bom["id"] + "/components/" + component["id"], json=body)
    assert response.status_code == 409, response.text
    assert business_rows(r["path"], r["metadata"]["business_tables"]) == expected_business
    assert current(r, work)["source_changed"] is False
    response = r["client"].put("/api/work-orders/" + source_work["id"], json=work_edit(source_work, planned_qty_base="3.75"))
    assert response.status_code == 200, response.text
    expected_business = business_rows(r["path"], r["metadata"]["business_tables"])
    assert current(r, work)["source_changed"] is True
    stale = action_body(r, work, task=tasks(r, work)[0])
    assert r["client"].post(BASE + "/requests/" + work["id"] + "/actions", json=stale, headers=csrf(r["client"])).status_code == 409
    assert business_rows(r["path"], r["metadata"]["business_tables"]) == expected_business
    assert r["client"].delete("/api/work-orders/" + source_work["id"], params={"row_version": response.json()["row_version"]}).status_code == 409

    sales = submit(r, "SALES_ORDER")
    source_sales = r["metadata"]["sources"]["SALES_ORDER"]
    response = r["client"].put("/api/sales-orders/" + source_sales["id"], json=sales_edit(source_sales, note="Sales content changed after submission"))
    assert response.status_code == 200, response.text
    expected_business = business_rows(r["path"], r["metadata"]["business_tables"])
    assert current(r, sales)["source_changed"] is True
    stale = action_body(r, sales, task=tasks(r, sales)[0])
    assert r["client"].post(BASE + "/requests/" + sales["id"] + "/actions", json=stale, headers=csrf(r["client"])).status_code == 409
    sales = act(r, sales, "resubmit")
    assert sales["attempt"] == 2 and sales["source_changed"] is False
    assert r["client"].delete("/api/sales-orders/" + source_sales["id"], params={"row_version": response.json()["row_version"]}).status_code == 409
    historical = read_sql(r["path"], "SELECT work_order_id FROM material_issue WHERE id=?", (r["metadata"]["historical_issue_id"],))[0]
    old_work = r["client"].get("/api/work-orders/" + historical["work_order_id"]).json()
    response = r["client"].put("/api/work-orders/" + old_work["id"], json=work_edit(old_work, note="Must remain protected by existing issue history"))
    assert response.status_code == 409, response.text
    decision = next(e for e in events(r, request) if e["action"] == "approve")
    original_identity = json.loads(decision["data_json"])["actor_identity"]
    person = next(person for person in r["client"].get("/api/employees").json() if person["id"] == decision["actor_employee_id"])
    response = edit_master(r["client"], "/api/employees", person, name=person["name"] + " 已改名", is_active=False)
    assert response.status_code == 200, response.text
    historical_event = next(e for e in current(r, request)["events"] if e["id"] == decision["id"])
    assert historical_event["actor_employee_id"] == person["id"]
    assert historical_event["actor_name"] == original_identity["name"]
    assert historical_event["data"]["actor_identity"] == original_identity
    assert r["client"].delete("/api/employees/" + person["id"], params={"row_version": response.json()["row_version"]}, headers=csrf(r["client"])).status_code == 409
    assert business_rows(r["path"], r["metadata"]["business_tables"]) == expected_business


def test_idempotent_submission_decision_conflicts_stale_task_and_double_click(qa):
    r = initialize(qa); c = r["client"]; body = submit_body(r)
    request = post(c, BASE + "/requests", body, (200, 201))
    before = events(r, request)
    assert post(c, BASE + "/requests", body, (200, 201)) == request
    assert events(r, request) == before
    conflict = {**body, "source_type": "SALES_ORDER", "source_id": r["metadata"]["sources"]["SALES_ORDER"]["id"], "definition_id": r["definitions"]["SALES_ORDER"]["id"]}
    assert c.post(BASE + "/requests", json=conflict, headers=csrf(c)).status_code == 409
    task = tasks(r, request)[0]
    decision = action_body(r, request, task=task)
    request = post(c, BASE + "/requests/" + request["id"] + "/actions", decision)
    assert post(c, BASE + "/requests/" + request["id"] + "/actions", decision) == request
    altered = {**decision, "action": "reject", "reason": "Conflicting replay"}
    assert c.post(BASE + "/requests/" + request["id"] + "/actions", json=altered, headers=csrf(c)).status_code == 409
    stale = {**decision, "request_key": str(uuid4())}
    assert c.post(BASE + "/requests/" + request["id"] + "/actions", json=stale, headers=csrf(c)).status_code == 409
    assert len([e for e in events(r, request) if e["action"] == "approve"]) == 1
    task = tasks(r, request)[0]
    duplicate = action_body(r, request, task=task)
    valid_headers = csrf(c)
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda _: c.post(BASE + "/requests/" + request["id"] + "/actions", json=duplicate, headers=valid_headers), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert len([e for e in events(r, request) if e["action"] == "approve"]) == 2
    assert_business_unchanged(r)


def concurrent_actions(r, request, bodies):
    barrier = threading.Barrier(len(bodies))
    valid_headers = csrf(r["client"])
    def send(body):
        barrier.wait(timeout=5)
        return r["client"].post(BASE + "/requests/" + request["id"] + "/actions", json=body, headers=valid_headers)
    with ThreadPoolExecutor(max_workers=len(bodies)) as workers:
        return list(workers.map(send, bodies))


def test_cosign_concurrency_and_return_race_have_serial_transactional_outcomes(qa):
    r = initialize(qa); request = advance(r, advance(r, submit(r)))
    co = tasks(r, request)
    responses = concurrent_actions(r, request, [action_body(r, request, task=task) for task in co])
    assert sorted(response.status_code for response in responses) == [200, 409]
    request = current(r, request)
    assert request["current_stage"] == 3 and len(tasks(r, request)) == 1
    request = advance(r, request)
    assert request["current_stage"] == 4
    assert len([e for e in events(r, request) if e["action"] == "approve" and e["stage_no"] == 3]) == 2
    request = act(r, request, "withdraw")
    request = act(r, request, "resubmit")
    request = advance(r, advance(r, request)); co = tasks(r, request)
    before_cycle = request["cycle"]
    responses = concurrent_actions(r, request, [action_body(r, request, task=co[0]), action_body(r, request, "return", task=co[1], reason="Concurrent return")])
    assert sorted(response.status_code for response in responses) == [200, 409]
    request = current(r, request)
    if request["current_stage"] == 3:
        request = act(r, request, "return", task=co[1], reason="Retry after stale concurrent return")
    assert request["current_stage"] == 2 and request["cycle"] > before_cycle
    assert len(tasks(r, request)) == 1
    request = advance(r, request)
    assert request["current_stage"] == 3 and len(tasks(r, request)) == 2
    assert_business_unchanged(r)


def test_managed_backup_clear_restore_preserves_new_history_and_immutable_triggers(qa):
    r = initialize(qa); c = r["client"]; request = submit(r)
    for _ in range(4):
        request = advance(r, request)
    assert request["status"] == "APPROVED"
    tables = [row["name"] for row in read_sql(r["path"], "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    def snapshot():
        return {table: sorted(read_sql(r["path"], 'SELECT * FROM "' + table + '"'), key=repr) for table in tables}
    before = snapshot()
    triggers = read_sql(r["path"], "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
    maintenance = "/api/sqlite-maintenance"
    def headers():
        response = c.get(maintenance + "/status")
        assert response.status_code == 200, response.text
        return {"X-ERP-Maintenance-Token": response.json()["csrf_token"], "Origin": ORIGIN}
    response = c.post(maintenance + "/backups", json={}, headers=headers())
    assert response.status_code in {200, 201}, response.text
    saved = response.json()["backup"]
    response = c.post(maintenance + "/review", json={"operation": "clear"}, headers=headers())
    assert response.status_code == 200, response.text
    response = c.post(maintenance + "/clear", json={"confirmation": "清空", "confirmation_token": response.json()["confirmation_token"]}, headers=headers())
    assert response.status_code == 200, response.text
    for table in tables:
        if table.startswith("approval_") or table.startswith("erp_"):
            assert read_sql(r["path"], 'SELECT count(*) n FROM "' + table + '"')[0]["n"] == 0
    response = c.post(maintenance + "/review", json={"operation": "restore", "backup_id": saved["id"]}, headers=headers())
    assert response.status_code == 200, response.text
    response = c.post(maintenance + "/restore", json={"backup_id": saved["id"], "confirmed": True, "confirmation_token": response.json()["confirmation_token"]}, headers=headers())
    assert response.status_code == 200, response.text
    assert snapshot() == before
    assert read_sql(r["path"], "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name") == triggers
    assert current(r, request) == request
    with closing(sqlite3.connect(isolated(r["path"]))) as con:
        register_exact_sql(con)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute("UPDATE approval_event SET comment='forbidden after restore'")
        con.rollback()
        assert con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert_business_unchanged(r)
