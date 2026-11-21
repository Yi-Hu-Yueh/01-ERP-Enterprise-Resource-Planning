"""Explicit, collision-safe, marked synthetic organization/configuration setup."""
from uuid import NAMESPACE_URL, uuid5

from .approvals_common import fail, insert, now, one, rows, uid, update
from .approvals_engine import DefinitionInput, definition_output, save_definition
from .approvals_org import master_output

# These are presentation/configuration blueprints, not employee routing logic.
# No rows are created on import, app startup, migration or a GET request.
ROLE_LABELS = {
    "PURCHASING": "採購主管", "FINANCE": "財務主管", "WAREHOUSE": "倉庫主管",
    "GENERAL_MANAGER": "總經理", "PRODUCTION": "生產主管", "MATERIAL": "物料管理主管",
    "QUALITY": "品管主管", "PLANT_MANAGER": "廠長", "SALES": "業務主管",
    "SALES_ADMIN": "銷售管理主管", "OPERATIONS": "營運主管",
}
DEPARTMENT_LABELS = {"REQUEST": "申請部門", **ROLE_LABELS}
WORKFLOWS = [
    ("DEMO-PO-APPROVAL", "採購訂單簽核", "PURCHASE_ORDER", [
        ("申請部門主管", ["requester_manager"]), ("採購主管", ["PURCHASING"]),
        ("財務主管＋倉庫主管會簽", ["FINANCE", "WAREHOUSE"]), ("總經理", ["GENERAL_MANAGER"])]),
    ("DEMO-WO-APPROVAL", "製造工單簽核", "WORK_ORDER", [
        ("生產主管", ["PRODUCTION"]), ("物料管理主管", ["MATERIAL"]),
        ("倉庫主管＋品管主管會簽", ["WAREHOUSE", "QUALITY"]), ("廠長", ["PLANT_MANAGER"])]),
    ("DEMO-SO-APPROVAL", "銷售訂單簽核", "SALES_ORDER", [
        ("業務主管", ["SALES"]), ("銷售管理主管", ["SALES_ADMIN"]),
        ("財務主管＋營運主管會簽", ["FINANCE", "OPERATIONS"]), ("總經理", ["GENERAL_MANAGER"])]),
]


def blueprints():
    return [dict(code=code, name=name, source_type=source_type, stages=[
        dict(name=label, assignees=[dict(kind="requester_manager") if role == "requester_manager"
            else dict(kind="role", role_code="DEMO-" + role, role_name=ROLE_LABELS[role]) for role in assignees])
        for label, assignees in stages]) for code, name, source_type, stages in WORKFLOWS]


def initialize_demo(session):
    departments, people, roles, created_departments = {}, {}, {}, set()
    def ensure(table, code, payload):
        # Stable initializer-only identity survives an Owner editing the demo
        # code/name. The workflow engine itself knows no synthetic IDs or names.
        demo_id = str(uuid5(NAMESPACE_URL, "tiger-erp:approval-demo:" + table + ":" + code))
        old = one(session, f"SELECT * FROM {table} WHERE id=:id", id=demo_id)
        collision = one(session, f"SELECT * FROM {table} WHERE code=:code", code=code)
        if collision and collision["id"] != demo_id:
            fail(409, "示範代碼與既有資料衝突，未覆寫任何資料：" + code)
        if old:
            if not old["is_demo"]:
                fail(409, "示範代碼與既有 Owner 資料衝突，未覆寫任何資料：" + code)
            return old, False
        timestamp = now()
        record = dict(id=demo_id, code=code, is_demo=1, created_at=timestamp, updated_at=timestamp,
                      row_version=1, is_active=1, notes="[DEMO] 合成測試資料／未驗證身分", **payload)
        insert(session, table, record)
        return record, True
    for key, label in DEPARTMENT_LABELS.items():
        department, created = ensure("erp_department", "DEMO-" + key, dict(name="[示範] " + label, parent_id=None, manager_employee_id=None))
        departments[key] = department
        if created:
            created_departments.add(department["id"])
    for key, label in ROLE_LABELS.items():
        role, _ = ensure("approval_role", "DEMO-" + key, dict(name="[示範] " + label))
        roles[key] = role
    for key, label in DEPARTMENT_LABELS.items():
        person, created = ensure("erp_employee", "DEMO-" + key + "-MANAGER",
            dict(name="[示範] " + label + "甲", department_id=departments[key]["id"], title=label, manager_employee_id=None))
        people[key] = person
        if departments[key]["id"] in created_departments:
            update(session, "erp_department", departments[key]["id"], dict(manager_employee_id=person["id"]))
            departments[key]["manager_employee_id"] = person["id"]
        if key in roles and created:
            insert(session, "approval_employee_role", dict(employee_id=person["id"], role_id=roles[key]["id"]))
    applicant, created = ensure("erp_employee", "DEMO-APPLICANT", dict(name="[示範] 申請員乙",
        department_id=departments["REQUEST"]["id"], title="示範申請人", manager_employee_id=people["REQUEST"]["id"]))
    people["APPLICANT"] = applicant
    # A separate synthetic delegate is convenient for exercising representation.
    delegate, _ = ensure("erp_employee", "DEMO-DELEGATE", dict(name="[示範] 代理員丙",
        department_id=departments["REQUEST"]["id"], title="示範代理人", manager_employee_id=people["REQUEST"]["id"]))
    people["DELEGATE"] = delegate
    definitions = []
    for code, name, source_type, stages in WORKFLOWS:
        existing = one(session, "SELECT * FROM approval_definition WHERE code=:code ORDER BY version DESC LIMIT 1", code=code)
        if existing:
            if not existing["is_demo"]:
                fail(409, "示範流程代碼與既有 Owner 設定衝突，未覆寫任何設定：" + code)
            definitions.append(definition_output(session, existing))
            continue
        config = DefinitionInput(employee_id=applicant["id"], request_key="internal-demo-blueprint", code=code,
            name=name, source_type=source_type, stages=[dict(name=label, assignees=[
                dict(kind="requester_manager") if role == "requester_manager" else dict(kind="role", role_id=roles[role]["id"])
                for role in assignees]) for label, assignees in stages], notes="[DEMO] 可編輯，修改另存新版本")
        definitions.append(save_definition(session, config, is_demo=True, created_by=applicant["id"]))
    return dict(departments=[master_output(session, "erp_department", row) for row in rows(session, "SELECT * FROM erp_department WHERE is_demo=1 ORDER BY code")],
        employees=[master_output(session, "erp_employee", row) for row in rows(session, "SELECT * FROM erp_employee WHERE is_demo=1 ORDER BY code")],
        roles=[master_output(session, "approval_role", row) for row in rows(session, "SELECT * FROM approval_role WHERE is_demo=1 ORDER BY code")],
        definitions=definitions, message="示範部門、員工、角色與三個四關流程已就緒；保留既有 Owner 資料及已編輯的示範欄位。")
