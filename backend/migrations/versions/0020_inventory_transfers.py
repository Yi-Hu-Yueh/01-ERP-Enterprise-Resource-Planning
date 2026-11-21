"""Direct atomic interwarehouse transfers, preserving global original-lot identity."""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "0020_inventory_transfers"
down_revision = "0019_inventory_adjustments"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")
finished = import_module("backend.migrations.versions.0014_finished_goods_receipts")
stocktaking = import_module("backend.migrations.versions.0018_stocktakes")

OLD_DOMAINS = stocktaking.OLD_DOMAINS + ["STOCKTAKE", "INVENTORY_ADJUSTMENT"]
OLD_POSTINGS = stocktaking.OLD_POSTINGS + ["stocktake_posting", "inventory_adjustment_posting"]


def exact(column):
    constraint = foundation.decimal_check(column)
    constraint.name = "ck_transfer_exact_" + column
    return constraint


def utc(column, nullable=False):
    constraint = finished.utc(column)
    if nullable:
        constraint = sa.CheckConstraint(f"{column} IS NULL OR ({constraint.sqltext})")
    constraint.name = "ck_transfer_utc_" + column
    return constraint


def source(alias="a"):
    """The existing global lot and both genuine, distinct warehouse identities."""
    return f"""EXISTS(SELECT 1 FROM stock_position position JOIN item i ON i.id=position.item_id
      JOIN unit u ON u.id=i.base_unit_id JOIN warehouse w ON w.id=position.warehouse_id
      JOIN warehouse destination ON destination.id={alias}.destination_warehouse_id
      WHERE position.id={alias}.source_position_id AND position.item_id={alias}.item_id
        AND i.item_kind={alias}.item_kind AND i.item_kind IN ('RAW','FINISHED')
        AND i.base_unit_id={alias}.unit_id AND position.warehouse_id={alias}.source_warehouse_id
        AND destination.id<>w.id
        AND position.lot_id IS {alias}.lot_id
        AND ((i.lot_tracking=0 AND position.lot_id IS NULL) OR (i.lot_tracking=1 AND EXISTS(
          SELECT 1 FROM inventory_lot lot WHERE lot.id=position.lot_id AND lot.item_id=i.id))))"""


def destination(alias="a"):
    return f"""EXISTS(SELECT 1 FROM stock_position position WHERE position.id={alias}.destination_position_id
      AND position.item_id={alias}.item_id AND position.warehouse_id={alias}.destination_warehouse_id
      AND position.lot_id IS {alias}.lot_id AND position.id<>{alias}.source_position_id)"""


def signed(leg, alias="a"):
    return f"CASE WHEN {leg}=1 THEN erp_decimal_neg({alias}.quantity) ELSE {alias}.quantity END"


def reason_text(alias="a"):
    # Match Python str.strip whitespace exactly, including tabs/fullwidth spaces,
    # while keeping raw SQLite validation independent of application UDFs.
    whitespace = "9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288"
    return f"trim({alias}.reason,char({whitespace}))"


def authorization(alias):
    return f"""COALESCE((erp_transfer_authorized()=1
      AND {alias}.authorization_basis='LOCAL_DEMO_MANUAL'
      AND erp_transfer_document() IS {alias}.transfer_id
      AND erp_transfer_digest() IS {alias}.confirmation_digest
      AND erp_transfer_snapshot() IS {alias}.confirmation_snapshot
      AND erp_transfer_time() IS {alias}.confirmation_received_at),0)=1"""


def source_guard(document):
    return f"""
      SELECT RAISE(ABORT,'inventory_transfer requires genuine existing position lineage') WHERE NOT EXISTS(
        SELECT 1 FROM inventory_transfer a WHERE a.id={document} AND ({source()}));
      SELECT RAISE(ABORT,'warehouse frozen by active stocktake') WHERE EXISTS(
        SELECT 1 FROM inventory_transfer a WHERE a.id={document}
          AND ({stocktaking.frozen('a.source_warehouse_id')} OR {stocktaking.frozen('a.destination_warehouse_id')}));
      SELECT RAISE(ABORT,'inventory_transfer requires active source and destination warehouses') WHERE EXISTS(
        SELECT 1 FROM inventory_transfer a JOIN warehouse source ON source.id=a.source_warehouse_id
        JOIN warehouse destination ON destination.id=a.destination_warehouse_id
        WHERE a.id={document} AND (source.is_active<>1 OR destination.is_active<>1));
      SELECT RAISE(ABORT,'inventory_transfer requires positive magnitude and meaningful reason') WHERE EXISTS(
        SELECT 1 FROM inventory_transfer a WHERE a.id={document}
          AND (erp_decimal_cmp(a.quantity,'0')<=0 OR {reason_text()}=''));
      SELECT RAISE(ABORT,'inventory_transfer negative movement exceeds actual stock') WHERE EXISTS(
        SELECT 1 FROM inventory_transfer a WHERE a.id={document}
          AND erp_decimal_cmp(a.quantity,{stocktaking.book('a.source_position_id')})>0);
    """


def entry_guard(posting, document):
    return f"""
      SELECT RAISE(ABORT,'inventory_transfer requires exactly two balanced genuine signed movements') WHERE
        (SELECT count(*) FROM inventory_transfer_entry WHERE posting_id={posting})<>2
        OR (SELECT count(*) FROM inventory_transfer_entry WHERE transfer_id={document})<>2
        OR NOT EXISTS(SELECT 1 FROM inventory_transfer a JOIN inventory_transfer_posting p ON p.transfer_id=a.id
          JOIN inventory_transfer_entry debit ON debit.posting_id=p.id AND debit.transfer_id=a.id AND debit.leg_no=1
          JOIN inventory_transfer_entry credit ON credit.posting_id=p.id AND credit.transfer_id=a.id AND credit.leg_no=2
          WHERE p.id={posting} AND a.id={document} AND ({source()}) AND ({destination()})
            AND debit.position_id=a.source_position_id AND credit.position_id=a.destination_position_id
            AND debit.created_by=p.created_by AND credit.created_by=p.created_by
            AND debit.qty_base_delta IS erp_decimal_neg(a.quantity) AND credit.qty_base_delta IS a.quantity
            AND erp_decimal_cmp(a.quantity,'0')>0
            AND erp_decimal_cmp((SELECT erp_decimal_sum(qty_base_delta) FROM inventory_transfer_entry
              WHERE posting_id={posting}),'0')=0);
    """


def extend_ledger():
    original = op.get_bind().exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='physical_stock_ledger'").scalar_one()
    for domain in OLD_DOMAINS:
        if original.count("'" + domain + "'") != 1:
            raise RuntimeError("Unexpected original physical-ledger domain " + domain)
    if "'INVENTORY_TRANSFER'" in original:
        raise RuntimeError("Transfer already occurs in the authoritative ledger")
    op.execute("DROP VIEW physical_stock_ledger")
    op.execute(original.rstrip().rstrip(";") + """
      UNION ALL SELECT e.id,e.posting_id,e.transfer_id,'INVENTORY_TRANSFER',e.position_id,e.qty_base_delta,NULL
      FROM inventory_transfer_entry e JOIN inventory_transfer_posting p ON p.id=e.posting_id
      JOIN inventory_transfer a ON a.id=e.transfer_id AND a.id=p.transfer_id
      WHERE p.posting_state='POSTED' AND a.state='POSTED'
    """)


def document_guards():
    audit = ["posted_at", "posted_by", "cancelled_at", "cancelled_by"]
    empty = " AND ".join("NEW." + field + " IS NULL" for field in audit)
    op.execute(f"""CREATE TRIGGER inventory_transfer_draft_insert BEFORE INSERT ON inventory_transfer
      WHEN NOT (NEW.state='DRAFT' AND NEW.row_version=1 AND NEW.destination_position_id IS NULL AND {empty})
      BEGIN SELECT RAISE(ABORT,'inventory_transfer must begin as an unposted draft'); END""")
    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER inventory_transfer_source_{action.lower()} BEFORE {action} ON inventory_transfer
          WHEN NOT (({source('NEW')}) AND
            (NEW.destination_position_id IS NULL OR ({destination('NEW')})))
          BEGIN SELECT RAISE(ABORT,'inventory_transfer requires genuine existing position lineage'); END""")
    immutable = ["id", "rowid", "document_no", "business_date", "source_position_id", "item_id", "item_kind", "unit_id",
                 "source_warehouse_id", "destination_warehouse_id", "lot_id", "quantity", "reason", "note", "created_at", "created_by"]
    same_source = " AND ".join("NEW." + field + " IS OLD." + field for field in immutable)
    unchanged = same_source + " AND NEW.destination_position_id IS OLD.destination_position_id"
    cancel = "COALESCE((erp_transfer_edit_action()='CANCEL' AND erp_transfer_edit_document() IS OLD.id),0)=1"
    op.execute(f"""CREATE TRIGGER inventory_transfer_state_machine BEFORE UPDATE ON inventory_transfer BEGIN
      SELECT RAISE(ABORT,'inventory_transfer creation identity immutable') WHERE
        NEW.id IS NOT OLD.id OR NEW.rowid<>OLD.rowid OR NEW.created_at IS NOT OLD.created_at
        OR NEW.created_by IS NOT OLD.created_by;
      SELECT RAISE(ABORT,'inventory_transfer state transition or authorization denied') WHERE NOT COALESCE((
        OLD.state='DRAFT' AND ((NEW.row_version=OLD.row_version+1 AND (
          (NEW.state='DRAFT' AND {empty} AND NEW.destination_position_id IS NULL
            AND NOT EXISTS(SELECT 1 FROM inventory_transfer_posting WHERE transfer_id=OLD.id))
          OR (NEW.state='CANCELLED' AND {unchanged} AND {cancel}
            AND NEW.posted_at IS NULL AND NEW.posted_by IS NULL
            AND NEW.cancelled_at IS NOT NULL AND NEW.cancelled_by IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM inventory_transfer_posting WHERE transfer_id=OLD.id))
          OR (NEW.state='POSTED' AND {unchanged} AND NEW.cancelled_at IS NULL AND NEW.cancelled_by IS NULL
            AND EXISTS(SELECT 1 FROM inventory_transfer_posting p WHERE p.transfer_id=OLD.id
              AND p.posting_state='POSTED' AND p.source_version=OLD.row_version AND {authorization('p')}
              AND NEW.posted_at IS p.posted_at AND NEW.posted_by IS p.posted_by))
        )) OR (NEW.state='DRAFT' AND NEW.row_version=OLD.row_version AND {empty} AND {same_source}
          AND OLD.destination_position_id IS NULL AND NEW.destination_position_id IS NOT NULL
          AND ({destination('NEW')}) AND EXISTS(SELECT 1 FROM inventory_transfer_posting p
            WHERE p.transfer_id=OLD.id AND p.posting_state='PREPARING' AND p.source_version=OLD.row_version
              AND {authorization('p')})))),0);
      SELECT RAISE(ABORT,'inventory_transfer cannot replace another document') WHERE EXISTS(
        SELECT 1 FROM inventory_transfer other WHERE other.id<>OLD.id AND other.document_no=NEW.document_no);
      END""")
    op.execute(f"""CREATE TRIGGER inventory_transfer_complete BEFORE UPDATE ON inventory_transfer
      WHEN NEW.state='POSTED' BEGIN
      {source_guard('OLD.id')}
      {entry_guard('(SELECT id FROM inventory_transfer_posting WHERE transfer_id=OLD.id)', 'OLD.id')}
      END""")
    op.execute("""CREATE TRIGGER inventory_transfer_delete_draft_only BEFORE DELETE ON inventory_transfer
      WHEN OLD.state<>'DRAFT' OR EXISTS(SELECT 1 FROM inventory_transfer_posting WHERE transfer_id=OLD.id)
      BEGIN SELECT RAISE(ABORT,'inventory_transfer final history cannot be deleted'); END""")


def posting_guards():
    op.execute("""CREATE TRIGGER inventory_transfer_key_request_identity BEFORE INSERT ON inventory_transfer_posting
      WHEN EXISTS(SELECT 1 FROM stock_create_receipt WHERE request_key=NEW.idempotency_key)
        OR EXISTS(SELECT 1 FROM stocktake WHERE start_key=NEW.idempotency_key)
      BEGIN SELECT RAISE(ABORT,'transfer key belongs to an existing creation or stocktake request'); END""")
    op.execute("""CREATE TRIGGER stock_create_receipt_inventory_transfer_key BEFORE INSERT ON stock_create_receipt
      WHEN EXISTS(SELECT 1 FROM inventory_transfer_posting WHERE idempotency_key=NEW.request_key)
      BEGIN SELECT RAISE(ABORT,'request key belongs to inventory transfer posting'); END""")
    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER stocktake_start_inventory_transfer_key_{action.lower()} BEFORE {action} ON stocktake
          WHEN NEW.start_key IS NOT NULL AND EXISTS(SELECT 1 FROM inventory_transfer_posting WHERE idempotency_key=NEW.start_key)
          BEGIN SELECT RAISE(ABORT,'stocktake start key belongs to inventory transfer posting'); END""")
    for other in OLD_POSTINGS:
        op.execute(f"""CREATE TRIGGER inventory_transfer_key_{other} BEFORE INSERT ON inventory_transfer_posting
          WHEN EXISTS(SELECT 1 FROM {other} WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'transfer key belongs to another posting domain'); END""")
        op.execute(f"""CREATE TRIGGER {other}_inventory_transfer_key BEFORE INSERT ON {other}
          WHEN EXISTS(SELECT 1 FROM inventory_transfer_posting WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'posting key belongs to inventory transfer'); END""")
    op.execute(f"""CREATE TRIGGER inventory_transfer_posting_prepare BEFORE INSERT ON inventory_transfer_posting BEGIN
      SELECT RAISE(ABORT,'inventory_transfer signed authorization denied') WHERE NOT ({authorization('NEW')});
      SELECT RAISE(ABORT,'inventory_transfer posting must prepare') WHERE NEW.posting_state<>'PREPARING';
      SELECT RAISE(ABORT,'inventory_transfer posting audit mismatch') WHERE
        NEW.posted_at IS NOT NEW.confirmation_received_at OR NEW.posted_by<>NEW.created_by;
      SELECT RAISE(ABORT,'inventory_transfer requires current draft version') WHERE NOT EXISTS(
        SELECT 1 FROM inventory_transfer a WHERE a.id=NEW.transfer_id
          AND a.state='DRAFT' AND a.row_version=NEW.source_version AND a.destination_position_id IS NULL);
      {source_guard('NEW.transfer_id')}
      END""")
    op.execute(f"""CREATE TRIGGER inventory_transfer_entry_signed BEFORE INSERT ON inventory_transfer_entry BEGIN
      SELECT RAISE(ABORT,'inventory_transfer entry is not the authorized signed magnitude') WHERE NOT EXISTS(
        SELECT 1 FROM inventory_transfer_posting p JOIN inventory_transfer a ON a.id=p.transfer_id
        WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING' AND {authorization('p')}
          AND a.id=NEW.transfer_id AND a.state='DRAFT' AND a.row_version=p.source_version
          AND ({destination()}) AND NEW.leg_no IN (1,2) AND NEW.created_by=p.created_by
          AND NEW.position_id=CASE WHEN NEW.leg_no=1 THEN a.source_position_id ELSE a.destination_position_id END
          AND erp_decimal_cmp(a.quantity,'0')>0 AND NEW.qty_base_delta IS ({signed('NEW.leg_no')}));
      {source_guard('NEW.transfer_id')}
      END""")
    protected = ["id", "rowid", "transfer_id", "idempotency_key", "request_fingerprint", "source_version",
                 "authorization_basis", "confirmation_digest", "confirmation_snapshot", "confirmation_received_at",
                 "posted_at", "posted_by", "created_at", "created_by"]
    unchanged = " AND ".join("NEW." + field + " IS OLD." + field for field in protected)
    op.execute(f"""CREATE TRIGGER inventory_transfer_posting_finish BEFORE UPDATE ON inventory_transfer_posting BEGIN
      SELECT RAISE(ABORT,'inventory_transfer signed authorization denied') WHERE NOT ({authorization('OLD')});
      SELECT RAISE(ABORT,'inventory_transfer posting immutable') WHERE NOT (
        OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {unchanged});
      SELECT RAISE(ABORT,'inventory_transfer source version changed') WHERE NOT EXISTS(
        SELECT 1 FROM inventory_transfer a WHERE a.id=OLD.transfer_id AND a.state='DRAFT'
          AND a.row_version=OLD.source_version);
      {source_guard('OLD.transfer_id')}{entry_guard('OLD.id', 'OLD.transfer_id')}
      END""")
    op.execute("""CREATE TRIGGER inventory_transfer_posting_no_delete BEFORE DELETE ON inventory_transfer_posting
      BEGIN SELECT RAISE(ABORT,'inventory_transfer posting cannot be deleted'); END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER inventory_transfer_entry_immutable_{action.lower()} BEFORE {action} ON inventory_transfer_entry
          BEGIN SELECT RAISE(ABORT,'inventory_transfer movement immutable'); END""")


def freeze_barriers():
    entry = "inventory_transfer_entry"
    table = "inventory_transfer_posting"
    frozen = stocktaking.frozen
    affected = lambda identity: f"EXISTS(SELECT 1 FROM inventory_transfer a WHERE a.id={identity} AND ({frozen('a.source_warehouse_id')} OR {frozen('a.destination_warehouse_id')}))"
    op.execute(f"""CREATE TRIGGER warehouse_freeze_inventory_transfer_entry_insert BEFORE INSERT ON {entry}
      WHEN {affected('NEW.transfer_id')} OR EXISTS(SELECT 1 FROM stock_position position
        WHERE position.id=NEW.position_id AND {frozen('position.warehouse_id')})
      BEGIN SELECT RAISE(ABORT,'warehouse frozen by active stocktake'); END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER warehouse_freeze_inventory_transfer_entry_{action.lower()} BEFORE {action} ON {entry}
          WHEN {affected('OLD.transfer_id')} OR EXISTS(SELECT 1 FROM stock_position position WHERE position.id=OLD.position_id AND {frozen('position.warehouse_id')})
          BEGIN SELECT RAISE(ABORT,'warehouse frozen by active stocktake'); END""")
    for action, predicate in [("INSERT", affected("NEW.transfer_id")),
                              ("UPDATE", affected("OLD.transfer_id") + " OR " + affected("NEW.transfer_id")),
                              ("DELETE", affected("OLD.transfer_id"))]:
        op.execute(f"""CREATE TRIGGER warehouse_freeze_inventory_transfer_posting_{action.lower()} BEFORE {action} ON {table}
          WHEN {predicate} BEGIN SELECT RAISE(ABORT,'warehouse frozen by active stocktake'); END""")


def identity_holds():
    """New typed references retain zero-position lineage without touching old guards."""
    for table, fields in [("stock_position", ["source_position_id", "destination_position_id"]), ("inventory_lot", ["lot_id"]),
                         ("item", ["item_id"]), ("unit", ["unit_id"]), ("warehouse", ["source_warehouse_id", "destination_warehouse_id"])]:
        referenced = lambda identity: " OR ".join(field + "=" + identity for field in fields)
        op.execute(f"""CREATE TRIGGER inventory_transfer_{table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} prior WHERE {stocktaking.collisions(table)}
            AND EXISTS(SELECT 1 FROM inventory_transfer WHERE {referenced('prior.id')}))
          BEGIN SELECT RAISE(ABORT,'inventory_transfer referenced source cannot be replaced'); END""")
        if table in {"stock_position", "inventory_lot"}:
            # Their existing unconditional UPDATE/DELETE guards remain intact.
            continue
        changed = "NEW.id IS NOT OLD.id OR NEW.rowid<>OLD.rowid"
        if table == "item":
            changed += " OR NEW.item_kind IS NOT OLD.item_kind OR NEW.base_unit_id IS NOT OLD.base_unit_id OR NEW.lot_tracking IS NOT OLD.lot_tracking"
        op.execute(f"""CREATE TRIGGER inventory_transfer_{table}_basis BEFORE UPDATE ON {table}
          WHEN ({changed}) AND EXISTS(SELECT 1 FROM inventory_transfer WHERE {referenced('OLD.id')})
          BEGIN SELECT RAISE(ABORT,'inventory_transfer source identity and unit basis immutable'); END""")
        op.execute(f"""CREATE TRIGGER inventory_transfer_{table}_delete BEFORE DELETE ON {table}
          WHEN EXISTS(SELECT 1 FROM inventory_transfer WHERE {referenced('OLD.id')})
          BEGIN SELECT RAISE(ABORT,'inventory_transfer referenced source cannot be deleted'); END""")


def allow_transfer_destination_position():
    """Append one precise grant alternative; preserve all old insertion semantics."""
    original = op.get_bind().exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='stock_position_identity'").scalar_one()
    before, separator, body = original.partition("BEGIN")
    prefix, marker, predicate = before.partition("WHEN NOT ")
    if not separator or not marker or "erp_transfer_" in original:
        raise RuntimeError("Unexpected original stock_position identity trigger")
    transfer = f"""EXISTS(SELECT 1 FROM inventory_transfer a JOIN inventory_transfer_posting p ON p.transfer_id=a.id
      WHERE p.posting_state='PREPARING' AND a.state='DRAFT' AND a.row_version=p.source_version
        AND a.destination_position_id IS NULL AND {authorization('p')} AND ({source()})
        AND NEW.item_id=a.item_id AND NEW.warehouse_id=a.destination_warehouse_id
        AND NEW.lot_id IS a.lot_id AND NEW.created_by=p.created_by
        AND EXISTS(SELECT 1 FROM warehouse w WHERE w.id=a.source_warehouse_id AND w.is_active=1)
        AND EXISTS(SELECT 1 FROM warehouse w WHERE w.id=a.destination_warehouse_id AND w.is_active=1)
        AND NOT ({stocktaking.frozen('a.source_warehouse_id')})
        AND NOT ({stocktaking.frozen('a.destination_warehouse_id')})
        AND erp_decimal_cmp(a.quantity,'0')>0
        AND erp_decimal_cmp(a.quantity,{stocktaking.book('a.source_position_id')})<=0)"""
    op.execute("DROP TRIGGER stock_position_identity")
    op.execute(prefix + "WHEN NOT (" + predicate.strip() + " OR " + transfer + ") BEGIN" + body)


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Transfer migration requires foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing foreign-key violations prevent transfer migration")
    fk = foundation.fk
    op.create_table("inventory_transfer", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("business_date", sa.Text(), nullable=False),
        sa.Column("source_position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("destination_warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("item_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False), sa.Column("unit_id", sa.Text(), fk("unit.id"), nullable=False),
        sa.Column("source_warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("lot_id", sa.Text(), fk("inventory_lot.id")),
        sa.Column("destination_position_id", sa.Text(), fk("stock_position.id")),
        sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("note", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.Text()), sa.Column("posted_by", sa.Text(), fk("actor.id")),
        sa.Column("cancelled_at", sa.Text()), sa.Column("cancelled_by", sa.Text(), fk("actor.id")),
        exact("quantity"), foundation.date_check("business_date"),
        utc("created_at"), utc("updated_at"), utc("posted_at", nullable=True), utc("cancelled_at", nullable=True),
        sa.CheckConstraint("trim(document_no)<>'' AND substr(business_date,1,4)<>'0000' "
            "AND typeof(row_version)='integer' AND row_version>0 AND item_kind IN ('RAW','FINISHED') "
            "AND source_warehouse_id<>destination_warehouse_id AND state IN ('DRAFT','POSTED','CANCELLED') "
            "AND erp_decimal_cmp(quantity,'0')>0 AND " + reason_text("inventory_transfer") + "<>''", name="ck_transfer_state"),
        sa.CheckConstraint("(state='POSTED' AND posted_at IS NOT NULL AND posted_by IS NOT NULL) OR "
            "(state<>'POSTED' AND posted_at IS NULL AND posted_by IS NULL)", name="ck_transfer_posted_audit"),
        sa.CheckConstraint("(state='CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL) OR "
            "(state<>'CANCELLED' AND cancelled_at IS NULL AND cancelled_by IS NULL)", name="ck_transfer_cancel_audit"))
    op.create_table("inventory_transfer_posting", *receiving.created(),
        sa.Column("transfer_id", sa.Text(), fk("inventory_transfer.id"), nullable=False, unique=True),
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
            "AND confirmation_digest NOT GLOB '*[^0-9a-f]*' AND json_valid(confirmation_snapshot)", name="ck_transfer_signed_posting"))
    op.create_table("inventory_transfer_entry", *receiving.created(),
        sa.Column("posting_id", sa.Text(), fk("inventory_transfer_posting.id"), nullable=False),
        sa.Column("transfer_id", sa.Text(), fk("inventory_transfer.id"), nullable=False),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False), sa.Column("qty_base_delta", sa.Text(), nullable=False),
        exact("qty_base_delta"), utc("created_at"),
        sa.UniqueConstraint("posting_id", "leg_no", name="uq_transfer_posting_leg"),
        sa.UniqueConstraint("transfer_id", "leg_no", name="uq_transfer_document_leg"),
        sa.CheckConstraint("typeof(leg_no)='integer' AND leg_no IN (1,2) AND erp_decimal_cmp(qty_base_delta,'0')<>0", name="ck_transfer_entry_nonzero"))
    for table, fields in {
        "inventory_transfer": ["source_position_id", "destination_position_id", "item_id", "unit_id", "source_warehouse_id", "destination_warehouse_id", "lot_id", "created_by", "posted_by", "cancelled_by"],
        "inventory_transfer_posting": ["transfer_id", "created_by", "posted_by"],
        "inventory_transfer_entry": ["posting_id", "transfer_id", "position_id", "created_by"],
    }.items():
        for field in fields:
            op.create_index("ix_" + table + "_" + field, table, [field])
    extend_ledger()
    for table in ["inventory_transfer", "inventory_transfer_posting", "inventory_transfer_entry"]:
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} prior WHERE {stocktaking.collisions(table)})
          BEGIN SELECT RAISE(ABORT,'inventory_transfer existing identity cannot be replaced'); END""")
    document_guards()
    posting_guards()
    freeze_barriers()
    identity_holds()
    allow_transfer_destination_position()
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Transfer migration produced invalid foreign-key references")
    if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
        raise RuntimeError("Transfer migration failed integrity checking")


def downgrade():
    raise RuntimeError("Transfer downgrade disabled; preserve immutable history and verified backups.")
