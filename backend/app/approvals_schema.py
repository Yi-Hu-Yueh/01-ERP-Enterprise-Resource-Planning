"""Approval-only relational schema; migrations deliberately contain no demo rows.

The workflow is an unauthenticated local simulation. It never writes source
documents, posting authorizations, inventory or the legacy actor identity.
"""

REF = "REFERENCES {table}(id) ON UPDATE RESTRICT ON DELETE RESTRICT"


def ref(table):
    return REF.format(table=table)


TABLES = {
    "erp_department": f"""id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      parent_id TEXT {ref('erp_department')}, manager_employee_id TEXT {ref('erp_employee')},
      is_active INTEGER NOT NULL CHECK(is_active IN (0,1)), notes TEXT,
      is_demo INTEGER NOT NULL DEFAULT 0 CHECK(is_demo IN (0,1)),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      row_version INTEGER NOT NULL CHECK(row_version>0), CHECK(length(trim(code))>0), CHECK(length(trim(name))>0)""",
    "erp_employee": f"""id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      department_id TEXT NOT NULL {ref('erp_department')}, title TEXT,
      manager_employee_id TEXT {ref('erp_employee')},
      is_active INTEGER NOT NULL CHECK(is_active IN (0,1)), notes TEXT,
      is_demo INTEGER NOT NULL DEFAULT 0 CHECK(is_demo IN (0,1)),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      row_version INTEGER NOT NULL CHECK(row_version>0), CHECK(length(trim(code))>0), CHECK(length(trim(name))>0)""",
    "approval_role": """id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
      is_active INTEGER NOT NULL CHECK(is_active IN (0,1)), notes TEXT,
      is_demo INTEGER NOT NULL DEFAULT 0 CHECK(is_demo IN (0,1)),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      row_version INTEGER NOT NULL CHECK(row_version>0), CHECK(length(trim(code))>0), CHECK(length(trim(name))>0)""",
    "approval_employee_role": f"""employee_id TEXT NOT NULL {ref('erp_employee')},
      role_id TEXT NOT NULL {ref('approval_role')}, PRIMARY KEY(employee_id,role_id)""",
    "approval_definition": f"""id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL,
      version INTEGER NOT NULL CHECK(version>0), source_type TEXT NOT NULL
      CHECK(source_type IN ('PURCHASE_ORDER','WORK_ORDER','SALES_ORDER')),
      is_active INTEGER NOT NULL CHECK(is_active IN (0,1)), notes TEXT,
      is_demo INTEGER NOT NULL DEFAULT 0 CHECK(is_demo IN (0,1)),
      created_at TEXT NOT NULL, created_by_employee_id TEXT {ref('erp_employee')},
      UNIQUE(code,version)""",
    "approval_stage": f"""id TEXT PRIMARY KEY, definition_id TEXT NOT NULL {ref('approval_definition')},
      stage_no INTEGER NOT NULL CHECK(stage_no>0), name TEXT NOT NULL,
      UNIQUE(definition_id,stage_no)""",
    "approval_slot": f"""id TEXT PRIMARY KEY, stage_id TEXT NOT NULL {ref('approval_stage')},
      slot_no INTEGER NOT NULL CHECK(slot_no>0), kind TEXT NOT NULL
      CHECK(kind IN ('employee','role','requester_manager')),
      employee_id TEXT {ref('erp_employee')}, role_id TEXT {ref('approval_role')},
      CHECK((kind='employee' AND employee_id IS NOT NULL AND role_id IS NULL)
        OR (kind='role' AND role_id IS NOT NULL AND employee_id IS NULL)
        OR (kind='requester_manager' AND employee_id IS NULL AND role_id IS NULL)),
      UNIQUE(stage_id,slot_no)""",
    "approval_request": f"""id TEXT PRIMARY KEY,
      source_type TEXT NOT NULL CHECK(source_type IN ('PURCHASE_ORDER','WORK_ORDER','SALES_ORDER')),
      purchase_order_id TEXT {ref('purchase_order')}, work_order_id TEXT {ref('work_order')},
      sales_order_id TEXT {ref('sales_order')}, requester_employee_id TEXT NOT NULL {ref('erp_employee')},
      definition_id TEXT NOT NULL {ref('approval_definition')}, source_document_no TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','RETURNED','WITHDRAWN')),
      current_stage INTEGER NOT NULL CHECK(current_stage>0),
      attempt INTEGER NOT NULL CHECK(attempt>0), cycle INTEGER NOT NULL CHECK(cycle>0),
      row_version INTEGER NOT NULL CHECK(row_version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      CHECK((source_type='PURCHASE_ORDER' AND purchase_order_id IS NOT NULL AND work_order_id IS NULL AND sales_order_id IS NULL)
        OR (source_type='WORK_ORDER' AND work_order_id IS NOT NULL AND purchase_order_id IS NULL AND sales_order_id IS NULL)
        OR (source_type='SALES_ORDER' AND sales_order_id IS NOT NULL AND purchase_order_id IS NULL AND work_order_id IS NULL))""",
    "approval_attempt": f"""id TEXT PRIMARY KEY, request_id TEXT NOT NULL {ref('approval_request')},
      attempt_no INTEGER NOT NULL CHECK(attempt_no>0), definition_id TEXT NOT NULL {ref('approval_definition')},
      snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)), fingerprint TEXT NOT NULL,
      resolved_json TEXT NOT NULL CHECK(json_valid(resolved_json)), created_at TEXT NOT NULL,
      submitted_by_employee_id TEXT NOT NULL {ref('erp_employee')}, UNIQUE(request_id,attempt_no)""",
    "approval_cycle": f"""id TEXT PRIMARY KEY, request_id TEXT NOT NULL {ref('approval_request')},
      attempt_id TEXT NOT NULL {ref('approval_attempt')}, cycle_no INTEGER NOT NULL CHECK(cycle_no>0),
      start_stage INTEGER NOT NULL CHECK(start_stage>0), created_at TEXT NOT NULL, reason TEXT,
      UNIQUE(request_id,cycle_no)""",
    "approval_task": f"""id TEXT PRIMARY KEY, request_id TEXT NOT NULL {ref('approval_request')},
      attempt_id TEXT NOT NULL {ref('approval_attempt')}, cycle_id TEXT NOT NULL {ref('approval_cycle')},
      stage_id TEXT NOT NULL {ref('approval_stage')}, stage_no INTEGER NOT NULL CHECK(stage_no>0),
      slot_id TEXT {ref('approval_slot')}, assigned_employee_id TEXT NOT NULL {ref('erp_employee')},
      is_additional INTEGER NOT NULL CHECK(is_additional IN (0,1)),
      status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','CANCELLED')),
      created_at TEXT NOT NULL, decision_event_id TEXT {ref('approval_event')},
      UNIQUE(cycle_id,stage_no,assigned_employee_id)""",
    "approval_event": f"""id TEXT PRIMARY KEY, request_id TEXT NOT NULL {ref('approval_request')},
      attempt_id TEXT NOT NULL {ref('approval_attempt')}, cycle_id TEXT NOT NULL {ref('approval_cycle')},
      definition_id TEXT NOT NULL {ref('approval_definition')}, stage_id TEXT NOT NULL {ref('approval_stage')},
      stage_no INTEGER NOT NULL CHECK(stage_no>0), task_id TEXT {ref('approval_task')},
      assigned_employee_id TEXT {ref('erp_employee')}, represented_employee_id TEXT {ref('erp_employee')},
      actor_employee_id TEXT NOT NULL {ref('erp_employee')}, action TEXT NOT NULL,
      occurred_at TEXT NOT NULL, reason TEXT, comment TEXT,
      previous_state TEXT NOT NULL, new_state TEXT NOT NULL,
      data_json TEXT NOT NULL CHECK(json_valid(data_json))""",
    "approval_carried_decision": f"""cycle_id TEXT NOT NULL {ref('approval_cycle')},
      stage_no INTEGER NOT NULL CHECK(stage_no>0), event_id TEXT NOT NULL {ref('approval_event')},
      PRIMARY KEY(cycle_id,event_id)""",
    "approval_added_signer": f"""id TEXT PRIMARY KEY, request_id TEXT NOT NULL {ref('approval_request')},
      attempt_id TEXT NOT NULL {ref('approval_attempt')}, stage_no INTEGER NOT NULL CHECK(stage_no>0),
      employee_id TEXT NOT NULL {ref('erp_employee')}, event_id TEXT NOT NULL {ref('approval_event')},
      UNIQUE(attempt_id,stage_no,employee_id)""",
    "approval_delegation": f"""id TEXT PRIMARY KEY, request_id TEXT NOT NULL {ref('approval_request')},
      task_id TEXT NOT NULL {ref('approval_task')}, original_employee_id TEXT NOT NULL {ref('erp_employee')},
      delegate_employee_id TEXT NOT NULL {ref('erp_employee')},
      effective_from TEXT NOT NULL, effective_to TEXT NOT NULL,
      created_by_employee_id TEXT NOT NULL {ref('erp_employee')}, explanation TEXT,
      event_id TEXT NOT NULL {ref('approval_event')}, created_at TEXT NOT NULL,
      CHECK(original_employee_id<>delegate_employee_id), CHECK(effective_from<effective_to)""",
    "approval_operation_receipt": f"""request_key TEXT PRIMARY KEY, operation TEXT NOT NULL,
      employee_id TEXT {ref('erp_employee')}, payload_hash TEXT NOT NULL,
      response_json TEXT NOT NULL CHECK(json_valid(response_json)), created_at TEXT NOT NULL""",
}

IMMUTABLE = ("approval_definition", "approval_stage", "approval_slot", "approval_attempt",
             "approval_cycle", "approval_event", "approval_carried_decision", "approval_added_signer",
             "approval_delegation", "approval_operation_receipt")


def statements():
    for table, columns in TABLES.items():
        yield f'CREATE TABLE "{table}" ({columns})'
    for table in IMMUTABLE:
        for operation in ("UPDATE", "DELETE"):
            yield f'''CREATE TRIGGER {table}_immutable_{operation.lower()} BEFORE {operation} ON {table}
              BEGIN SELECT RAISE(ABORT,'approval immutable history'); END'''
    yield "CREATE INDEX approval_inbox ON approval_task(assigned_employee_id,status,request_id)"
    yield "CREATE INDEX approval_history ON approval_event(request_id,occurred_at,id)"
    yield "CREATE INDEX approval_source ON approval_request(source_type,purchase_order_id,work_order_id,sales_order_id,status)"
    for source_column in ("purchase_order_id", "work_order_id", "sales_order_id"):
        yield f"CREATE UNIQUE INDEX approval_one_pending_{source_column} ON approval_request({source_column}) WHERE status='PENDING' AND {source_column} IS NOT NULL"
    # Prevent cycles even for direct SQL writes, not only the UI/API.
    for table, column in (("erp_department", "parent_id"), ("erp_employee", "manager_employee_id")):
        for operation in ("INSERT", "UPDATE"):
            yield f'''CREATE TRIGGER {table}_hierarchy_{operation.lower()} BEFORE {operation} ON {table}
              WHEN NEW.{column} IS NOT NULL BEGIN
                SELECT RAISE(ABORT,'organization hierarchy cycle') WHERE NEW.{column}=NEW.id OR EXISTS(
                  WITH RECURSIVE ancestors(id) AS (SELECT NEW.{column} UNION
                    SELECT t.{column} FROM {table} t JOIN ancestors a ON t.id=a.id WHERE t.{column} IS NOT NULL)
                  SELECT 1 FROM ancestors WHERE id=NEW.id);
              END'''
    yield '''CREATE TRIGGER approval_request_identity BEFORE UPDATE ON approval_request
      WHEN NEW.id<>OLD.id OR NEW.source_type<>OLD.source_type
        OR NEW.purchase_order_id IS NOT OLD.purchase_order_id OR NEW.work_order_id IS NOT OLD.work_order_id
        OR NEW.sales_order_id IS NOT OLD.sales_order_id OR NEW.requester_employee_id<>OLD.requester_employee_id
        OR NEW.created_at<>OLD.created_at
      BEGIN SELECT RAISE(ABORT,'approval request identity immutable'); END'''
    yield '''CREATE TRIGGER approval_request_preserve BEFORE DELETE ON approval_request
      BEGIN SELECT RAISE(ABORT,'approval request history must be preserved'); END'''
    yield '''CREATE TRIGGER approval_task_identity BEFORE UPDATE ON approval_task
      WHEN NEW.id<>OLD.id OR NEW.request_id<>OLD.request_id OR NEW.attempt_id<>OLD.attempt_id
        OR NEW.cycle_id<>OLD.cycle_id OR NEW.stage_id<>OLD.stage_id OR NEW.stage_no<>OLD.stage_no
        OR NEW.slot_id IS NOT OLD.slot_id OR NEW.assigned_employee_id<>OLD.assigned_employee_id
        OR NEW.is_additional<>OLD.is_additional OR NEW.created_at<>OLD.created_at
      BEGIN SELECT RAISE(ABORT,'approval task assignment immutable'); END'''
    yield '''CREATE TRIGGER approval_task_preserve BEFORE DELETE ON approval_task
      BEGIN SELECT RAISE(ABORT,'approval task history must be preserved'); END'''
