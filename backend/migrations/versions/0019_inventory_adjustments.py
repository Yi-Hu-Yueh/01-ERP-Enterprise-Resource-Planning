"""Separate existing-position adjustments, using the authoritative shared ledger."""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "0019_inventory_adjustments"
down_revision = "0018_stocktakes"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")
finished = import_module("backend.migrations.versions.0014_finished_goods_receipts")
stocktaking = import_module("backend.migrations.versions.0018_stocktakes")

OLD_DOMAINS = stocktaking.OLD_DOMAINS + ["STOCKTAKE"]
OLD_POSTINGS = stocktaking.OLD_POSTINGS + ["stocktake_posting"]


def exact(column):
    constraint = foundation.decimal_check(column)
    constraint.name = "ck_adjustment_exact_" + column
    return constraint


def utc(column, nullable=False):
    constraint = finished.utc(column)
    if nullable:
        constraint = sa.CheckConstraint(f"{column} IS NULL OR ({constraint.sqltext})")
    constraint.name = "ck_adjustment_utc_" + column
    return constraint


def source(alias="a"):
    """Actual registered position; no free-text lot or invented operational source."""
    return f"""EXISTS(SELECT 1 FROM stock_position position JOIN item i ON i.id=position.item_id
      JOIN unit u ON u.id=i.base_unit_id JOIN warehouse w ON w.id=position.warehouse_id
      WHERE position.id={alias}.position_id AND position.item_id={alias}.item_id
        AND i.item_kind={alias}.item_kind AND i.item_kind IN ('RAW','FINISHED')
        AND i.base_unit_id={alias}.unit_id AND position.warehouse_id={alias}.warehouse_id
        AND position.lot_id IS {alias}.lot_id
        AND ((i.lot_tracking=0 AND position.lot_id IS NULL) OR (i.lot_tracking=1 AND EXISTS(
          SELECT 1 FROM inventory_lot lot WHERE lot.id=position.lot_id AND lot.item_id=i.id))))"""


def signed(alias="a"):
    return f"CASE WHEN {alias}.direction='INCREASE' THEN {alias}.quantity ELSE erp_decimal_neg({alias}.quantity) END"


def reason_text(alias="a"):
    # Match Python str.strip whitespace exactly, including tabs/fullwidth spaces,
    # while keeping raw SQLite validation independent of application UDFs.
    whitespace = "9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288"
    return f"trim({alias}.reason,char({whitespace}))"


def authorization(alias):
    return f"""COALESCE((erp_adjustment_authorized()=1
      AND {alias}.authorization_basis='LOCAL_DEMO_MANUAL'
      AND erp_adjustment_document() IS {alias}.adjustment_id
      AND erp_adjustment_digest() IS {alias}.confirmation_digest
      AND erp_adjustment_snapshot() IS {alias}.confirmation_snapshot
      AND erp_adjustment_time() IS {alias}.confirmation_received_at),0)=1"""


def source_guard(document):
    return f"""
      SELECT RAISE(ABORT,'inventory_adjustment requires genuine existing position lineage') WHERE NOT EXISTS(
        SELECT 1 FROM inventory_adjustment a WHERE a.id={document} AND ({source()}));
      SELECT RAISE(ABORT,'warehouse frozen by active stocktake') WHERE EXISTS(
        SELECT 1 FROM inventory_adjustment a WHERE a.id={document} AND {stocktaking.frozen('a.warehouse_id')});
      SELECT RAISE(ABORT,'inventory_adjustment requires positive magnitude and meaningful reason') WHERE EXISTS(
        SELECT 1 FROM inventory_adjustment a WHERE a.id={document}
          AND (erp_decimal_cmp(a.quantity,'0')<=0 OR {reason_text()}=''));
      SELECT RAISE(ABORT,'inventory_adjustment negative movement exceeds actual stock') WHERE EXISTS(
        SELECT 1 FROM inventory_adjustment a WHERE a.id={document} AND a.direction='DECREASE'
          AND erp_decimal_cmp(a.quantity,{stocktaking.book('a.position_id')})>0);
    """


def entry_guard(posting, document):
    return f"""
      SELECT RAISE(ABORT,'inventory_adjustment requires exactly one genuine signed movement') WHERE
        (SELECT count(*) FROM inventory_adjustment_entry WHERE posting_id={posting})<>1 OR NOT EXISTS(
          SELECT 1 FROM inventory_adjustment_entry e JOIN inventory_adjustment a ON a.id=e.adjustment_id
          JOIN inventory_adjustment_posting p ON p.id=e.posting_id
          WHERE e.posting_id={posting} AND e.adjustment_id={document} AND p.adjustment_id=a.id
            AND e.position_id=a.position_id AND e.leg_no=1 AND e.created_by=p.created_by
            AND erp_decimal_cmp(a.quantity,'0')>0 AND e.qty_base_delta IS ({signed()}));
    """


def extend_ledger():
    original = op.get_bind().exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='physical_stock_ledger'").scalar_one()
    for domain in OLD_DOMAINS:
        if original.count("'" + domain + "'") != 1:
            raise RuntimeError("Unexpected original physical-ledger domain " + domain)
    if "'INVENTORY_ADJUSTMENT'" in original:
        raise RuntimeError("Adjustment already occurs in the authoritative ledger")
    op.execute("DROP VIEW physical_stock_ledger")
    op.execute(original.rstrip().rstrip(";") + """
      UNION ALL SELECT e.id,e.posting_id,e.adjustment_id,'INVENTORY_ADJUSTMENT',e.position_id,e.qty_base_delta,NULL
      FROM inventory_adjustment_entry e JOIN inventory_adjustment_posting p ON p.id=e.posting_id
      JOIN inventory_adjustment a ON a.id=e.adjustment_id AND a.id=p.adjustment_id
      WHERE p.posting_state='POSTED' AND a.state='POSTED'
    """)


def document_guards():
    audit = ["posted_at", "posted_by", "cancelled_at", "cancelled_by"]
    empty = " AND ".join("NEW." + field + " IS NULL" for field in audit)
    op.execute(f"""CREATE TRIGGER inventory_adjustment_draft_insert BEFORE INSERT ON inventory_adjustment
      WHEN NOT (NEW.state='DRAFT' AND NEW.row_version=1 AND {empty})
      BEGIN SELECT RAISE(ABORT,'inventory_adjustment must begin as an unposted draft'); END""")
    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER inventory_adjustment_source_{action.lower()} BEFORE {action} ON inventory_adjustment
          WHEN NOT ({source('NEW')})
          BEGIN SELECT RAISE(ABORT,'inventory_adjustment requires genuine existing position lineage'); END""")
    immutable = ["id", "rowid", "document_no", "business_date", "position_id", "item_id", "item_kind", "unit_id",
                 "warehouse_id", "lot_id", "direction", "quantity", "reason", "note", "created_at", "created_by"]
    unchanged = " AND ".join("NEW." + field + " IS OLD." + field for field in immutable)
    cancel = "COALESCE((erp_adjustment_edit_action()='CANCEL' AND erp_adjustment_edit_document() IS OLD.id),0)=1"
    op.execute(f"""CREATE TRIGGER inventory_adjustment_state_machine BEFORE UPDATE ON inventory_adjustment BEGIN
      SELECT RAISE(ABORT,'inventory_adjustment creation identity immutable') WHERE
        NEW.id IS NOT OLD.id OR NEW.rowid<>OLD.rowid OR NEW.created_at IS NOT OLD.created_at
        OR NEW.created_by IS NOT OLD.created_by;
      SELECT RAISE(ABORT,'inventory_adjustment state transition or authorization denied') WHERE NOT COALESCE((
        OLD.state='DRAFT' AND NEW.row_version=OLD.row_version+1 AND (
          (NEW.state='DRAFT' AND {empty}
            AND NOT EXISTS(SELECT 1 FROM inventory_adjustment_posting WHERE adjustment_id=OLD.id))
          OR (NEW.state='CANCELLED' AND {unchanged} AND {cancel}
            AND NEW.posted_at IS NULL AND NEW.posted_by IS NULL
            AND NEW.cancelled_at IS NOT NULL AND NEW.cancelled_by IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM inventory_adjustment_posting WHERE adjustment_id=OLD.id))
          OR (NEW.state='POSTED' AND {unchanged} AND NEW.cancelled_at IS NULL AND NEW.cancelled_by IS NULL
            AND EXISTS(SELECT 1 FROM inventory_adjustment_posting p WHERE p.adjustment_id=OLD.id
              AND p.posting_state='POSTED' AND p.source_version=OLD.row_version AND {authorization('p')}
              AND NEW.posted_at IS p.posted_at AND NEW.posted_by IS p.posted_by))
        )),0);
      SELECT RAISE(ABORT,'inventory_adjustment cannot replace another document') WHERE EXISTS(
        SELECT 1 FROM inventory_adjustment other WHERE other.id<>OLD.id AND other.document_no=NEW.document_no);
      END""")
    op.execute(f"""CREATE TRIGGER inventory_adjustment_complete BEFORE UPDATE ON inventory_adjustment
      WHEN NEW.state='POSTED' BEGIN
      {source_guard('OLD.id')}
      {entry_guard('(SELECT id FROM inventory_adjustment_posting WHERE adjustment_id=OLD.id)', 'OLD.id')}
      END""")
    op.execute("""CREATE TRIGGER inventory_adjustment_delete_draft_only BEFORE DELETE ON inventory_adjustment
      WHEN OLD.state<>'DRAFT' OR EXISTS(SELECT 1 FROM inventory_adjustment_posting WHERE adjustment_id=OLD.id)
      BEGIN SELECT RAISE(ABORT,'inventory_adjustment final history cannot be deleted'); END""")


def posting_guards():
    for other in OLD_POSTINGS:
        op.execute(f"""CREATE TRIGGER inventory_adjustment_key_{other} BEFORE INSERT ON inventory_adjustment_posting
          WHEN EXISTS(SELECT 1 FROM {other} WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'adjustment key belongs to another posting domain'); END""")
        op.execute(f"""CREATE TRIGGER {other}_inventory_adjustment_key BEFORE INSERT ON {other}
          WHEN EXISTS(SELECT 1 FROM inventory_adjustment_posting WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'posting key belongs to inventory adjustment'); END""")
    op.execute(f"""CREATE TRIGGER inventory_adjustment_posting_prepare BEFORE INSERT ON inventory_adjustment_posting BEGIN
      SELECT RAISE(ABORT,'inventory_adjustment signed authorization denied') WHERE NOT ({authorization('NEW')});
      SELECT RAISE(ABORT,'inventory_adjustment posting must prepare') WHERE NEW.posting_state<>'PREPARING';
      SELECT RAISE(ABORT,'inventory_adjustment posting audit mismatch') WHERE
        NEW.posted_at IS NOT NEW.confirmation_received_at OR NEW.posted_by<>NEW.created_by;
      SELECT RAISE(ABORT,'inventory_adjustment requires current draft version') WHERE NOT EXISTS(
        SELECT 1 FROM inventory_adjustment a WHERE a.id=NEW.adjustment_id
          AND a.state='DRAFT' AND a.row_version=NEW.source_version);
      {source_guard('NEW.adjustment_id')}
      END""")
    op.execute(f"""CREATE TRIGGER inventory_adjustment_entry_signed BEFORE INSERT ON inventory_adjustment_entry BEGIN
      SELECT RAISE(ABORT,'inventory_adjustment entry is not the authorized signed magnitude') WHERE NOT EXISTS(
        SELECT 1 FROM inventory_adjustment_posting p JOIN inventory_adjustment a ON a.id=p.adjustment_id
        WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING' AND {authorization('p')}
          AND a.id=NEW.adjustment_id AND a.state='DRAFT' AND a.row_version=p.source_version
          AND NEW.position_id=a.position_id AND NEW.leg_no=1 AND NEW.created_by=p.created_by
          AND erp_decimal_cmp(a.quantity,'0')>0 AND NEW.qty_base_delta IS ({signed()}));
      {source_guard('NEW.adjustment_id')}
      END""")
    protected = ["id", "rowid", "adjustment_id", "idempotency_key", "request_fingerprint", "source_version",
                 "authorization_basis", "confirmation_digest", "confirmation_snapshot", "confirmation_received_at",
                 "posted_at", "posted_by", "created_at", "created_by"]
    unchanged = " AND ".join("NEW." + field + " IS OLD." + field for field in protected)
    op.execute(f"""CREATE TRIGGER inventory_adjustment_posting_finish BEFORE UPDATE ON inventory_adjustment_posting BEGIN
      SELECT RAISE(ABORT,'inventory_adjustment signed authorization denied') WHERE NOT ({authorization('OLD')});
      SELECT RAISE(ABORT,'inventory_adjustment posting immutable') WHERE NOT (
        OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {unchanged});
      SELECT RAISE(ABORT,'inventory_adjustment source version changed') WHERE NOT EXISTS(
        SELECT 1 FROM inventory_adjustment a WHERE a.id=OLD.adjustment_id AND a.state='DRAFT'
          AND a.row_version=OLD.source_version);
      {source_guard('OLD.adjustment_id')}{entry_guard('OLD.id', 'OLD.adjustment_id')}
      END""")
    op.execute("""CREATE TRIGGER inventory_adjustment_posting_no_delete BEFORE DELETE ON inventory_adjustment_posting
      BEGIN SELECT RAISE(ABORT,'inventory_adjustment posting cannot be deleted'); END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER inventory_adjustment_entry_immutable_{action.lower()} BEFORE {action} ON inventory_adjustment_entry
          BEGIN SELECT RAISE(ABORT,'inventory_adjustment movement immutable'); END""")


def freeze_barriers():
    entry = "inventory_adjustment_entry"
    table = "inventory_adjustment_posting"
    frozen = stocktaking.frozen
    affected = lambda identity: f"EXISTS(SELECT 1 FROM inventory_adjustment a WHERE a.id={identity} AND {frozen('a.warehouse_id')})"
    op.execute(f"""CREATE TRIGGER warehouse_freeze_inventory_adjustment_entry_insert BEFORE INSERT ON {entry}
      WHEN EXISTS(SELECT 1 FROM stock_position position WHERE position.id=NEW.position_id AND {frozen('position.warehouse_id')})
      BEGIN SELECT RAISE(ABORT,'warehouse frozen by active stocktake'); END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER warehouse_freeze_inventory_adjustment_entry_{action.lower()} BEFORE {action} ON {entry}
          WHEN EXISTS(SELECT 1 FROM stock_position position WHERE position.id=OLD.position_id AND {frozen('position.warehouse_id')})
          BEGIN SELECT RAISE(ABORT,'warehouse frozen by active stocktake'); END""")
    for action, predicate in [("INSERT", affected("NEW.adjustment_id")),
                              ("UPDATE", affected("OLD.adjustment_id") + " OR " + affected("NEW.adjustment_id")),
                              ("DELETE", affected("OLD.adjustment_id"))]:
        op.execute(f"""CREATE TRIGGER warehouse_freeze_inventory_adjustment_posting_{action.lower()} BEFORE {action} ON {table}
          WHEN {predicate} BEGIN SELECT RAISE(ABORT,'warehouse frozen by active stocktake'); END""")


def identity_holds():
    """New typed references retain zero-position lineage without touching old guards."""
    for table, field in [("stock_position", "position_id"), ("inventory_lot", "lot_id"),
                         ("item", "item_id"), ("unit", "unit_id"), ("warehouse", "warehouse_id")]:
        op.execute(f"""CREATE TRIGGER inventory_adjustment_{table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} prior WHERE {stocktaking.collisions(table)}
            AND EXISTS(SELECT 1 FROM inventory_adjustment WHERE {field}=prior.id))
          BEGIN SELECT RAISE(ABORT,'inventory_adjustment referenced source cannot be replaced'); END""")
        if table in {"stock_position", "inventory_lot"}:
            # Their existing unconditional UPDATE/DELETE guards remain intact.
            continue
        changed = "NEW.id IS NOT OLD.id OR NEW.rowid<>OLD.rowid"
        if table == "item":
            changed += " OR NEW.item_kind IS NOT OLD.item_kind OR NEW.base_unit_id IS NOT OLD.base_unit_id OR NEW.lot_tracking IS NOT OLD.lot_tracking"
        op.execute(f"""CREATE TRIGGER inventory_adjustment_{table}_basis BEFORE UPDATE ON {table}
          WHEN ({changed}) AND EXISTS(SELECT 1 FROM inventory_adjustment WHERE {field}=OLD.id)
          BEGIN SELECT RAISE(ABORT,'inventory_adjustment source identity and unit basis immutable'); END""")
        op.execute(f"""CREATE TRIGGER inventory_adjustment_{table}_delete BEFORE DELETE ON {table}
          WHEN EXISTS(SELECT 1 FROM inventory_adjustment WHERE {field}=OLD.id)
          BEGIN SELECT RAISE(ABORT,'inventory_adjustment referenced source cannot be deleted'); END""")


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Adjustment migration requires foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing foreign-key violations prevent adjustment migration")
    fk = foundation.fk
    op.create_table("inventory_adjustment", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("business_date", sa.Text(), nullable=False),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("item_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False), sa.Column("unit_id", sa.Text(), fk("unit.id"), nullable=False),
        sa.Column("warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("lot_id", sa.Text(), fk("inventory_lot.id")),
        sa.Column("direction", sa.Text(), nullable=False), sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("note", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.Text()), sa.Column("posted_by", sa.Text(), fk("actor.id")),
        sa.Column("cancelled_at", sa.Text()), sa.Column("cancelled_by", sa.Text(), fk("actor.id")),
        exact("quantity"), foundation.date_check("business_date"),
        utc("created_at"), utc("updated_at"), utc("posted_at", nullable=True), utc("cancelled_at", nullable=True),
        sa.CheckConstraint("trim(document_no)<>'' AND substr(business_date,1,4)<>'0000' "
            "AND typeof(row_version)='integer' AND row_version>0 AND item_kind IN ('RAW','FINISHED') "
            "AND direction IN ('INCREASE','DECREASE') AND state IN ('DRAFT','POSTED','CANCELLED')", name="ck_adjustment_state"),
        sa.CheckConstraint("(state='POSTED' AND posted_at IS NOT NULL AND posted_by IS NOT NULL) OR "
            "(state<>'POSTED' AND posted_at IS NULL AND posted_by IS NULL)", name="ck_adjustment_posted_audit"),
        sa.CheckConstraint("(state='CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL) OR "
            "(state<>'CANCELLED' AND cancelled_at IS NULL AND cancelled_by IS NULL)", name="ck_adjustment_cancel_audit"))
    op.create_table("inventory_adjustment_posting", *receiving.created(),
        sa.Column("adjustment_id", sa.Text(), fk("inventory_adjustment.id"), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.Text(), nullable=False), sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("posting_state", sa.Text(), nullable=False), sa.Column("authorization_basis", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False), sa.Column("confirmation_snapshot", sa.Text(), nullable=False),
        sa.Column("confirmation_received_at", sa.Text(), nullable=False), sa.Column("posted_at", sa.Text(), nullable=False),
        sa.Column("posted_by", sa.Text(), fk("actor.id"), nullable=False),
        utc("created_at"), utc("confirmation_received_at"), utc("posted_at"),
        sa.CheckConstraint("typeof(source_version)='integer' AND source_version>0 "
            "AND posting_state IN ('PREPARING','POSTED') AND authorization_basis='LOCAL_DEMO_MANUAL' "
            "AND trim(request_fingerprint)<>'' AND length(confirmation_digest)=64 "
            "AND confirmation_digest NOT GLOB '*[^0-9a-f]*' AND json_valid(confirmation_snapshot)", name="ck_adjustment_signed_posting"))
    op.create_table("inventory_adjustment_entry", *receiving.created(),
        sa.Column("posting_id", sa.Text(), fk("inventory_adjustment_posting.id"), nullable=False, unique=True),
        sa.Column("adjustment_id", sa.Text(), fk("inventory_adjustment.id"), nullable=False, unique=True),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False), sa.Column("qty_base_delta", sa.Text(), nullable=False),
        exact("qty_base_delta"), utc("created_at"),
        sa.CheckConstraint("typeof(leg_no)='integer' AND leg_no=1 AND erp_decimal_cmp(qty_base_delta,'0')<>0", name="ck_adjustment_entry_nonzero"))
    for table, fields in {
        "inventory_adjustment": ["position_id", "item_id", "unit_id", "warehouse_id", "lot_id", "created_by", "posted_by", "cancelled_by"],
        "inventory_adjustment_posting": ["adjustment_id", "created_by", "posted_by"],
        "inventory_adjustment_entry": ["posting_id", "adjustment_id", "position_id", "created_by"],
    }.items():
        for field in fields:
            op.create_index("ix_" + table + "_" + field, table, [field])
    extend_ledger()
    for table in ["inventory_adjustment", "inventory_adjustment_posting", "inventory_adjustment_entry"]:
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} prior WHERE {stocktaking.collisions(table)})
          BEGIN SELECT RAISE(ABORT,'inventory_adjustment existing identity cannot be replaced'); END""")
    document_guards()
    posting_guards()
    freeze_barriers()
    identity_holds()
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Adjustment migration produced invalid foreign-key references")
    if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
        raise RuntimeError("Adjustment migration failed integrity checking")


def downgrade():
    raise RuntimeError("Adjustment downgrade disabled; preserve immutable history and verified backups.")
