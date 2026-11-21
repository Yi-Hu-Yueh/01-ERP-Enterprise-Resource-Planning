"""Whole-warehouse stocktakes, durable freeze barriers and atomic exact variances.

Old tables and their existing triggers remain intact. The one physical ledger is
extended only after the new source-linked tables exist. No Owner stock is posted.
"""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "0018_stocktakes"
down_revision = "0017_customer_returns"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")
finished = import_module("backend.migrations.versions.0014_finished_goods_receipts")

OLD_DOMAINS = ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN",
               "FINISHED_GOODS_RECEIPT", "SALES_SHIPMENT", "CUSTOMER_RETURN"]
OLD_ENTRIES = ["stock_entry", "supplier_return_entry", "material_issue_entry", "material_return_entry",
               "finished_goods_entry", "sales_shipment_entry", "customer_return_entry"]
OLD_POSTINGS = ["stock_posting", "supplier_return_posting", "material_issue_posting", "material_return_posting",
                "finished_goods_posting", "sales_shipment_posting", "customer_return_finalization"]


def exact(column, nullable=False):
    constraint = foundation.decimal_check(column)
    if nullable:
        constraint = sa.CheckConstraint(f"{column} IS NULL OR ({constraint.sqltext})")
    constraint.name = "ck_stocktake_exact_" + column
    return constraint


def utc(column, nullable=False):
    constraint = finished.utc(column)
    if nullable:
        constraint = sa.CheckConstraint(f"{column} IS NULL OR ({constraint.sqltext})")
    constraint.name = "ck_stocktake_utc_" + column
    return constraint


def edit(action, document):
    """Private, connection-bound lifecycle authority; unavailable to raw clients."""
    return f"COALESCE((erp_stocktake_edit_action()='{action}' AND erp_stocktake_edit_document() IS {document}),0)=1"


def authorization(alias):
    return f"""COALESCE((erp_stocktake_authorized()=1
      AND {alias}.authorization_basis='LOCAL_DEMO_MANUAL'
      AND erp_stocktake_document() IS {alias}.stocktake_id
      AND erp_stocktake_digest() IS {alias}.confirmation_digest
      AND erp_stocktake_snapshot() IS {alias}.confirmation_snapshot
      AND erp_stocktake_time() IS {alias}.confirmation_received_at),0)=1"""


def frozen(warehouse):
    return f"EXISTS(SELECT 1 FROM stocktake freeze WHERE freeze.warehouse_id={warehouse} AND freeze.freeze_active=1)"


def book(position):
    return f"COALESCE((SELECT erp_decimal_sum(qty_base_delta) FROM physical_stock_ledger WHERE position_id={position}),'0')"


def line_source(alias="l"):
    return f"""EXISTS(SELECT 1 FROM stock_position p JOIN item i ON i.id=p.item_id
      JOIN stocktake s ON s.id={alias}.stocktake_id
      WHERE p.id={alias}.position_id AND p.item_id={alias}.item_id
        AND i.item_kind={alias}.item_kind AND i.base_unit_id={alias}.unit_id
        AND p.warehouse_id={alias}.warehouse_id AND p.warehouse_id=s.warehouse_id
        AND p.lot_id IS {alias}.lot_id
        AND ((i.lot_tracking=0 AND p.lot_id IS NULL) OR (i.lot_tracking=1 AND EXISTS(
          SELECT 1 FROM inventory_lot lot WHERE lot.id=p.lot_id AND lot.item_id=i.id))))"""


def coverage_guard(document, counted=False):
    """All and only real warehouse positions, with unaltered book quantities."""
    sql = f"""
      SELECT RAISE(ABORT,'stocktake snapshot does not cover the whole warehouse') WHERE EXISTS(
        SELECT 1 FROM stock_position p JOIN stocktake s ON s.warehouse_id=p.warehouse_id
        WHERE s.id={document} AND NOT EXISTS(SELECT 1 FROM stocktake_line l
          WHERE l.stocktake_id=s.id AND l.position_id=p.id));
      SELECT RAISE(ABORT,'stocktake source identity or frozen book quantity changed') WHERE EXISTS(
        SELECT 1 FROM stocktake_line l WHERE l.stocktake_id={document}
          AND (NOT ({line_source()}) OR l.book_qty IS NOT {book('l.position_id')}));
    """
    if counted:
        sql += f"""
      SELECT RAISE(ABORT,'stocktake has unentered physical counts') WHERE EXISTS(
        SELECT 1 FROM stocktake_line WHERE stocktake_id={document} AND counted_qty IS NULL);
      SELECT RAISE(ABORT,'stocktake has unresolved physical-stock observations') WHERE EXISTS(
        SELECT 1 FROM stocktake_exception WHERE stocktake_id={document});
        """
    return sql


def entry_guard(posting, document):
    return f"""
      SELECT RAISE(ABORT,'stocktake variance movements do not exactly match counts') WHERE EXISTS(
        SELECT 1 FROM stocktake_entry e LEFT JOIN stocktake_line l ON l.id=e.stocktake_line_id
        JOIN stocktake_posting p ON p.id=e.posting_id
        WHERE e.posting_id={posting} AND (l.id IS NULL OR e.stocktake_id IS NOT {document}
          OR l.stocktake_id IS NOT {document} OR e.position_id IS NOT l.position_id
          OR l.counted_qty IS NULL OR erp_decimal_cmp(l.counted_qty,l.book_qty)=0
          OR e.qty_base_delta IS NOT erp_decimal_sub(l.counted_qty,l.book_qty)
          OR e.leg_no<>1 OR e.created_by<>p.created_by));
      SELECT RAISE(ABORT,'stocktake variance movement missing or extra') WHERE EXISTS(
        SELECT 1 FROM stocktake_line l WHERE l.stocktake_id={document}
          AND ((erp_decimal_cmp(l.counted_qty,l.book_qty)<>0 AND
            (SELECT count(*) FROM stocktake_entry e WHERE e.posting_id={posting}
              AND e.stocktake_line_id=l.id)<>1)
          OR (erp_decimal_cmp(l.counted_qty,l.book_qty)=0 AND EXISTS(
            SELECT 1 FROM stocktake_entry e WHERE e.stocktake_line_id=l.id))));
    """


def extend_ledger():
    original = op.get_bind().exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='physical_stock_ledger'").scalar_one()
    for domain in OLD_DOMAINS:
        if original.count("'" + domain + "'") != 1:
            raise RuntimeError("Unexpected original physical-ledger domain " + domain)
    if "'STOCKTAKE'" in original:
        raise RuntimeError("Stocktake already occurs in the shared physical ledger")
    op.execute("DROP VIEW physical_stock_ledger")
    op.execute(original.rstrip().rstrip(";") + """
      UNION ALL SELECT e.id,e.posting_id,e.stocktake_id,'STOCKTAKE',e.position_id,e.qty_base_delta,NULL
      FROM stocktake_entry e JOIN stocktake_posting p ON p.id=e.posting_id
      JOIN stocktake s ON s.id=e.stocktake_id AND s.id=p.stocktake_id
      WHERE p.posting_state='POSTED' AND s.state='CLOSED'
    """)


def collisions(table, old="prior"):
    """INSERT OR REPLACE must not bypass DELETE guards when recursion is OFF."""
    terms = [f"{old}.id=NEW.id", f"{old}.rowid=NEW.rowid"]
    connection = op.get_bind()
    for index in connection.exec_driver_sql(f'PRAGMA index_list("{table}")'):
        if not index[2]:
            continue
        columns = [row[2] for row in connection.exec_driver_sql(f'PRAGMA index_info("{index[1]}")')]
        if not columns or any(column is None for column in columns):
            raise RuntimeError("Unsupported identity index on " + table)
        conditions = [f'NEW."{column}" IS NOT NULL AND {old}."{column}" IS NEW."{column}"' for column in columns]
        if index[4]:
            predicates = {
                "uq_stocktake_active_warehouse": "{alias}.freeze_active=1",
                "uq_stock_position_without_lot": "{alias}.lot_id IS NULL",
                "uq_stock_position_with_lot": "{alias}.lot_id IS NOT NULL",
            }
            if index[1] not in predicates:
                raise RuntimeError("Unsupported partial identity index on " + table + ": " + index[1])
            predicate = predicates[index[1]]
            conditions.extend([predicate.format(alias=old), predicate.format(alias="NEW")])
        terms.append("(" + " AND ".join(conditions) + ")")
    return "(" + " OR ".join(terms) + ")"


def freeze_barriers():
    """One persisted warehouse rule for every previous ordinary stock writer."""
    for table in OLD_ENTRIES:
        incoming = f"EXISTS(SELECT 1 FROM stock_position p WHERE p.id=NEW.position_id AND {frozen('p.warehouse_id')})"
        outgoing = f"EXISTS(SELECT 1 FROM stock_position p WHERE p.id=OLD.position_id AND {frozen('p.warehouse_id')})"
        replacing = f"EXISTS(SELECT 1 FROM {table} prior JOIN stock_position p ON p.id=prior.position_id WHERE {collisions(table)} AND {frozen('p.warehouse_id')})"
        for action, predicate in [("INSERT", f"{incoming} OR {replacing}"),
                                  ("UPDATE", f"{incoming} OR {outgoing}"), ("DELETE", outgoing)]:
            op.execute(f"""CREATE TRIGGER warehouse_freeze_{table}_{action.lower()} BEFORE {action} ON {table}
              WHEN {predicate} BEGIN SELECT RAISE(ABORT,'warehouse frozen by active stocktake'); END""")
    # A ledger entry is visible only through its posting. Guarding entry writes
    # alone would miss replacement of a posted parent, or finalization of a
    # PREPARING parent whose entries were registered before the freeze.
    for table, entry in zip(OLD_POSTINGS, OLD_ENTRIES):
        posting_fk = "finalization_id" if table == "customer_return_finalization" else "posting_id"
        affected = lambda identity: f"EXISTS(SELECT 1 FROM {entry} e JOIN stock_position p ON p.id=e.position_id WHERE e.{posting_fk}={identity} AND {frozen('p.warehouse_id')})"
        replacing = f"EXISTS(SELECT 1 FROM {table} prior WHERE {collisions(table)} AND {affected('prior.id')})"
        for action, predicate in [("INSERT", replacing),
                                  ("UPDATE", f"{affected('OLD.id')} OR {affected('NEW.id')} OR {replacing}"),
                                  ("DELETE", affected("OLD.id"))]:
            op.execute(f"""CREATE TRIGGER warehouse_freeze_{table}_{action.lower()} BEFORE {action} ON {table}
              WHEN {predicate} BEGIN SELECT RAISE(ABORT,'warehouse frozen posting history immutable'); END""")
    incoming = frozen("NEW.warehouse_id")
    outgoing = frozen("OLD.warehouse_id")
    replacing = f"EXISTS(SELECT 1 FROM stock_position prior WHERE {collisions('stock_position')} AND {frozen('prior.warehouse_id')})"
    for action, predicate in [("INSERT", f"{incoming} OR {replacing}"),
                              ("UPDATE", f"{incoming} OR {outgoing}"), ("DELETE", outgoing)]:
        op.execute(f"""CREATE TRIGGER warehouse_freeze_position_{action.lower()} BEFORE {action} ON stock_position
          WHEN {predicate} BEGIN SELECT RAISE(ABORT,'warehouse frozen by active stocktake'); END""")
    # A global unattached lot does not identify a warehouse. Linking it through a
    # position is covered above; changing/replacing an associated lot is blocked.
    for table, relation in [
        ("inventory_lot", "EXISTS(SELECT 1 FROM stock_position p WHERE p.lot_id={id} AND " + frozen("p.warehouse_id") + ")"),
        ("warehouse", frozen("{id}")),
        ("item", "EXISTS(SELECT 1 FROM stock_position p WHERE p.item_id={id} AND " + frozen("p.warehouse_id") + ")"),
    ]:
        old_frozen = relation.format(id="OLD.id")
        replace_frozen = relation.format(id="prior.id")
        op.execute(f"""CREATE TRIGGER warehouse_freeze_{table}_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} prior WHERE {collisions(table)} AND {replace_frozen})
          BEGIN SELECT RAISE(ABORT,'warehouse frozen identity cannot be replaced'); END""")
        for action in ["UPDATE", "DELETE"]:
            # Warehouse/item descriptive edits remain possible; identity and
            # inventory semantics cannot be changed underneath a frozen snapshot.
            changed = "1" if table == "inventory_lot" or action == "DELETE" else (
                "NEW.id IS NOT OLD.id OR NEW.rowid<>OLD.rowid" if table == "warehouse" else
                "NEW.id IS NOT OLD.id OR NEW.rowid<>OLD.rowid OR NEW.item_kind IS NOT OLD.item_kind "
                "OR NEW.base_unit_id IS NOT OLD.base_unit_id OR NEW.lot_tracking IS NOT OLD.lot_tracking")
            op.execute(f"""CREATE TRIGGER warehouse_freeze_{table}_{action.lower()} BEFORE {action} ON {table}
              WHEN ({old_frozen}) AND ({changed})
              BEGIN SELECT RAISE(ABORT,'warehouse frozen inventory identity immutable'); END""")


def header_guards():
    lifecycle = ["snapshot_at", "started_by", "start_key", "start_fingerprint", "closed_at", "closed_by", "cancelled_at", "cancelled_by"]
    empty = " AND ".join("NEW." + column + " IS NULL" for column in lifecycle)
    op.execute(f"""CREATE TRIGGER stocktake_draft_insert BEFORE INSERT ON stocktake
      WHEN NOT (NEW.state='DRAFT' AND NEW.freeze_active=0 AND NEW.row_version=1 AND {empty})
      BEGIN SELECT RAISE(ABORT,'stocktake must begin as an unfrozen draft'); END""")
    immutable = ["id", "rowid", "document_no", "warehouse_id", "business_date", "scope_kind", "note", "created_at", "created_by"]
    source_same = " AND ".join("NEW." + column + " IS OLD." + column for column in immutable)
    snapshot_same = " AND ".join("NEW." + column + " IS OLD." + column for column in lifecycle[:4])
    terminal_empty = " AND ".join("NEW." + column + " IS NULL" for column in lifecycle[4:])
    version = "NEW.row_version=OLD.row_version+1"
    state_from_counts = """((NEW.state='ACTIVE_FROZEN' AND EXISTS(SELECT 1 FROM stocktake_line
        WHERE stocktake_id=OLD.id AND counted_qty IS NULL)) OR
      (NEW.state='REVIEW_READY' AND NOT EXISTS(SELECT 1 FROM stocktake_line
        WHERE stocktake_id=OLD.id AND counted_qty IS NULL)))"""
    op.execute(f"""CREATE TRIGGER stocktake_state_machine BEFORE UPDATE ON stocktake BEGIN
      SELECT RAISE(ABORT,'stocktake creation identity immutable') WHERE
        NEW.id IS NOT OLD.id OR NEW.rowid<>OLD.rowid OR NEW.created_at IS NOT OLD.created_at
        OR NEW.created_by IS NOT OLD.created_by;
      SELECT RAISE(ABORT,'stocktake state transition or authorization denied') WHERE NOT COALESCE((
        (OLD.state='DRAFT' AND NEW.state='DRAFT' AND {version} AND NEW.freeze_active=0 AND {empty})
        OR (OLD.state='DRAFT' AND NEW.state='STARTING' AND {edit('START', 'OLD.id')}
          AND {version} AND {source_same} AND NEW.freeze_active=1
          AND NEW.snapshot_at IS NOT NULL AND NEW.started_by IS NOT NULL
          AND NEW.start_key IS NOT NULL AND NEW.start_fingerprint IS NOT NULL AND {terminal_empty})
        OR (OLD.state='STARTING' AND NEW.state IN ('ACTIVE_FROZEN','REVIEW_READY')
          AND {edit('START', 'OLD.id')} AND NEW.row_version=OLD.row_version
          AND {source_same} AND {snapshot_same} AND {terminal_empty} AND {state_from_counts})
        OR (OLD.state IN ('ACTIVE_FROZEN','REVIEW_READY') AND NEW.state IN ('ACTIVE_FROZEN','REVIEW_READY')
          AND {edit('COUNT', 'OLD.id')} AND {version} AND {source_same} AND {snapshot_same}
          AND {terminal_empty} AND {state_from_counts})
        OR (OLD.state IN ('ACTIVE_FROZEN','REVIEW_READY') AND NEW.state=OLD.state
          AND {edit('EXCEPTION', 'OLD.id')} AND {version} AND {source_same} AND {snapshot_same} AND {terminal_empty})
        OR (OLD.state IN ('DRAFT','ACTIVE_FROZEN','REVIEW_READY') AND NEW.state='CANCELLED'
          AND {edit('CANCEL', 'OLD.id')} AND {version} AND {source_same} AND {snapshot_same}
          AND NEW.freeze_active=0 AND NEW.closed_at IS NULL AND NEW.closed_by IS NULL
          AND NEW.cancelled_at IS NOT NULL AND NEW.cancelled_by IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM stocktake_posting WHERE stocktake_id=OLD.id))
        OR (OLD.state='REVIEW_READY' AND NEW.state='CLOSED' AND {version}
          AND {source_same} AND {snapshot_same} AND NEW.freeze_active=0
          AND NEW.cancelled_at IS NULL AND NEW.cancelled_by IS NULL
          AND EXISTS(SELECT 1 FROM stocktake_posting p WHERE p.stocktake_id=OLD.id
            AND p.posting_state='POSTED' AND p.source_version=OLD.row_version
            AND {authorization('p')} AND NEW.closed_at IS p.posted_at AND NEW.closed_by IS p.posted_by))
      ),0);
      SELECT RAISE(ABORT,'stocktake cannot replace another document') WHERE EXISTS(
        SELECT 1 FROM stocktake other WHERE other.id<>OLD.id AND (other.document_no=NEW.document_no
          OR (NEW.start_key IS NOT NULL AND other.start_key=NEW.start_key)));
      END""")
    for target in ["ACTIVE_FROZEN", "REVIEW_READY", "CLOSED"]:
        op.execute(f"""CREATE TRIGGER stocktake_coverage_{target.lower()} BEFORE UPDATE ON stocktake
          WHEN NEW.state='{target}' AND (OLD.state='STARTING' OR NEW.state='CLOSED') BEGIN
          {coverage_guard('OLD.id', counted=target == 'CLOSED')}
          END""")
    op.execute(f"""CREATE TRIGGER stocktake_close_entries BEFORE UPDATE ON stocktake WHEN NEW.state='CLOSED' BEGIN
      {entry_guard('(SELECT id FROM stocktake_posting WHERE stocktake_id=OLD.id)', 'OLD.id')}
      END""")
    op.execute("""CREATE TRIGGER stocktake_delete_draft_only BEFORE DELETE ON stocktake
      WHEN OLD.state<>'DRAFT' OR OLD.freeze_active<>0
      BEGIN SELECT RAISE(ABORT,'stocktake snapshot and final history cannot be deleted'); END""")


def line_guards():
    op.execute(f"""CREATE TRIGGER stocktake_line_snapshot_insert BEFORE INSERT ON stocktake_line BEGIN
      SELECT RAISE(ABORT,'stocktake snapshot line creation denied') WHERE NOT EXISTS(
        SELECT 1 FROM stocktake s WHERE s.id=NEW.stocktake_id AND s.state='STARTING'
          AND s.freeze_active=1 AND {edit('START', 's.id')} AND NEW.row_version=1
          AND NEW.counted_qty IS NULL AND NEW.counted_at IS NULL AND NEW.counted_by IS NULL
          AND NEW.note IS NULL AND NEW.created_by=s.started_by);
      SELECT RAISE(ABORT,'stocktake snapshot line source mismatch') WHERE NOT ({line_source('NEW')})
        OR NEW.book_qty IS NOT {book('NEW.position_id')};
      END""")
    protected = ["id", "rowid", "stocktake_id", "position_id", "item_id", "item_kind", "unit_id", "warehouse_id",
                 "lot_id", "book_qty", "created_at", "created_by"]
    same = " AND ".join("NEW." + column + " IS OLD." + column for column in protected)
    op.execute(f"""CREATE TRIGGER stocktake_line_count_update BEFORE UPDATE ON stocktake_line BEGIN
      SELECT RAISE(ABORT,'stocktake snapshot or finalized count immutable') WHERE NOT COALESCE((
        {same} AND NEW.row_version=OLD.row_version+1 AND EXISTS(SELECT 1 FROM stocktake s
          WHERE s.id=OLD.stocktake_id AND s.state IN ('ACTIVE_FROZEN','REVIEW_READY')
            AND s.freeze_active=1 AND {edit('COUNT', 's.id')}
            AND NOT EXISTS(SELECT 1 FROM stocktake_posting WHERE stocktake_id=s.id))),0);
      END""")
    op.execute("""CREATE TRIGGER stocktake_line_no_delete BEFORE DELETE ON stocktake_line
      BEGIN SELECT RAISE(ABORT,'stocktake snapshot line cannot be deleted'); END""")
    op.execute(f"""CREATE TRIGGER stocktake_exception_insert BEFORE INSERT ON stocktake_exception
      WHEN NOT EXISTS(SELECT 1 FROM stocktake s WHERE s.id=NEW.stocktake_id
        AND s.state IN ('ACTIVE_FROZEN','REVIEW_READY') AND s.freeze_active=1
        AND {edit('EXCEPTION', 's.id')} AND NOT EXISTS(SELECT 1 FROM stocktake_posting WHERE stocktake_id=s.id))
      BEGIN SELECT RAISE(ABORT,'stocktake observation requires an active frozen document'); END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER stocktake_exception_immutable_{action.lower()} BEFORE {action} ON stocktake_exception
          BEGIN SELECT RAISE(ABORT,'stocktake unmatched observation is immutable'); END""")


def posting_guards():
    for other in OLD_POSTINGS:
        op.execute(f"""CREATE TRIGGER stocktake_key_{other} BEFORE INSERT ON stocktake_posting
          WHEN EXISTS(SELECT 1 FROM {other} WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'stocktake key belongs to another posting domain'); END""")
        op.execute(f"""CREATE TRIGGER {other}_stocktake_key BEFORE INSERT ON {other}
          WHEN EXISTS(SELECT 1 FROM stocktake_posting WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'posting key belongs to stocktake'); END""")
    op.execute(f"""CREATE TRIGGER stocktake_posting_prepare BEFORE INSERT ON stocktake_posting BEGIN
      SELECT RAISE(ABORT,'stocktake signed closure authorization denied') WHERE NOT ({authorization('NEW')});
      SELECT RAISE(ABORT,'stocktake posting must prepare') WHERE NEW.posting_state<>'PREPARING';
      SELECT RAISE(ABORT,'stocktake closure audit mismatch') WHERE
        NEW.posted_at IS NOT NEW.confirmation_received_at OR NEW.posted_by<>NEW.created_by;
      SELECT RAISE(ABORT,'stocktake closure requires current review-ready freeze') WHERE NOT EXISTS(
        SELECT 1 FROM stocktake s WHERE s.id=NEW.stocktake_id AND s.state='REVIEW_READY'
          AND s.freeze_active=1 AND s.row_version=NEW.source_version);
      {coverage_guard('NEW.stocktake_id', counted=True)}
      END""")
    op.execute(f"""CREATE TRIGGER stocktake_entry_variance BEFORE INSERT ON stocktake_entry BEGIN
      SELECT RAISE(ABORT,'stocktake variance entry is not an authorized counted difference') WHERE NOT EXISTS(
        SELECT 1 FROM stocktake_posting p JOIN stocktake s ON s.id=p.stocktake_id
        JOIN stocktake_line l ON l.stocktake_id=s.id WHERE p.id=NEW.posting_id
          AND p.posting_state='PREPARING' AND {authorization('p')} AND s.id=NEW.stocktake_id
          AND s.state='REVIEW_READY' AND s.freeze_active=1 AND s.row_version=p.source_version
          AND l.id=NEW.stocktake_line_id AND l.position_id=NEW.position_id
          AND l.counted_qty IS NOT NULL AND erp_decimal_cmp(l.counted_qty,'0')>=0
          AND erp_decimal_cmp(l.counted_qty,l.book_qty)<>0
          AND NEW.qty_base_delta IS erp_decimal_sub(l.counted_qty,l.book_qty)
          AND NEW.leg_no=1 AND NEW.created_by=p.created_by);
      {coverage_guard('NEW.stocktake_id', counted=True)}
      END""")
    protected = ["id", "rowid", "stocktake_id", "idempotency_key", "request_fingerprint", "source_version",
                 "authorization_basis", "confirmation_digest", "confirmation_snapshot", "confirmation_received_at",
                 "posted_at", "posted_by", "created_at", "created_by"]
    same = " AND ".join("NEW." + column + " IS OLD." + column for column in protected)
    op.execute(f"""CREATE TRIGGER stocktake_posting_finish BEFORE UPDATE ON stocktake_posting BEGIN
      SELECT RAISE(ABORT,'stocktake signed closure authorization denied') WHERE NOT ({authorization('OLD')});
      SELECT RAISE(ABORT,'stocktake posting history immutable') WHERE NOT (
        OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {same});
      SELECT RAISE(ABORT,'stocktake closure source version or freeze changed') WHERE NOT EXISTS(
        SELECT 1 FROM stocktake s WHERE s.id=OLD.stocktake_id AND s.state='REVIEW_READY'
          AND s.freeze_active=1 AND s.row_version=OLD.source_version);
      {coverage_guard('OLD.stocktake_id', counted=True)}
      {entry_guard('OLD.id', 'OLD.stocktake_id')}
      END""")
    op.execute("""CREATE TRIGGER stocktake_posting_no_delete BEFORE DELETE ON stocktake_posting
      BEGIN SELECT RAISE(ABORT,'stocktake posting cannot be deleted'); END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER stocktake_entry_immutable_{action.lower()} BEFORE {action} ON stocktake_entry
          BEGIN SELECT RAISE(ABORT,'stocktake variance movement immutable'); END""")


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Stocktake migration requires foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing foreign-key violations prevent stocktake migration")
    fk = foundation.fk
    op.create_table("stocktake", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("business_date", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False), sa.Column("state", sa.Text(), nullable=False),
        sa.Column("freeze_active", sa.Boolean(create_constraint=False), nullable=False), sa.Column("note", sa.Text()),
        sa.Column("snapshot_at", sa.Text()), sa.Column("started_by", sa.Text(), fk("actor.id")),
        sa.Column("start_key", sa.Text(), unique=True), sa.Column("start_fingerprint", sa.Text()),
        sa.Column("closed_at", sa.Text()), sa.Column("closed_by", sa.Text(), fk("actor.id")),
        sa.Column("cancelled_at", sa.Text()), sa.Column("cancelled_by", sa.Text(), fk("actor.id")),
        foundation.date_check("business_date"), utc("created_at"), utc("updated_at"),
        utc("snapshot_at", nullable=True), utc("closed_at", nullable=True), utc("cancelled_at", nullable=True),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0 AND trim(document_no)<>'' "
            "AND substr(business_date,1,4)<>'0000' AND scope_kind='WHOLE_WAREHOUSE' "
            "AND state IN ('DRAFT','STARTING','ACTIVE_FROZEN','REVIEW_READY','CLOSED','CANCELLED') "
            "AND typeof(freeze_active)='integer' AND freeze_active IN (0,1) "
            "AND freeze_active=(state IN ('STARTING','ACTIVE_FROZEN','REVIEW_READY'))", name="ck_stocktake_state"),
        sa.CheckConstraint("(snapshot_at IS NULL AND started_by IS NULL AND start_key IS NULL AND start_fingerprint IS NULL) "
            "OR (snapshot_at IS NOT NULL AND started_by IS NOT NULL AND start_key IS NOT NULL "
            "AND start_fingerprint IS NOT NULL AND trim(start_fingerprint)<>'')", name="ck_stocktake_snapshot_audit"),
        sa.CheckConstraint("(closed_at IS NULL AND closed_by IS NULL AND state<>'CLOSED') OR "
            "(closed_at IS NOT NULL AND closed_by IS NOT NULL AND state='CLOSED')", name="ck_stocktake_closure_audit"),
        sa.CheckConstraint("(cancelled_at IS NULL AND cancelled_by IS NULL AND state<>'CANCELLED') OR "
            "(cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL AND state='CANCELLED')", name="ck_stocktake_cancel_audit"))
    op.create_index("uq_stocktake_active_warehouse", "stocktake", ["warehouse_id"], unique=True,
                    sqlite_where=sa.text("freeze_active=1"))
    op.create_table("stocktake_line", *foundation.audit(),
        sa.Column("stocktake_id", sa.Text(), fk("stocktake.id"), nullable=False),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("item_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("unit_id", sa.Text(), fk("unit.id"), nullable=False),
        sa.Column("warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("lot_id", sa.Text(), fk("inventory_lot.id")), sa.Column("book_qty", sa.Text(), nullable=False),
        sa.Column("counted_qty", sa.Text()), sa.Column("counted_at", sa.Text()),
        sa.Column("counted_by", sa.Text(), fk("actor.id")), sa.Column("note", sa.Text()),
        sa.UniqueConstraint("stocktake_id", "position_id", name="uq_stocktake_line_position"),
        exact("book_qty"), exact("counted_qty", nullable=True), utc("created_at"), utc("updated_at"), utc("counted_at", nullable=True),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0 AND item_kind IN ('RAW','FINISHED') "
            "AND (counted_qty IS NULL OR erp_decimal_cmp(counted_qty,'0')>=0)", name="ck_stocktake_count_nonnegative"),
        sa.CheckConstraint("(counted_qty IS NULL AND counted_at IS NULL AND counted_by IS NULL) OR "
            "(counted_qty IS NOT NULL AND counted_at IS NOT NULL AND counted_by IS NOT NULL)", name="ck_stocktake_count_audit"))
    op.create_table("stocktake_exception", *receiving.created(),
        sa.Column("stocktake_id", sa.Text(), fk("stocktake.id"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False), utc("created_at"),
        sa.CheckConstraint("trim(description)<>''", name="ck_stocktake_exception_description"))
    op.create_table("stocktake_posting", *receiving.created(),
        sa.Column("stocktake_id", sa.Text(), fk("stocktake.id"), nullable=False, unique=True),
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
            "AND confirmation_digest NOT GLOB '*[^0-9a-f]*' AND json_valid(confirmation_snapshot)", name="ck_stocktake_posting_confirmation"))
    op.create_table("stocktake_entry", *receiving.created(),
        sa.Column("posting_id", sa.Text(), fk("stocktake_posting.id"), nullable=False),
        sa.Column("stocktake_id", sa.Text(), fk("stocktake.id"), nullable=False),
        sa.Column("stocktake_line_id", sa.Text(), fk("stocktake_line.id"), nullable=False, unique=True),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False), sa.Column("qty_base_delta", sa.Text(), nullable=False),
        sa.UniqueConstraint("posting_id", "stocktake_line_id", name="uq_stocktake_posting_line"),
        exact("qty_base_delta"), utc("created_at"),
        sa.CheckConstraint("typeof(leg_no)='integer' AND leg_no=1 AND erp_decimal_cmp(qty_base_delta,'0')<>0", name="ck_stocktake_entry_nonzero"))
    for table, fields in {
        "stocktake": ["warehouse_id", "created_by", "started_by", "closed_by", "cancelled_by"],
        "stocktake_line": ["stocktake_id", "position_id", "item_id", "unit_id", "warehouse_id", "lot_id", "created_by", "counted_by"],
        "stocktake_exception": ["stocktake_id", "created_by"],
        "stocktake_posting": ["stocktake_id", "created_by", "posted_by"],
        "stocktake_entry": ["posting_id", "stocktake_id", "stocktake_line_id", "position_id", "created_by"],
    }.items():
        for field in fields:
            op.create_index("ix_" + table + "_" + field, table, [field])
    extend_ledger()
    for table in ["stocktake", "stocktake_line", "stocktake_exception", "stocktake_posting", "stocktake_entry"]:
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} prior WHERE {collisions(table)})
          BEGIN SELECT RAISE(ABORT,'stocktake existing identity cannot be replaced'); END""")
    header_guards()
    line_guards()
    freeze_barriers()
    posting_guards()
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Stocktake migration produced invalid foreign-key references")
    if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
        raise RuntimeError("Stocktake migration failed integrity checking")


def downgrade():
    raise RuntimeError("Stocktake downgrade disabled; preserve immutable history and verified backups.")
