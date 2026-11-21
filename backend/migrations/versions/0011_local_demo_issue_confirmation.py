"""Permit scoped manual local-demo issue authorization, preserving existing events."""
import re

from alembic import op

revision = "0011_local_demo_issues"
down_revision = "0010_material_issues"
branch_labels = None
depends_on = None

POSTING = "material_issue_posting"
TEMPORARY = "_material_issue_posting_0011"
CONFIRMATION_COLUMNS = (
    "confirmation_digest", "confirmation_snapshot", "confirmation_received_at",
)


def authorization(alias):
    """The SQLite connection receives these request-scoped grants from the server."""
    return f"""COALESCE((
        ({alias}.authorization_basis='CONTROLLED_QA_ONLY'
         AND COALESCE(erp_issue_test_authorized(),0)=1
         AND {alias}.confirmation_digest IS NULL
         AND {alias}.confirmation_snapshot IS NULL
         AND {alias}.confirmation_received_at IS NULL)
        OR
        ({alias}.authorization_basis='LOCAL_DEMO_MANUAL'
         AND COALESCE(erp_issue_manual_authorized(),0)=1
         AND erp_issue_manual_document() IS {alias}.material_issue_id
         AND erp_issue_manual_digest() IS {alias}.confirmation_digest
         AND erp_issue_manual_snapshot() IS {alias}.confirmation_snapshot
         AND erp_issue_manual_time() IS {alias}.confirmation_received_at)
    ),0)=1"""


def replace_once(sql, old, new):
    if sql.count(old) != 1:
        raise RuntimeError("Expected one material-issue authorization fragment")
    return sql.replace(old, new, 1)


def revise_trigger(name, sql):
    old_gate = "SELECT RAISE(ABORT,'material_issue policy pending') WHERE erp_issue_test_authorized()<>1;"
    if name == "material_issue_posting_prepare":
        return replace_once(sql, old_gate,
            "SELECT RAISE(ABORT,'material_issue policy pending: authorization context denied') WHERE NOT ("
            + authorization("NEW") + ");")
    if name == "material_issue_entry_outbound":
        sql = replace_once(sql,
            "WHEN erp_issue_test_authorized()<>1 OR NOT EXISTS(",
            "WHEN NOT EXISTS(")
        return replace_once(sql,
            "WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING'",
            "WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING' AND ("
            + authorization("p") + ")")
    if name == "material_issue_posting_finalize":
        sql = replace_once(sql, old_gate,
            "SELECT RAISE(ABORT,'material_issue policy pending: authorization context denied') WHERE NOT ("
            + authorization("OLD") + ");")
        protected = "NEW.created_by IS OLD.created_by"
        additions = " AND ".join(
            "NEW." + field + " IS OLD." + field for field in CONFIRMATION_COLUMNS)
        return replace_once(sql, protected, protected + " AND " + additions)
    return sql


def dependency_objects(objects):
    """Capture the posting table's view/trigger dependency closure, not all guards."""
    references = {POSTING}
    selected = {}
    while True:
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in references) + r")\b")
        additions = [obj for obj in objects if obj[0] in {"view", "trigger"}
                     and obj[1] not in selected and pattern.search(obj[3])]
        if not additions:
            break
        for obj in additions:
            selected[obj[1]] = obj
            if obj[0] == "view":
                references.add(obj[1])
    return list(selected.values())


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Material-issue migration requires foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing foreign-key violations prevent material-issue migration")

    original = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (POSTING,)).scalar_one()
    original_columns = [row[1] for row in connection.exec_driver_sql(
        "PRAGMA table_info(material_issue_posting)")]
    if any(column in original_columns for column in CONFIRMATION_COLUMNS):
        raise RuntimeError("Unexpected existing material-issue confirmation columns")
    quoted_columns = ",".join('"' + field.replace('"', '""') + '"' for field in original_columns)
    old_rows = connection.exec_driver_sql(
        "SELECT rowid," + quoted_columns + " FROM material_issue_posting ORDER BY rowid").all()

    condition = """authorization_basis IN ('CONTROLLED_QA_ONLY','LOCAL_DEMO_MANUAL')
        AND ((authorization_basis='CONTROLLED_QA_ONLY'
              AND confirmation_digest IS NULL AND confirmation_snapshot IS NULL
              AND confirmation_received_at IS NULL)
          OR (authorization_basis='LOCAL_DEMO_MANUAL'
              AND confirmation_digest IS NOT NULL AND confirmation_snapshot IS NOT NULL
              AND confirmation_received_at IS NOT NULL))"""
    revised = replace_once(original, "authorization_basis='CONTROLLED_QA_ONLY'", condition)
    first_constraint = re.search(
        r"(?m)^([ \t]*)(?:PRIMARY KEY|CONSTRAINT|CHECK|UNIQUE|FOREIGN KEY)\b", revised)
    if first_constraint is None:
        raise RuntimeError("Expected original material-issue table constraints")
    indent = first_constraint[1]
    new_columns = "".join(indent + field + " TEXT,\n" for field in CONFIRMATION_COLUMNS)
    revised = revised[:first_constraint.start()] + new_columns + revised[first_constraint.start():]
    revised, count = re.subn(
        r'^(CREATE TABLE\s+)(?:"material_issue_posting"|material_issue_posting)',
        lambda match: match[1] + '"' + TEMPORARY + '"', revised)
    if count != 1:
        raise RuntimeError("Expected original material-issue posting table definition")

    objects = connection.exec_driver_sql(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE type IN ('view','trigger','index') AND sql IS NOT NULL ORDER BY type,name").all()
    dependent = dependency_objects(objects)
    required = {"material_issue_posting_prepare", "material_issue_entry_outbound",
                "material_issue_posting_finalize", "physical_stock_ledger"}
    if not required.issubset({obj[1] for obj in dependent}):
        raise RuntimeError("Required original material-issue guards or ledger view are missing")
    indexes = [obj for obj in objects if obj[0] == "index" and obj[2] == POSTING]

    # SQLite cannot ALTER an existing CHECK. Preserve this single table and its
    # incoming entry FKs through an atomic replacement while foreign_keys stays ON.
    connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    for kind, name, table, sql in sorted(dependent, key=lambda obj: obj[0] == "view"):
        op.execute("DROP " + kind + ' "' + name.replace('"', '""') + '"')
    op.execute(revised)
    op.execute('INSERT INTO "' + TEMPORARY + '" (rowid,' + quoted_columns + ") "
               "SELECT rowid," + quoted_columns + " FROM material_issue_posting")
    op.execute("DROP TABLE material_issue_posting")
    op.execute('ALTER TABLE "' + TEMPORARY + '" RENAME TO material_issue_posting')
    for kind, name, table, sql in indexes:
        op.execute(sql)
    for kind, name, table, sql in sorted(dependent, key=lambda obj: obj[0] != "view"):
        op.execute(revise_trigger(name, sql) if kind == "trigger" else sql)

    preserved = connection.exec_driver_sql(
        "SELECT rowid," + quoted_columns + " FROM material_issue_posting ORDER BY rowid").all()
    if preserved != old_rows:
        raise RuntimeError("Material-issue posting rows changed during migration")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Material-issue migration produced invalid foreign-key references")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Material-issue migration lost foreign-key enforcement")
    # Deferred DROP records remain in SQLite's counter even after the valid
    # replacement restores every parent identity. Clear that counter only after
    # all original references/guards are restored and explicit FK checking passes.
    connection.exec_driver_sql("PRAGMA defer_foreign_keys=OFF")


def downgrade():
    raise RuntimeError("Local-demo issue downgrade is disabled; preserve verified backup recovery.")
