"""One versioned approval state machine, with explicit unverified employee IDs.

Requests/tasks are projections; immutable attempts, cycles, decisions and carried
decision references retain exactly what each review considered. All writes run
under Database.write() (SQLite BEGIN IMMEDIATE), including idempotency receipts.
"""
from datetime import datetime, timezone
import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import text

from .approvals_common import canonical, digest, employee, fail, insert, now, one, required, rows, uid, update

SOURCE_TABLE = {"PURCHASE_ORDER": ("purchase_order", "purchase_order_id", "purchase_order_line"),
                "WORK_ORDER": ("work_order", "work_order_id", None),
                "SALES_ORDER": ("sales_order", "sales_order_id", "sales_order_line")}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationInput(Input):
    employee_id: str
    request_key: str


class AssigneeInput(Input):
    kind: Literal["employee", "role", "requester_manager"]
    employee_id: Optional[str] = None
    role_id: Optional[str] = None


class StageInput(Input):
    name: str
    assignees: list[AssigneeInput] = Field(min_length=1)


class DefinitionInput(OperationInput):
    code: str
    name: str
    source_type: Literal["PURCHASE_ORDER", "WORK_ORDER", "SALES_ORDER"]
    stages: list[StageInput] = Field(min_length=1)
    is_active: StrictBool = True
    notes: Optional[str] = None
    row_version: Optional[int] = Field(default=None, ge=1)
    base_version: Optional[int] = Field(default=None, ge=1)


class SubmitInput(OperationInput):
    definition_id: str
    source_type: Literal["PURCHASE_ORDER", "WORK_ORDER", "SALES_ORDER"]
    source_id: str
    assignments: dict[str, str] = Field(default_factory=dict)
    comment: Optional[str] = None
    expected_fingerprint: Optional[str] = None


class ActionInput(OperationInput):
    row_version: int = Field(ge=1)
    cycle: int = Field(ge=1)
    action: Literal["approve", "reject", "return", "withdraw", "resubmit", "withdraw_approval", "add_signer", "delegate"]
    task_id: Optional[str] = None
    target_employee_id: Optional[str] = None
    reason: Optional[str] = None
    comment: Optional[str] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    assignments: dict[str, str] = Field(default_factory=dict)
    expected_fingerprint: Optional[str] = None


def definition_output(session, definition):
    result = dict(definition)
    result["row_version"] = result["version"]
    result["is_active"], result["is_demo"] = bool(result["is_active"]), bool(result["is_demo"])
    stages = rows(session, "SELECT * FROM approval_stage WHERE definition_id=:id ORDER BY stage_no", id=result["id"])
    for stage in stages:
        stage["assignees"] = rows(session, """SELECT s.*, e.name employee_name, r.name role_name, r.code role_code
            FROM approval_slot s LEFT JOIN erp_employee e ON e.id=s.employee_id
            LEFT JOIN approval_role r ON r.id=s.role_id WHERE stage_id=:id ORDER BY slot_no""", id=stage["id"])
        for slot in stage["assignees"]:
            slot["key"] = f'{stage["stage_no"]}.{slot["slot_no"]}'
    result["stages"] = stages
    return result


def save_definition(session, data, is_demo=False, created_by=None):
    code, name = required(data.code, "流程代碼"), required(data.name, "流程名稱")
    previous = one(session, "SELECT * FROM approval_definition WHERE code=:code ORDER BY version DESC LIMIT 1", code=code)
    expected = data.base_version or data.row_version
    if previous and expected != previous["version"]:
        fail(409, "流程定義已更新；請讀取目前版本再另存新版。既有申請保留原版。")
    if previous and previous["source_type"] != data.source_type:
        fail(422, "同一流程代碼的來源單據類型必須一致；不同類型請建立另一流程。")
    if previous and previous["is_demo"]:
        is_demo = True
    if is_demo and (len(data.stages) < 4 or len(data.stages[2].assignees) < 2):
        fail(422, "三個示範流程必須至少四關，第三關至少兩名必要會簽者。")
    record_id = uid()
    record = dict(id=record_id, code=code, name=name, source_type=data.source_type,
                  version=(previous["version"] + 1 if previous else 1), is_active=data.is_active,
                  notes=data.notes, is_demo=int(is_demo), created_at=now(),
                  created_by_employee_id=created_by if created_by is not None else data.employee_id)
    insert(session, "approval_definition", record)
    for number, stage in enumerate(data.stages, 1):
        stage_id = uid()
        insert(session, "approval_stage", dict(id=stage_id, definition_id=record_id, stage_no=number, name=required(stage.name, "關卡名稱")))
        seen = set()
        for slot_no, slot in enumerate(stage.assignees, 1):
            if slot.kind == "employee":
                employee(session, slot.employee_id)
                if slot.role_id:
                    fail(422, "員工指派不可同時設定角色。")
                identity = (slot.kind, slot.employee_id)
            elif slot.kind == "role":
                role = one(session, "SELECT * FROM approval_role WHERE id=:id", id=slot.role_id)
                if not role or not role["is_active"] or slot.employee_id:
                    fail(422, "請選擇啟用的角色，並清除員工指派。")
                identity = (slot.kind, slot.role_id)
            else:
                if slot.role_id or slot.employee_id:
                    fail(422, "申請部門主管由申請人的部門關聯解析，不可同時指定員工或角色。")
                identity = (slot.kind, None)
            if identity in seen:
                fail(422, "同關必要簽核責任不可重複。")
            seen.add(identity)
            insert(session, "approval_slot", dict(id=uid(), stage_id=stage_id, slot_no=slot_no,
                kind=slot.kind, employee_id=slot.employee_id, role_id=slot.role_id))
    return definition_output(session, record)


def source_snapshot(session, source_type, source_id):
    if source_type not in SOURCE_TABLE:
        fail(422, "不支援的簽核來源類型。")
    table, parent_field, line_table = SOURCE_TABLE[source_type]
    header = one(session, f"SELECT * FROM {table} WHERE id=:id", id=source_id)
    if not header:
        fail(404, "來源單據不存在，請重新選擇真實已儲存單據。")
    lines = rows(session, f"SELECT * FROM {line_table} WHERE {parent_field}=:id ORDER BY line_no,id", id=source_id) if line_table else []
    snapshot = dict(source_type=source_type, header=header, lines=lines)
    if source_type == "WORK_ORDER":
        # Existing MRP actually resolves these linked manufacturing definitions.
        # Freeze their exact persisted content so changing the production basis
        # requires a new review. Stock/ledger balances are never part of review.
        snapshot["bom"] = one(session, "SELECT * FROM bom WHERE id=:id", id=header["bom_id"]) if header["bom_id"] else None
        snapshot["bom_components"] = rows(session, "SELECT * FROM bom_component WHERE bom_id=:id ORDER BY line_no,id", id=header["bom_id"]) if header["bom_id"] else []
        snapshot["routing"] = one(session, "SELECT * FROM routing WHERE id=:id", id=header["routing_id"]) if header["routing_id"] else None
        snapshot["routing_steps"] = rows(session, "SELECT * FROM routing_step WHERE routing_id=:id ORDER BY sequence_no,id", id=header["routing_id"]) if header["routing_id"] else []
    return snapshot, digest(snapshot)


def resolved_slots(session, definition_id, requester, assignments):
    definition = one(session, "SELECT * FROM approval_definition WHERE id=:id", id=definition_id)
    if not definition or not definition["is_active"]:
        fail(422, "請選擇啟用的流程版本。")
    current = one(session, "SELECT MAX(version) version FROM approval_definition WHERE code=:code", code=definition["code"])
    if current["version"] != definition["version"]:
        fail(409, "流程已有新版，新的送審請使用最新版。既有申請不受影響。")
    result = []
    allowed_keys = set()
    for stage in definition_output(session, definition)["stages"]:
        resolved, seen = [], set()
        for slot in stage["assignees"]:
            key = slot["key"]
            allowed_keys.add(key)
            chosen = assignments.get(key)
            if slot["kind"] == "employee":
                eligible = [employee(session, slot["employee_id"])]
            elif slot["kind"] == "role":
                role = one(session, "SELECT * FROM approval_role WHERE id=:id", id=slot["role_id"])
                eligible = rows(session, """SELECT e.* FROM erp_employee e JOIN approval_employee_role er ON er.employee_id=e.id
                    WHERE er.role_id=:id AND e.is_active=1 ORDER BY e.code""", id=slot["role_id"]) if role and role["is_active"] else []
            else:
                department = one(session, "SELECT * FROM erp_department WHERE id=:id", id=requester["department_id"])
                manager = department["manager_employee_id"] if department else None
                eligible = [employee(session, manager)] if manager else []
            ids = {record["id"] for record in eligible}
            if chosen and chosen not in ids:
                fail(422, f"第 {stage['stage_no']} 關第 {slot['slot_no']} 位指派員工不符合該責任。")
            if not chosen:
                if len(eligible) != 1:
                    fail(422, f"第 {stage['stage_no']} 關第 {slot['slot_no']} 位有 {len(eligible)} 名符合員工，必須明確選擇一位。")
                chosen = eligible[0]["id"]
            if chosen in seen:
                fail(422, "同關的必要會簽者必須為不同員工，不可由同一模擬員工滿足兩個責任。")
            seen.add(chosen)
            resolved.append(dict(slot_id=slot["id"], slot_no=slot["slot_no"], assigned_employee_id=chosen))
        result.append(dict(stage_id=stage["id"], stage_no=stage["stage_no"], name=stage["name"], slots=resolved))
    if set(assignments) - allowed_keys:
        fail(422, "指派內容包含不存在的關卡責任。")
    return result


def request_row(session, request_id):
    record = one(session, "SELECT * FROM approval_request WHERE id=:id", id=request_id)
    if not record:
        fail(404, "簽核申請不存在。")
    return record


def current_attempt(session, request):
    return one(session, "SELECT * FROM approval_attempt WHERE request_id=:id AND attempt_no=:attempt", id=request["id"], attempt=request["attempt"])


def current_cycle(session, request):
    return one(session, "SELECT * FROM approval_cycle WHERE request_id=:id AND cycle_no=:cycle", id=request["id"], cycle=request["cycle"])


def source_id(request):
    return request[SOURCE_TABLE[request["source_type"]][1]]


def changed_source(session, request):
    snapshot, fingerprint = source_snapshot(session, request["source_type"], source_id(request))
    return snapshot, fingerprint, fingerprint != current_attempt(session, request)["fingerprint"]


def stage_record(session, request, number=None):
    return one(session, "SELECT * FROM approval_stage WHERE definition_id=:id AND stage_no=:number",
               id=request["definition_id"], number=number or request["current_stage"])


def task_delegation(session, task, actor, at=None):
    return one(session, """SELECT * FROM approval_delegation WHERE task_id=:task
        AND delegate_employee_id=:actor AND effective_from<=:at AND effective_to>:at ORDER BY created_at DESC LIMIT 1""",
        task=task["id"], actor=actor, at=at or now())


def task_eligible(session, task, actor):
    return task["assigned_employee_id"] == actor or task_delegation(session, task, actor) is not None


def event(session, request, actor, action, previous_state, new_state, task=None, reason=None, comment=None, data=None):
    stage = stage_record(session, request, task["stage_no"] if task else None)
    def identity(employee_id):
        if not employee_id:
            return None
        person = employee(session, employee_id, active=False)
        department = one(session, "SELECT id,code,name FROM erp_department WHERE id=:id", id=person["department_id"])
        return dict(id=person["id"], code=person["code"], name=person["name"], department=department)
    details = dict(data or {})
    details.update(source_type=request["source_type"], source_id=source_id(request),
        document_no=request["source_document_no"], definition_version=one(session,
            "SELECT version FROM approval_definition WHERE id=:id", id=request["definition_id"])["version"],
        actor_identity=identity(actor), assigned_identity=identity(task["assigned_employee_id"] if task else None),
        represented_identity=identity(task["assigned_employee_id"] if task and task["assigned_employee_id"] != actor else None),
        identity_basis="SELECTED_DEMO_EMPLOYEE_UNVERIFIED")
    record = dict(id=uid(), request_id=request["id"], attempt_id=current_attempt(session, request)["id"],
        cycle_id=current_cycle(session, request)["id"], definition_id=request["definition_id"],
        stage_id=stage["id"], stage_no=stage["stage_no"], task_id=task["id"] if task else None,
        assigned_employee_id=task["assigned_employee_id"] if task else None,
        represented_employee_id=(task["assigned_employee_id"] if task and task["assigned_employee_id"] != actor else None),
        actor_employee_id=actor, action=action, occurred_at=now(), reason=reason, comment=comment,
        previous_state=previous_state, new_state=new_state, data_json=canonical(details))
    insert(session, "approval_event", record)
    return record


def enter_stage(session, request):
    attempt, cycle = current_attempt(session, request), current_cycle(session, request)
    config = next(stage for stage in json.loads(attempt["resolved_json"]) if stage["stage_no"] == request["current_stage"])
    additions = rows(session, "SELECT * FROM approval_added_signer WHERE attempt_id=:id AND stage_no=:stage",
                     id=attempt["id"], stage=request["current_stage"])
    slots = [dict(slot, is_additional=0) for slot in config["slots"]]
    slots.extend(dict(slot_id=None, assigned_employee_id=row["employee_id"], is_additional=1) for row in additions)
    for slot in slots:
        employee(session, slot["assigned_employee_id"])
        insert(session, "approval_task", dict(id=uid(), request_id=request["id"], attempt_id=attempt["id"],
            cycle_id=cycle["id"], stage_id=config["stage_id"], stage_no=request["current_stage"],
            slot_id=slot["slot_id"], assigned_employee_id=slot["assigned_employee_id"],
            is_additional=slot["is_additional"], status="PENDING", created_at=now(), decision_event_id=None))


def start_cycle(session, request, start_stage, reason, carry_from=None):
    attempt = current_attempt(session, request)
    request["cycle"] += 1
    request["current_stage"], request["status"] = start_stage, "PENDING"
    cycle_id = uid()
    insert(session, "approval_cycle", dict(id=cycle_id, request_id=request["id"], attempt_id=attempt["id"],
        cycle_no=request["cycle"], start_stage=start_stage, created_at=now(), reason=reason))
    if carry_from:
        # Earlier completed stages retain explicit references to actual decisions.
        # No fictitious repeated click is manufactured for a carried stage.
        decisions = rows(session, """SELECT e.id,e.stage_no FROM approval_event e
            JOIN approval_task t ON t.decision_event_id=e.id
            WHERE t.cycle_id=:old AND t.status='APPROVED' AND t.stage_no<:stage
            UNION SELECT e.id,e.stage_no FROM approval_carried_decision c JOIN approval_event e ON e.id=c.event_id
            WHERE c.cycle_id=:old AND c.stage_no<:stage""", old=carry_from, stage=start_stage)
        for decision in decisions:
            insert(session, "approval_carried_decision", dict(cycle_id=cycle_id, stage_no=decision["stage_no"], event_id=decision["id"]))
    enter_stage(session, request)


def persist_request(session, request):
    request["row_version"] += 1
    request["updated_at"] = now()
    update(session, "approval_request", request["id"], {key: request[key] for key in
           ("definition_id", "source_document_no", "status", "current_stage", "attempt", "cycle", "row_version", "updated_at")})


def cancel_pending(session, request):
    session.execute(text("UPDATE approval_task SET status='CANCELLED' WHERE request_id=:id AND status='PENDING'"), dict(id=request["id"]))


def new_attempt(session, request, actor, assignments, definition_id=None):
    definition_id = definition_id or request["definition_id"]
    snapshot, fingerprint = source_snapshot(session, request["source_type"], source_id(request))
    resolved = resolved_slots(session, definition_id, employee(session, request["requester_employee_id"]), assignments)
    request["definition_id"] = definition_id
    insert(session, "approval_attempt", dict(id=uid(), request_id=request["id"], attempt_no=request["attempt"],
        definition_id=definition_id, snapshot_json=canonical(snapshot), fingerprint=fingerprint,
        resolved_json=canonical(resolved), created_at=now(), submitted_by_employee_id=actor))


def submit_request(session, data):
    requester = employee(session, data.employee_id)
    definition = one(session, "SELECT * FROM approval_definition WHERE id=:id", id=data.definition_id)
    if not definition or definition["source_type"] != data.source_type:
        fail(422, "流程與來源單據類型不一致。")
    snapshot, fingerprint = source_snapshot(session, data.source_type, data.source_id)
    if data.expected_fingerprint is not None and data.expected_fingerprint != fingerprint:
        fail(409, "來源單據已變更，請取消並重新讀取單據後確認送簽內容。")
    column = SOURCE_TABLE[data.source_type][1]
    if one(session, f"SELECT id FROM approval_request WHERE {column}=:source AND status='PENDING'", source=data.source_id):
        fail(409, "此來源單據已有進行中的簽核；請查看進度，勿重複送審。")
    timestamp = now()
    request = dict(id=uid(), source_type=data.source_type, purchase_order_id=None, work_order_id=None,
        sales_order_id=None, requester_employee_id=requester["id"], definition_id=data.definition_id,
        source_document_no=snapshot["header"]["document_no"], status="PENDING", current_stage=1,
        attempt=1, cycle=0, row_version=1, created_at=timestamp, updated_at=timestamp)
    request[column] = data.source_id
    # First cycle is created below, but the projection itself satisfies schema.
    stored = dict(request, cycle=1)
    insert(session, "approval_request", stored)
    new_attempt(session, request, data.employee_id, data.assignments)
    start_cycle(session, request, 1, "首次送審")
    event(session, request, data.employee_id, "submit", "NEW", "PENDING", comment=data.comment)
    return request_detail(session, request, data.employee_id)


def pending_task(session, request, data):
    tasks = rows(session, """SELECT t.* FROM approval_task t JOIN approval_cycle c ON c.id=t.cycle_id
        WHERE t.request_id=:id AND c.cycle_no=:cycle AND t.stage_no=:stage AND t.status='PENDING'""",
        id=request["id"], cycle=request["cycle"], stage=request["current_stage"])
    choices = [task for task in tasks if task_eligible(session, task, data.employee_id) and (not data.task_id or data.task_id == task["id"])]
    if len(choices) != 1:
        fail(409, "目前模擬員工沒有此關有效待簽責任，或必須明確選擇代理責任；請重新讀取。")
    # Validate both sides of a represented responsibility, independently of UI.
    employee(session, choices[0]["assigned_employee_id"])
    return choices[0]


def own_approval(session, request, actor, task_id=None):
    attempt = current_attempt(session, request)
    choices = rows(session, """SELECT t.*, e.rowid event_sequence FROM approval_task t
        JOIN approval_event e ON e.id=t.decision_event_id
        WHERE t.request_id=:id AND t.attempt_id=:attempt AND t.status='APPROVED'
          AND e.actor_employee_id=:actor ORDER BY e.rowid DESC""", id=request["id"], attempt=attempt["id"], actor=actor)
    if task_id:
        choices = [task for task in choices if task["id"] == task_id]
    for task in choices:
        cycle = current_cycle(session, request)
        valid = task["cycle_id"] == cycle["id"] or one(session,
            "SELECT event_id FROM approval_carried_decision WHERE cycle_id=:cycle AND event_id=:event",
            cycle=cycle["id"], event=task["decision_event_id"])
        if not valid:
            continue
        downstream = one(session, """SELECT e.id FROM approval_event e
            WHERE e.request_id=:id AND e.attempt_id=:attempt AND e.rowid>:sequence AND e.stage_no>:stage
              AND e.action IN ('approve','reject','return','add_signer','delegate','withdraw_approval') LIMIT 1""",
            id=request["id"], attempt=attempt["id"], sequence=task["event_sequence"], stage=task["stage_no"])
        if downstream:
            if task_id:
                fail(409, "後續關卡已有人員操作，不能撤銷本人簽核；原簽核歷史保留。")
            continue
        return task
    return None


def normalize_time(value, label):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            fail(422, label + "必須包含時區。")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, AttributeError):
        fail(422, label + "必須為含時區的有效時間。")


def action_request(session, request_id, data):
    request = request_row(session, request_id)
    if request["row_version"] != data.row_version or request["cycle"] != data.cycle:
        fail(409, "簽核已被其他操作更新或已換關／換輪，請重新讀取；本次未新增決策。")
    previous = request["status"]
    if data.action in ("reject", "return"):
        required(data.reason, "不同意／退回原因")
    if data.action == "resubmit":
        if data.employee_id != request["requester_employee_id"]:
            fail(403, "只有原申請人可重新送審。")
        _, fingerprint, changed = changed_source(session, request)
        if data.expected_fingerprint is not None and data.expected_fingerprint != fingerprint:
            fail(409, "來源單據已變更，請取消並重新讀取單據後確認送簽內容。")
        if request["status"] not in ("REJECTED", "RETURNED", "WITHDRAWN") and not (request["status"] == "PENDING" and changed):
            fail(409, "此申請目前不可重新送審；來源變更或退回／不同意／撤回後可建立新送審。")
        source_column = SOURCE_TABLE[request["source_type"]][1]
        if one(session, f"SELECT id FROM approval_request WHERE {source_column}=:source AND id<>:id AND status='PENDING'",
               source=source_id(request), id=request_id):
            fail(409, "同一來源已有另一件進行中的簽核，不能重新送審形成重複有效申請。")
        cancel_pending(session, request)
        request["attempt"] += 1
        # Preserve frozen definition for retries. A new request may choose a new
        # version, while an old request never silently switches its configuration.
        old_attempt = one(session, "SELECT * FROM approval_attempt WHERE request_id=:id AND attempt_no=:attempt", id=request_id, attempt=request["attempt"] - 1)
        snapshot, fingerprint = source_snapshot(session, request["source_type"], source_id(request))
        request["source_document_no"] = snapshot["header"]["document_no"]
        if data.assignments:
            # Re-resolution must preserve this exact version, even if superseded.
            resolved = resolve_frozen_slots(session, request, data.assignments)
        else:
            resolved = json.loads(old_attempt["resolved_json"])
            for stage in resolved:
                for slot in stage["slots"]:
                    employee(session, slot["assigned_employee_id"])
        insert(session, "approval_attempt", dict(id=uid(), request_id=request_id, attempt_no=request["attempt"],
            definition_id=request["definition_id"], snapshot_json=canonical(snapshot), fingerprint=fingerprint,
            resolved_json=canonical(resolved), created_at=now(), submitted_by_employee_id=data.employee_id))
        start_cycle(session, request, 1, data.reason or "重新送審")
        event(session, request, data.employee_id, "resubmit", previous, "PENDING", reason=data.reason, comment=data.comment,
              data=dict(previous_attempt=request["attempt"] - 1))
    elif data.action == "withdraw":
        if data.employee_id != request["requester_employee_id"]:
            fail(403, "只有原申請人可撤回申請。")
        if request["status"] != "PENDING":
            fail(409, "只有完成前進行中的申請可撤回。")
        request["status"] = "WITHDRAWN"
        cancel_pending(session, request)
        event(session, request, data.employee_id, "withdraw", previous, "WITHDRAWN", reason=data.reason, comment=data.comment)
    elif data.action == "withdraw_approval":
        if request["status"] not in ("PENDING", "APPROVED"):
            fail(409, "申請目前不可撤銷本人簽核。")
        source_column = SOURCE_TABLE[request["source_type"]][1]
        if one(session, f"SELECT id FROM approval_request WHERE {source_column}=:source AND id<>:id AND status='PENDING'",
               source=source_id(request), id=request_id):
            fail(409, "同一來源已有另一件進行中的簽核，不能重新開啟舊申請。")
        task = own_approval(session, request, data.employee_id, data.task_id)
        if not task:
            fail(409, "沒有可安全撤銷的本人簽核；後續關卡若已有人員操作則不可撤銷。")
        old_cycle = current_cycle(session, request)["id"]
        event(session, request, data.employee_id, "withdraw_approval", previous, "PENDING", task=task,
              reason=data.reason, comment=data.comment,
              data=dict(withdrawn_event_id=task["decision_event_id"], reopening_stage=task["stage_no"], new_cycle=request["cycle"] + 1))
        cancel_pending(session, request)
        start_cycle(session, request, task["stage_no"], "撤銷本人簽核：該關全部重新簽核", carry_from=old_cycle)
    else:
        if request["status"] != "PENDING":
            fail(409, "此申請已不同意、退回申請人、撤回或完成，不接受新簽核。")
        _, _, changed = changed_source(session, request)
        if changed:
            fail(409, "來源單據已變更，原快照保留；請由申請人重新送審建立新版本後再簽核。")
        task = pending_task(session, request, data)
        if data.action == "approve":
            duplicate_actor = one(session, """SELECT e.id FROM approval_task t JOIN approval_event e ON e.id=t.decision_event_id
                WHERE t.cycle_id=:cycle AND t.stage_no=:stage AND t.status='APPROVED' AND e.actor_employee_id=:actor""",
                cycle=task["cycle_id"], stage=task["stage_no"], actor=data.employee_id)
            if duplicate_actor:
                fail(409, "同一模擬員工不可在同一關替兩個必要簽核身分表示同意，請切換其他合資格員工。")
            remaining = one(session, "SELECT count(*) n FROM approval_task WHERE cycle_id=:cycle AND stage_no=:stage AND status='PENDING'",
                            cycle=task["cycle_id"], stage=task["stage_no"])["n"]
            stage_count = one(session, "SELECT count(*) n FROM approval_stage WHERE definition_id=:id", id=request["definition_id"])["n"]
            finishing = remaining == 1 and request["current_stage"] == stage_count
            decision = event(session, request, data.employee_id, "approve", previous, "APPROVED" if finishing else "PENDING",
                             task=task, comment=data.comment, reason=data.reason,
                             data=dict(stage_completed=remaining == 1, next_stage=(request["current_stage"] + 1 if remaining == 1 and not finishing else None)))
            update(session, "approval_task", task["id"], dict(status="APPROVED", decision_event_id=decision["id"]))
            if remaining == 1:
                if finishing:
                    request["status"] = "APPROVED"
                else:
                    request["current_stage"] += 1
                    enter_stage(session, request)
        elif data.action == "reject":
            request["status"] = "REJECTED"
            event(session, request, data.employee_id, "reject", previous, "REJECTED", task, data.reason, data.comment)
            cancel_pending(session, request)
        elif data.action == "return":
            previous_stage = request["current_stage"] - 1
            old_cycle = current_cycle(session, request)["id"]
            event(session, request, data.employee_id, "return", previous, "PENDING" if previous_stage else "RETURNED",
                  task, data.reason, data.comment, data=dict(return_to_stage=previous_stage or "requester", new_cycle=request["cycle"] + 1 if previous_stage else None))
            cancel_pending(session, request)
            if previous_stage:
                start_cycle(session, request, previous_stage, data.reason, carry_from=old_cycle)
            else:
                request["status"] = "RETURNED"
        elif data.action == "add_signer":
            target = employee(session, data.target_employee_id)
            existing = one(session, "SELECT id FROM approval_task WHERE cycle_id=:cycle AND stage_no=:stage AND assigned_employee_id=:employee",
                           cycle=task["cycle_id"], stage=task["stage_no"], employee=target["id"])
            if existing:
                fail(409, "該員工已是本關簽核者，加簽不得取代或重複必要簽核者。")
            decision = event(session, request, data.employee_id, "add_signer", previous, previous, task,
                             data.reason, data.comment, data=dict(target_employee_id=target["id"], completion_rule="required_same_stage"))
            addition = dict(id=uid(), request_id=request_id, attempt_id=task["attempt_id"], stage_no=task["stage_no"], employee_id=target["id"], event_id=decision["id"])
            insert(session, "approval_added_signer", addition)
            insert(session, "approval_task", dict(id=uid(), request_id=request_id, attempt_id=task["attempt_id"], cycle_id=task["cycle_id"],
                stage_id=task["stage_id"], stage_no=task["stage_no"], slot_id=None, assigned_employee_id=target["id"],
                is_additional=1, status="PENDING", created_at=now(), decision_event_id=None))
        elif data.action == "delegate":
            if task["assigned_employee_id"] != data.employee_id:
                fail(403, "代理人不得再次轉授；只有原指派員工可以設定自己的待簽責任代理。")
            target = employee(session, data.target_employee_id)
            if target["id"] == task["assigned_employee_id"]:
                fail(422, "代理員工必須與原指派員工不同。")
            start, end = normalize_time(data.effective_from, "代理開始時間"), normalize_time(data.effective_to, "代理結束時間")
            if start >= end:
                fail(422, "代理結束時間必須晚於開始時間。")
            if one(session, """SELECT id FROM approval_delegation WHERE task_id=:task
                AND effective_from<:end AND effective_to>:start""", task=task["id"], start=start, end=end):
                fail(409, "此責任已有重疊的代理期間，請選擇不重疊期間。")
            other = one(session, """SELECT t.id FROM approval_task t WHERE t.cycle_id=:cycle AND t.stage_no=:stage
                AND t.id<>:task AND t.assigned_employee_id=:employee""", cycle=task["cycle_id"], stage=task["stage_no"], task=task["id"], employee=target["id"])
            shared = one(session, """SELECT d.id FROM approval_delegation d JOIN approval_task t ON t.id=d.task_id
                WHERE t.cycle_id=:cycle AND t.stage_no=:stage AND d.task_id<>:task
                AND d.delegate_employee_id=:employee AND d.effective_from<:end AND d.effective_to>:start""",
                cycle=task["cycle_id"], stage=task["stage_no"], task=task["id"], employee=target["id"], start=start, end=end)
            if other or shared:
                fail(409, "代理員工在本關已有另一個必要簽核責任，不能同時代表兩個身分。")
            decision = event(session, request, data.employee_id, "delegate", previous, previous, task, data.reason, data.comment,
                             data=dict(target_employee_id=target["id"], effective_from=start, effective_to=end, responsibility="task_only"))
            insert(session, "approval_delegation", dict(id=uid(), request_id=request_id, task_id=task["id"],
                original_employee_id=task["assigned_employee_id"], delegate_employee_id=target["id"], effective_from=start,
                effective_to=end, created_by_employee_id=data.employee_id, explanation=data.reason or data.comment,
                event_id=decision["id"], created_at=now()))
    persist_request(session, request)
    return request_detail(session, request, data.employee_id)


def resolve_frozen_slots(session, request, assignments):
    """Resolve explicitly supplied choices against the same frozen definition."""
    previous = current_attempt(session, dict(request, attempt=request["attempt"] - 1))
    config = json.loads(previous["resolved_json"])
    valid_keys = set()
    definition = definition_output(session, one(session, "SELECT * FROM approval_definition WHERE id=:id", id=request["definition_id"]))
    for stage, saved in zip(definition["stages"], config):
        seen = set()
        for slot, resolved in zip(stage["assignees"], saved["slots"]):
            key = slot["key"]
            valid_keys.add(key)
            chosen = assignments.get(key, resolved["assigned_employee_id"])
            employee(session, chosen)
            if slot["kind"] == "employee" and chosen != slot["employee_id"]:
                fail(422, "指定員工責任不可改派其他員工。")
            if slot["kind"] == "role" and not one(session, """SELECT er.role_id FROM approval_employee_role er
                JOIN approval_role r ON r.id=er.role_id WHERE er.employee_id=:employee AND er.role_id=:role AND r.is_active=1""", employee=chosen, role=slot["role_id"]):
                fail(422, "重新送審指派員工不符合角色。")
            if slot["kind"] == "requester_manager":
                requester = employee(session, request["requester_employee_id"])
                dept = one(session, "SELECT * FROM erp_department WHERE id=:id", id=requester["department_id"])
                if chosen != dept["manager_employee_id"]:
                    fail(422, "重新送審的申請部門主管不符。")
            if chosen in seen:
                fail(422, "同關必要會簽責任必須指派不同員工。")
            seen.add(chosen)
            resolved["assigned_employee_id"] = chosen
    if set(assignments) - valid_keys:
        fail(422, "指派包含不存在的責任。")
    return config


def request_detail(session, request, actor):
    result = dict(request)
    definition = definition_output(session, one(session, "SELECT * FROM approval_definition WHERE id=:id", id=request["definition_id"]))
    attempt, cycle = current_attempt(session, request), current_cycle(session, request)
    current_snapshot, current_fingerprint, source_changed = changed_source(session, request)
    requester = employee(session, request["requester_employee_id"], active=False)
    result.update(source_id=source_id(request), requester_name=requester["name"], workflow_name=definition["name"],
        definition_version=definition["version"], source_changed=source_changed, current_fingerprint=current_fingerprint,
        fingerprint=attempt["fingerprint"], snapshot=json.loads(attempt["snapshot_json"]), current_snapshot=current_snapshot,
        identity_notice="測試模式／未驗證身分", security="PARTIAL — RISK CONTAINED")
    result["definition"] = definition
    tasks = rows(session, """SELECT t.*,e.name assigned_employee_name,c.cycle_no cycle FROM approval_task t
        JOIN erp_employee e ON e.id=t.assigned_employee_id JOIN approval_cycle c ON c.id=t.cycle_id
        WHERE t.request_id=:id ORDER BY c.cycle_no,t.stage_no,t.created_at,t.id""", id=request["id"])
    for task in tasks:
        task["is_additional"] = bool(task["is_additional"])
        can_act = (request["status"] == "PENDING" and task["cycle_id"] == cycle["id"]
                   and task["stage_no"] == request["current_stage"] and task["status"] == "PENDING"
                   and not source_changed and task_eligible(session, task, actor)
                   and employee(session, task["assigned_employee_id"], active=False)["is_active"] == 1)
        task["can_act"] = bool(can_act)
        task["acting_for_employee_id"] = task["assigned_employee_id"] if can_act and actor != task["assigned_employee_id"] else None
        task["available_actions"] = ["approve", "reject", "return", "add_signer"] if can_act else []
        if can_act and actor == task["assigned_employee_id"]:
            task["available_actions"].append("delegate")
    result["tasks"] = tasks
    carries = rows(session, "SELECT * FROM approval_carried_decision WHERE cycle_id=:id", id=cycle["id"])
    stages = []
    for stage in definition["stages"]:
        stage_tasks = [task for task in tasks if task["cycle_id"] == cycle["id"] and task["stage_no"] == stage["stage_no"]]
        carried = [row["event_id"] for row in carries if row["stage_no"] == stage["stage_no"]]
        stage_status = "CARRIED" if carried else "APPROVED" if stage_tasks and all(t["status"] == "APPROVED" for t in stage_tasks) else "PENDING" if stage_tasks and any(t["status"] == "PENDING" for t in stage_tasks) else "CANCELLED" if stage_tasks else "WAITING"
        stages.append(dict(stage_no=stage["stage_no"], name=stage["name"], status=stage_status,
                           tasks=stage_tasks, carried_event_ids=carried, assignees=stage["assignees"]))
    result["stages"] = stages
    result["pending_assignees"] = [dict(id=task["assigned_employee_id"], name=task["assigned_employee_name"])
        for task in tasks if task["cycle_id"] == cycle["id"] and task["status"] == "PENDING" and task["stage_no"] == request["current_stage"]]
    events = rows(session, """SELECT e.*,a.name actor_name,c.cycle_no cycle,at.attempt_no attempt,
        represented.name represented_employee_name,assigned.name assigned_employee_name
        FROM approval_event e JOIN erp_employee a ON a.id=e.actor_employee_id
        JOIN approval_cycle c ON c.id=e.cycle_id JOIN approval_attempt at ON at.id=e.attempt_id
        LEFT JOIN erp_employee represented ON represented.id=e.represented_employee_id
        LEFT JOIN erp_employee assigned ON assigned.id=e.assigned_employee_id
        WHERE e.request_id=:id ORDER BY e.rowid""", id=request["id"])
    for decision in events:
        decision["data"] = json.loads(decision.pop("data_json"))
        # Show the immutable identity seen at action time, even after master edits.
        decision["actor_name"] = decision["data"]["actor_identity"]["name"]
        for field in ("assigned", "represented"):
            identity = decision["data"].get(field + "_identity")
            decision[field + "_employee_name"] = identity["name"] if identity else None
    result["events"] = events
    result["delegations"] = rows(session, "SELECT * FROM approval_delegation WHERE request_id=:id ORDER BY created_at,id", id=request["id"])
    result["attempts"] = rows(session, "SELECT id,attempt_no,definition_id,fingerprint,created_at,submitted_by_employee_id FROM approval_attempt WHERE request_id=:id ORDER BY attempt_no", id=request["id"])
    result["cycles"] = rows(session, "SELECT * FROM approval_cycle WHERE request_id=:id ORDER BY cycle_no", id=request["id"])
    available = []
    if request["requester_employee_id"] == actor:
        if request["status"] == "PENDING":
            available.append("withdraw")
        if request["status"] in ("REJECTED", "RETURNED", "WITHDRAWN") or (request["status"] == "PENDING" and source_changed):
            available.append("resubmit")
    if request["status"] in ("PENDING", "APPROVED") and own_approval(session, request, actor):
        available.append("withdraw_approval")
    result["available_actions"] = available
    return result
