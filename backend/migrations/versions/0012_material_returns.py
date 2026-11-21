"""Add exact same-source manufacturing returns; Owner posting remains closed."""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "0012_material_returns"
down_revision = "0011_local_demo_issues"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")


def decimal(column):
    check = foundation.decimal_check(column)
    check.name = "ck_material_return_" + column
    return check


def utc(column):
    """The project's UTCText wire/storage form, with six fractional digits."""
    digit = "[0-9]"
    pattern = digit * 4 + "-" + digit * 2 + "-" + digit * 2 + "T"
    pattern += digit * 2 + ":" + digit * 2 + ":" + digit * 2 + "." + digit * 6 + "Z"
    return sa.CheckConstraint(
        f"typeof({column})='text' AND length({column})=27 AND {column} GLOB '{pattern}' "
        f"AND substr({column},1,4)<>'0000' AND substr({column},12,2) BETWEEN '00' AND '23' "
        f"AND substr({column},15,2) BETWEEN '00' AND '59' AND substr({column},18,2) BETWEEN '00' AND '59' "
        f"AND strftime('%Y-%m-%dT%H:%M:%S',substr({column},1,19)) IS NOT NULL "
        f"AND strftime('%Y-%m-%dT%H:%M:%S',substr({column},1,19))=substr({column},1,19) "
        f"AND date(substr({column},1,10))=substr({column},1,10)",
        name="ck_material_return_utc_" + column)


def authorization(alias):
    """Every stage needs this exact operation/document/audit/connection grant."""
    return f"""COALESCE((erp_material_return_authorized()=1
        AND erp_material_return_basis() IS {alias}.authorization_basis
        AND erp_material_return_document() IS {alias}.material_return_id
        AND erp_material_return_digest() IS {alias}.confirmation_digest
        AND erp_material_return_snapshot() IS {alias}.confirmation_snapshot
        AND erp_material_return_time() IS {alias}.confirmation_received_at),0)=1"""


def source_identity(line="line", header="document"):
    """A real committed outward event is the sole source of all return identity."""
    return f"""EXISTS(
        SELECT 1 FROM material_issue_line origin
        JOIN material_issue issue ON issue.id=origin.material_issue_id
        JOIN material_issue_posting posted ON posted.material_issue_id=issue.id
        JOIN material_issue_entry outbound ON outbound.posting_id=posted.id
          AND outbound.issue_line_id=origin.id AND outbound.leg_no=1
        JOIN work_order work ON work.id=issue.work_order_id
        JOIN item material ON material.id=origin.material_id
        JOIN stock_position position ON position.id=origin.position_id
        WHERE origin.id={line}.origin_line_id AND origin.row_version={line}.origin_line_version
          AND posted.posting_state='POSTED' AND issue.work_order_id={header}.work_order_id
          AND work.row_version={header}.work_order_version
          AND posted.work_order_version=issue.work_order_version
          AND issue.row_version=posted.source_version+1
          AND material.item_kind='RAW' AND material.base_unit_id=origin.unit_id
          AND position.item_id=origin.material_id AND position.warehouse_id=origin.warehouse_id
          AND outbound.position_id=origin.position_id AND outbound.created_by=posted.created_by
          AND erp_decimal_cmp(outbound.qty_base_delta,'0')<0
          AND outbound.qty_base_delta=erp_decimal_neg(origin.quantity)
          AND (SELECT count(*) FROM material_issue_entry WHERE posting_id=posted.id)=
              (SELECT count(*) FROM material_issue_line WHERE material_issue_id=issue.id)
          AND NOT EXISTS(SELECT 1 FROM material_issue_line other
            WHERE other.material_issue_id=issue.id AND NOT EXISTS(
              SELECT 1 FROM material_issue_entry effect
              WHERE effect.posting_id=posted.id AND effect.issue_line_id=other.id
                AND effect.leg_no=1 AND effect.position_id=other.position_id
                AND effect.qty_base_delta=erp_decimal_neg(other.quantity)
                AND erp_decimal_cmp(effect.qty_base_delta,'0')<0)))"""


def source_guard(document):
    return f"""
      SELECT RAISE(ABORT,'material_return stale work order') WHERE NOT EXISTS(
        SELECT 1 FROM material_return h JOIN work_order w ON w.id=h.work_order_id
        WHERE h.id={document} AND w.row_version=h.work_order_version);
      SELECT RAISE(ABORT,'material_return source must be a matching genuine posted issue line')
      WHERE EXISTS(SELECT 1 FROM material_return_line line
        JOIN material_return document ON document.id=line.material_return_id
        WHERE document.id={document} AND NOT ({source_identity()}));
    """


def posted_returned(origin="origin.id"):
    # Entry and line must agree; DRAFT and PREPARING returns never enter this sum.
    return f"""COALESCE((SELECT erp_decimal_sum(previous.quantity)
      FROM material_return_line previous
      JOIN material_return_posting posted ON posted.material_return_id=previous.material_return_id
      JOIN material_return_entry effect ON effect.posting_id=posted.id AND effect.return_line_id=previous.id
      JOIN material_issue_line source ON source.id=previous.origin_line_id
      WHERE posted.posting_state='POSTED' AND previous.origin_line_id={origin}
        AND effect.leg_no=1 AND effect.position_id=source.position_id
        AND effect.qty_base_delta=previous.quantity AND erp_decimal_cmp(effect.qty_base_delta,'0')>0),'0')"""


def quantity_guards(document):
    return f"""
      SELECT RAISE(ABORT,'material_return actual inbound quantities must be positive') WHERE EXISTS(
        SELECT 1 FROM material_return_line WHERE material_return_id={document}
          AND erp_decimal_cmp(quantity,'0')<=0);
      SELECT RAISE(ABORT,'material_return source quantity exceeded') WHERE EXISTS(
        SELECT 1 FROM material_return_line line
        JOIN material_issue_line origin ON origin.id=line.origin_line_id
        JOIN material_issue_posting issued ON issued.material_issue_id=origin.material_issue_id
          AND issued.posting_state='POSTED'
        JOIN material_issue_entry outbound ON outbound.posting_id=issued.id
          AND outbound.issue_line_id=origin.id AND outbound.leg_no=1
        WHERE line.material_return_id={document} GROUP BY origin.id
        HAVING erp_decimal_cmp(erp_decimal_sum(line.quantity),
          erp_decimal_sub(erp_decimal_neg(outbound.qty_base_delta),{posted_returned()}))>0);
      SELECT erp_decimal_cmp(COALESCE((SELECT erp_decimal_sum(ledger.qty_base_delta)
        FROM physical_stock_ledger ledger WHERE ledger.position_id=origin.position_id),'0'),'0')
        FROM material_return_line line JOIN material_issue_line origin ON origin.id=line.origin_line_id
        WHERE line.material_return_id={document} GROUP BY origin.position_id;
    """


def extend_ledger():
    connection = op.get_bind()
    original = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='physical_stock_ledger'").scalar_one()
    for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE"]:
        if original.count("'" + domain + "'") != 1:
            raise RuntimeError("Unexpected original physical-stock ledger domain: " + domain)
    if "MATERIAL_RETURN" in original:
        raise RuntimeError("Manufacturing returns already occur in the physical-stock ledger")
    # No table reconstruction and no old trigger edit. Their existing references
    # resolve to this same view after the atomic DDL transaction commits.
    op.execute("DROP VIEW physical_stock_ledger")
    op.execute(original.rstrip().rstrip(";") + """
      UNION ALL
      SELECT e.id,e.posting_id,e.return_line_id,'MATERIAL_RETURN',e.position_id,e.qty_base_delta,h.work_order_id
      FROM material_return_entry e JOIN material_return_posting p ON p.id=e.posting_id
      JOIN material_return h ON h.id=p.material_return_id WHERE p.posting_state='POSTED'
    """)


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Material-return migration requires foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing foreign-key violations prevent material-return migration")
    fk = foundation.fk
    op.create_table("material_return", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("work_order_id", sa.Text(), fk("work_order.id"), nullable=False),
        sa.Column("work_order_version", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Text(), nullable=False), sa.Column("note", sa.Text()),
        sa.CheckConstraint("trim(document_no)<>'' AND typeof(row_version)='integer' AND row_version>0 "
                           "AND typeof(work_order_version)='integer' AND work_order_version>0"),
        foundation.date_check("business_date"), utc("created_at"), utc("updated_at"))
    op.create_table("material_return_line", *foundation.audit(),
        sa.Column("material_return_id", sa.Text(), fk("material_return.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("origin_line_id", sa.Text(), fk("material_issue_line.id"), nullable=False),
        sa.Column("origin_line_version", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Text(), nullable=False), sa.Column("note", sa.Text()),
        sa.UniqueConstraint("material_return_id", "line_no"), decimal("quantity"),
        sa.CheckConstraint("typeof(line_no)='integer' AND typeof(row_version)='integer' AND row_version>0 "
                           "AND typeof(origin_line_version)='integer' AND origin_line_version>0"),
        utc("created_at"), utc("updated_at"))
    op.create_table("material_return_posting", *receiving.created(),
        sa.Column("material_return_id", sa.Text(), fk("material_return.id"), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("work_order_version", sa.Integer(), nullable=False),
        sa.Column("posting_state", sa.Text(), nullable=False),
        sa.Column("authorization_basis", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("confirmation_snapshot", sa.Text(), nullable=False),
        sa.Column("confirmation_received_at", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.Text(), nullable=False),
        sa.Column("posted_by", sa.Text(), fk("actor.id"), nullable=False),
        sa.CheckConstraint("posting_state IN ('PREPARING','POSTED') "
                           "AND authorization_basis IN ('CONTROLLED_QA_ONLY','LOCAL_DEMO_MANUAL') "
                           "AND typeof(source_version)='integer' AND source_version>0 "
                           "AND typeof(work_order_version)='integer' AND work_order_version>0 "
                           "AND trim(request_fingerprint)<>'' AND trim(confirmation_digest)<>'' "
                           "AND trim(confirmation_snapshot)<>''"),
        utc("created_at"), utc("posted_at"), utc("confirmation_received_at"))
    op.create_table("material_return_entry", *receiving.created(),
        sa.Column("posting_id", sa.Text(), fk("material_return_posting.id"), nullable=False),
        sa.Column("return_line_id", sa.Text(), fk("material_return_line.id"), nullable=False),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False),
        sa.Column("qty_base_delta", sa.Text(), nullable=False), decimal("qty_base_delta"),
        sa.UniqueConstraint("return_line_id", "leg_no"),
        sa.CheckConstraint("leg_no=1 AND typeof(leg_no)='integer' AND substr(qty_base_delta,1,1)<>'-' "
                           "AND qty_base_delta<>'0'"), utc("created_at"))
    for table, columns in {
        "material_return": ["work_order_id", "created_by"],
        "material_return_line": ["material_return_id", "origin_line_id", "created_by"],
        "material_return_posting": ["material_return_id", "created_by", "posted_by"],
        "material_return_entry": ["posting_id", "return_line_id", "position_id", "created_by"],
    }.items():
        for column in columns:
            op.create_index("ix_" + table + "_" + column, table, [column])
    extend_ledger()

    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER material_return_refs_{action.lower()} BEFORE {action} ON material_return
          WHEN NOT EXISTS(SELECT 1 FROM work_order w WHERE w.id=NEW.work_order_id
            AND w.row_version=NEW.work_order_version)
          BEGIN SELECT RAISE(ABORT,'material_return stale work order'); END""")
        op.execute(f"""CREATE TRIGGER material_return_line_source_{action.lower()} BEFORE {action} ON material_return_line BEGIN
            SELECT RAISE(ABORT,'material_return source must be a matching genuine posted issue line')
            WHERE NOT EXISTS(SELECT 1 FROM material_return document WHERE document.id=NEW.material_return_id
              AND {source_identity('NEW')});
          END""")
    op.execute("""CREATE TRIGGER material_return_work_order_change BEFORE UPDATE OF work_order_id ON material_return
      WHEN NEW.work_order_id<>OLD.work_order_id AND EXISTS(
        SELECT 1 FROM material_return_line WHERE material_return_id=OLD.id)
      BEGIN SELECT RAISE(ABORT,'material_return cannot reassign work order with lines'); END""")
    op.execute("""CREATE TRIGGER material_return_line_no_reparent BEFORE UPDATE OF material_return_id ON material_return_line
      WHEN NEW.material_return_id<>OLD.material_return_id
      BEGIN SELECT RAISE(ABORT,'material_return cannot reparent line'); END""")
    for table, extra in {
        "material_return": "OR document_no=NEW.document_no",
        "material_return_line": "OR (material_return_id=NEW.material_return_id AND line_no=NEW.line_no)",
        "material_return_posting": "OR material_return_id=NEW.material_return_id OR idempotency_key=NEW.idempotency_key",
        "material_return_entry": "OR (return_line_id=NEW.return_line_id AND leg_no=NEW.leg_no)",
    }.items():
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id OR rowid=NEW.rowid {extra})
          BEGIN SELECT RAISE(ABORT,'material_return existing identity cannot be replaced'); END""")
    for table, condition in [
        ("material_return", "document_no=NEW.document_no"),
        ("material_return_line", "material_return_id=NEW.material_return_id AND line_no=NEW.line_no"),
    ]:
        op.execute(f"""CREATE TRIGGER {table}_update_no_replace BEFORE UPDATE ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} WHERE ({condition} OR rowid=NEW.rowid) AND id<>OLD.id)
          BEGIN SELECT RAISE(ABORT,'material_return existing identity cannot be replaced'); END""")
        op.execute(f"""CREATE TRIGGER {table}_audit BEFORE UPDATE ON {table}
          WHEN NEW.id<>OLD.id OR NEW.rowid<>OLD.rowid OR NEW.created_by<>OLD.created_by OR NEW.created_at<>OLD.created_at
          BEGIN SELECT RAISE(ABORT,'material_return creation audit immutable'); END""")
    for table, condition in [
        ("material_return", "material_return_id=OLD.id"),
        ("material_return_line", "material_return_id=OLD.material_return_id"),
    ]:
        for action in ["UPDATE", "DELETE"]:
            op.execute(f"""CREATE TRIGGER {table}_posted_{action.lower()} BEFORE {action} ON {table}
              WHEN EXISTS(SELECT 1 FROM material_return_posting WHERE {condition})
              BEGIN SELECT RAISE(ABORT,'material_return posted history immutable'); END""")
    op.execute("""CREATE TRIGGER material_return_line_posted_insert BEFORE INSERT ON material_return_line
      WHEN EXISTS(SELECT 1 FROM material_return_posting WHERE material_return_id=NEW.material_return_id)
      BEGIN SELECT RAISE(ABORT,'material_return posted history immutable'); END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER material_return_entry_immutable_{action.lower()} BEFORE {action} ON material_return_entry
          BEGIN SELECT RAISE(ABORT,'material_return entry immutable'); END""")
    op.execute("""CREATE TRIGGER material_return_posting_immutable_delete BEFORE DELETE ON material_return_posting
      BEGIN SELECT RAISE(ABORT,'material_return posting immutable'); END""")
    for other in ["stock_posting", "supplier_return_posting", "material_issue_posting"]:
        op.execute(f"""CREATE TRIGGER material_return_key_{other} BEFORE INSERT ON material_return_posting
          WHEN EXISTS(SELECT 1 FROM {other} WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'material_return posting key belongs to another domain'); END""")
        op.execute(f"""CREATE TRIGGER {other}_material_return_key BEFORE INSERT ON {other}
          WHEN EXISTS(SELECT 1 FROM material_return_posting WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'posting key belongs to material_return'); END""")
    op.execute(f"""CREATE TRIGGER material_return_posting_prepare BEFORE INSERT ON material_return_posting BEGIN
      SELECT RAISE(ABORT,'material_return policy pending: authorization context denied') WHERE NOT ({authorization('NEW')});
      SELECT RAISE(ABORT,'material_return must prepare') WHERE NEW.posting_state<>'PREPARING';
      SELECT RAISE(ABORT,'material_return invalid document version') WHERE NOT EXISTS(
        SELECT 1 FROM material_return h WHERE h.id=NEW.material_return_id
          AND h.row_version=NEW.source_version+1 AND h.work_order_version=NEW.work_order_version);
      SELECT RAISE(ABORT,'material_return empty document') WHERE NOT EXISTS(
        SELECT 1 FROM material_return_line WHERE material_return_id=NEW.material_return_id);
      {source_guard('NEW.material_return_id')}{quantity_guards('NEW.material_return_id')} END""")
    op.execute(f"""CREATE TRIGGER material_return_entry_inbound BEFORE INSERT ON material_return_entry BEGIN
      SELECT RAISE(ABORT,'material_return invalid inbound movement or policy pending') WHERE NOT EXISTS(
        SELECT 1 FROM material_return_posting p
        JOIN material_return document ON document.id=p.material_return_id
        JOIN material_return_line line ON line.material_return_id=p.material_return_id
        JOIN material_issue_line origin ON origin.id=line.origin_line_id
        WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING' AND ({authorization('p')})
          AND line.id=NEW.return_line_id AND NEW.position_id=origin.position_id
          AND NEW.leg_no=1 AND NEW.qty_base_delta=line.quantity AND erp_decimal_cmp(line.quantity,'0')>0
          AND NEW.created_by=p.created_by AND {source_identity()});
      SELECT RAISE(ABORT,'material_return source quantity exceeded by preparing movements') WHERE EXISTS(
        SELECT 1 FROM material_return_line line JOIN material_issue_line origin ON origin.id=line.origin_line_id
        JOIN material_issue_posting issued ON issued.material_issue_id=origin.material_issue_id AND issued.posting_state='POSTED'
        JOIN material_issue_entry outbound ON outbound.posting_id=issued.id AND outbound.issue_line_id=origin.id AND outbound.leg_no=1
        WHERE line.id=NEW.return_line_id AND erp_decimal_cmp(
          erp_decimal_sub(erp_decimal_neg(outbound.qty_base_delta),{posted_returned()}),
          (SELECT erp_decimal_sum(value) FROM (
            SELECT NEW.qty_base_delta AS value UNION ALL
            SELECT e.qty_base_delta FROM material_return_entry e
            JOIN material_return_posting preparing ON preparing.id=e.posting_id
            JOIN material_return_line pending ON pending.id=e.return_line_id
            WHERE preparing.posting_state='PREPARING' AND pending.origin_line_id=origin.id)))<0);
      END""")
    protected = ["id", "rowid", "material_return_id", "idempotency_key", "request_fingerprint", "source_version",
                 "work_order_version", "authorization_basis", "confirmation_digest", "confirmation_snapshot",
                 "confirmation_received_at", "posted_at", "posted_by", "created_at", "created_by"]
    unchanged = " AND ".join("NEW." + field + " IS OLD." + field for field in protected)
    op.execute(f"""CREATE TRIGGER material_return_posting_finalize BEFORE UPDATE ON material_return_posting BEGIN
      SELECT RAISE(ABORT,'material_return policy pending: authorization context denied') WHERE NOT ({authorization('OLD')});
      SELECT RAISE(ABORT,'material_return posting immutable') WHERE NOT
        (OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {unchanged});
      SELECT RAISE(ABORT,'material_return incomplete movements') WHERE EXISTS(
        SELECT 1 FROM material_return_line line WHERE line.material_return_id=OLD.material_return_id AND NOT EXISTS(
          SELECT 1 FROM material_return_entry e WHERE e.posting_id=OLD.id AND e.return_line_id=line.id AND e.leg_no=1))
        OR (SELECT count(*) FROM material_return_entry WHERE posting_id=OLD.id)<>
           (SELECT count(*) FROM material_return_line WHERE material_return_id=OLD.material_return_id);
      {source_guard('OLD.material_return_id')}{quantity_guards('OLD.material_return_id')} END""")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Material-return migration produced invalid foreign-key references")


def downgrade():
    raise RuntimeError("Material-return downgrade is disabled; preserve verified backup recovery.")
