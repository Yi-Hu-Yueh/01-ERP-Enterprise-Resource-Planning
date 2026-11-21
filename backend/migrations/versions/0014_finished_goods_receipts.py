"""Source-linked finished-goods receipts in the one existing physical ledger."""
from importlib import import_module
import re

from alembic import op
import sqlalchemy as sa

revision = "0014_finished_goods_receipts"
down_revision = "0013_production_completions"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")
return_migration = import_module("backend.migrations.versions.0012_material_returns")
rebuild = import_module("backend.migrations.versions.0011_local_demo_issue_confirmation")


def exact(column):
    check = foundation.decimal_check(column)
    check.name = "ck_finished_goods_" + column
    return check


def utc(column):
    check = return_migration.utc(column)
    check.name = "ck_finished_goods_utc_" + column
    return check


def source(alias="r"):
    """Identity is derived from a genuine immutable completion, never client data."""
    return f"""EXISTS(SELECT 1 FROM production_completion c
      JOIN work_order w ON w.id=c.work_order_id JOIN item i ON i.id=c.product_id
      WHERE c.id={alias}.completion_id AND c.state='CONFIRMED'
        AND c.row_version={alias}.completion_version AND c.product_id=w.product_id
        AND i.item_kind='FINISHED' AND i.base_unit_id=c.unit_id
        AND c.confirmation_id IS NOT NULL AND c.confirmed_at IS NOT NULL
        AND c.confirmed_by IS NOT NULL AND c.authorization_basis='LOCAL_DEMO_MANUAL')"""


def lot_matches(receipt="r", completion="c", item="i"):
    return f"""(({item}.lot_tracking=0 AND {receipt}.lot_id IS NULL AND {receipt}.lot_code IS NULL)
      OR ({item}.lot_tracking=1 AND {receipt}.lot_code IS NOT NULL
        AND trim({receipt}.lot_code)<>'' AND {receipt}.lot_id IS NOT NULL
        AND EXISTS(SELECT 1 FROM inventory_lot lot WHERE lot.id={receipt}.lot_id
          AND lot.item_id={completion}.product_id AND lot.lot_code={receipt}.lot_code
          AND lot.origin_kind='MANUFACTURED' AND lot.supplier_id IS NULL
          AND lot.supplier_lot_code IS NULL)))"""


def received(completion="c.id"):
    """Count only genuine complete, matching positive posted movements once."""
    return f"""COALESCE((SELECT erp_decimal_sum(previous.quantity)
      FROM finished_goods_receipt previous
      JOIN finished_goods_posting posted ON posted.receipt_id=previous.id
      JOIN finished_goods_entry effect ON effect.posting_id=posted.id AND effect.receipt_id=previous.id
      JOIN production_completion origin ON origin.id=previous.completion_id
      JOIN stock_position position ON position.id=effect.position_id
      WHERE previous.completion_id={completion} AND posted.posting_state='POSTED'
        AND origin.state='CONFIRMED' AND previous.completion_version=origin.row_version
        AND previous.row_version=posted.source_version+1
        AND effect.leg_no=1 AND effect.qty_base_delta=previous.quantity
        AND erp_decimal_cmp(effect.qty_base_delta,'0')>0 AND effect.created_by=posted.created_by
        AND position.item_id=origin.product_id AND position.warehouse_id=previous.warehouse_id
        AND position.lot_id IS previous.lot_id),'0')"""


def authorization(alias):
    return f"""COALESCE((erp_finished_goods_authorized()=1
      AND {alias}.authorization_basis='LOCAL_DEMO_MANUAL'
      AND erp_finished_goods_document() IS {alias}.receipt_id
      AND erp_finished_goods_digest() IS {alias}.confirmation_digest
      AND erp_finished_goods_snapshot() IS {alias}.confirmation_snapshot
      AND erp_finished_goods_time() IS {alias}.confirmation_received_at),0)=1"""


def draft_context(alias="r"):
    return f"""COALESCE(erp_finished_goods_authorized(),0)=1
      AND erp_finished_goods_document() IS {alias}.id
      AND erp_finished_goods_digest() IS NOT NULL
      AND erp_finished_goods_snapshot() IS NOT NULL
      AND erp_finished_goods_time() IS NOT NULL AND ({source(alias)})
      AND erp_decimal_cmp({alias}.quantity,'0')>0"""


def quantity_guard(document):
    return f"""
      SELECT RAISE(ABORT,'finished_goods receipt must be a positive actual quantity') WHERE EXISTS(
        SELECT 1 FROM finished_goods_receipt r WHERE r.id={document} AND erp_decimal_cmp(r.quantity,'0')<=0);
      SELECT RAISE(ABORT,'finished_goods confirmed good quantity exceeded') WHERE EXISTS(
        SELECT 1 FROM finished_goods_receipt r JOIN production_completion c ON c.id=r.completion_id
        WHERE r.id={document} AND erp_decimal_cmp(r.quantity,erp_decimal_sub(c.good_qty_base,{received()}))>0);
    """


def posting_source_guard(document):
    return f"""
      SELECT RAISE(ABORT,'finished_goods confirmed source or inventory lot mismatch') WHERE NOT EXISTS(
        SELECT 1 FROM finished_goods_receipt r JOIN production_completion c ON c.id=r.completion_id
        JOIN item i ON i.id=c.product_id JOIN warehouse w ON w.id=r.warehouse_id
        WHERE r.id={document} AND ({source()}) AND ({lot_matches()}));
    """


def revise_shared_trigger(name, original):
    if name == "inventory_lot_raw":
        return f"""CREATE TRIGGER inventory_lot_raw BEFORE INSERT ON inventory_lot
          WHEN NOT ((NEW.origin_kind='PURCHASED' AND EXISTS(
            SELECT 1 FROM item WHERE id=NEW.item_id AND item_kind='RAW' AND lot_tracking=1))
            OR (NEW.origin_kind='MANUFACTURED' AND NEW.supplier_id IS NULL
              AND NEW.supplier_lot_code IS NULL
              AND EXISTS(SELECT 1 FROM finished_goods_receipt r
                JOIN production_completion c ON c.id=r.completion_id JOIN item i ON i.id=c.product_id
                WHERE ({draft_context()}) AND i.lot_tracking=1
                  AND NEW.item_id=c.product_id AND NEW.lot_code=r.lot_code)))
          BEGIN SELECT RAISE(ABORT,'stock_lot requires tracked RAW or authorized manufactured FINISHED item'); END"""
    if name == "stock_position_identity":
        return f"""CREATE TRIGGER stock_position_identity BEFORE INSERT ON stock_position
          WHEN NOT (EXISTS(SELECT 1 FROM item i WHERE i.id=NEW.item_id AND i.item_kind='RAW'
            AND ((i.lot_tracking=0 AND NEW.lot_id IS NULL) OR (i.lot_tracking=1 AND EXISTS(
              SELECT 1 FROM inventory_lot l WHERE l.id=NEW.lot_id AND l.item_id=NEW.item_id))))
            OR EXISTS(SELECT 1 FROM finished_goods_receipt r
              JOIN production_completion c ON c.id=r.completion_id JOIN item i ON i.id=c.product_id
              WHERE ({draft_context()}) AND NEW.item_id=c.product_id
                AND NEW.warehouse_id=r.warehouse_id AND NEW.lot_id IS r.lot_id AND ({lot_matches()})))
          BEGIN SELECT RAISE(ABORT,'stock_position item lot mismatch or finished-goods grant denied'); END"""
    return original


def extend_lot_infrastructure():
    """Reuse0011's FK-ON atomic parent replacement, retaining every old rowid."""
    connection = op.get_bind()
    table, temporary = "inventory_lot", "_inventory_lot_0014"
    original = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).scalar_one()
    columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(inventory_lot)")]
    quoted = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    old_rows = connection.exec_driver_sql("SELECT rowid," + quoted + " FROM inventory_lot ORDER BY rowid").all()
    revised, count = re.subn(r"(?m)^(\s*supplier_id\s+TEXT)\s+NOT NULL(?=,)", r"\1", original)
    if count != 1:
        raise RuntimeError("Expected one original mandatory purchased-lot supplier column")
    revised = rebuild.replace_once(revised, "origin_kind='PURCHASED'", """((origin_kind='PURCHASED' AND supplier_id IS NOT NULL)
      OR (origin_kind='MANUFACTURED' AND supplier_id IS NULL AND supplier_lot_code IS NULL))""")
    revised, count = re.subn(r'^(CREATE TABLE\s+)(?:"inventory_lot"|inventory_lot)',
                             lambda match: match[1] + '"' + temporary + '"', revised)
    if count != 1:
        raise RuntimeError("Expected original inventory-lot table definition")
    objects = connection.exec_driver_sql(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE type IN ('view','trigger','index') AND sql IS NOT NULL ORDER BY type,name").all()
    # The0011 helper uses a module-local root table. Keep its algorithm and scope
    # this invocation explicitly to inventory_lot without changing that module.
    references, selected = {table}, {}
    while True:
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in references) + r")\b")
        additions = [obj for obj in objects if obj[0] in {"view", "trigger"}
                     and obj[1] not in selected and pattern.search(obj[3])]
        if not additions:
            break
        for obj in additions:
            selected[obj[1]] = obj
            if obj[0] == "view":
                references.add(obj[1])
    dependent = list(selected.values())
    if not {"inventory_lot_raw", "stock_position_identity", "inventory_lot_immutable_update",
            "inventory_lot_immutable_delete"}.issubset(selected):
        raise RuntimeError("Required original lot/position integrity guards missing")
    indexes = [obj for obj in objects if obj[0] == "index" and obj[2] == table]
    connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    for kind, name, _, sql in sorted(dependent, key=lambda obj: obj[0] == "view"):
        op.execute("DROP " + kind + ' "' + name.replace('"', '""') + '"')
    op.execute(revised)
    op.execute('INSERT INTO "' + temporary + '" (rowid,' + quoted + ') SELECT rowid,' + quoted + ' FROM inventory_lot')
    op.execute("DROP TABLE inventory_lot")
    op.execute('ALTER TABLE "' + temporary + '" RENAME TO inventory_lot')
    for _, _, _, sql in indexes:
        op.execute(sql)
    for kind, name, _, sql in sorted(dependent, key=lambda obj: obj[0] != "view"):
        op.execute(revise_shared_trigger(name, sql) if kind == "trigger" else sql)
    if connection.exec_driver_sql("SELECT rowid," + quoted + " FROM inventory_lot ORDER BY rowid").all() != old_rows:
        raise RuntimeError("Existing inventory-lot rows or metadata changed during migration")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Finished-goods lot migration produced foreign-key violations")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Finished-goods migration lost foreign-key enforcement")
    # Match0011: clear only the stale deferred DROP counter after every physical
    # parent identity and dependent guard has been restored and checked.
    connection.exec_driver_sql("PRAGMA defer_foreign_keys=OFF")


def extend_ledger():
    connection = op.get_bind()
    original = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='physical_stock_ledger'").scalar_one()
    for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN"]:
        if original.count("'" + domain + "'") != 1:
            raise RuntimeError("Unexpected original stock-ledger domain " + domain)
    if "FINISHED_GOODS_RECEIPT" in original:
        raise RuntimeError("Finished-goods receipts already occur in the shared ledger")
    op.execute("DROP VIEW physical_stock_ledger")
    op.execute(original.rstrip().rstrip(";") + """
      UNION ALL SELECT e.id,e.posting_id,e.receipt_id,'FINISHED_GOODS_RECEIPT',e.position_id,e.qty_base_delta,c.work_order_id
      FROM finished_goods_entry e JOIN finished_goods_posting p ON p.id=e.posting_id
      JOIN finished_goods_receipt r ON r.id=p.receipt_id JOIN production_completion c ON c.id=r.completion_id
      WHERE p.posting_state='POSTED'
    """)


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Finished-goods migration requires foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing foreign-key violations prevent finished-goods migration")
    fk = foundation.fk
    op.create_table("finished_goods_receipt", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("completion_id", sa.Text(), fk("production_completion.id"), nullable=False),
        sa.Column("completion_version", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("lot_id", sa.Text(), fk("inventory_lot.id")), sa.Column("lot_code", sa.Text()),
        sa.Column("business_date", sa.Text(), nullable=False), sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()), exact("quantity"), foundation.date_check("business_date"),
        sa.CheckConstraint("trim(document_no)<>'' AND substr(business_date,1,4)<>'0000' "
            "AND typeof(row_version)='integer' AND row_version>0 "
            "AND typeof(completion_version)='integer' AND completion_version>0 "
            "AND (lot_code IS NULL OR trim(lot_code)<>'')"), utc("created_at"), utc("updated_at"))
    op.create_table("finished_goods_posting", *receiving.created(),
        sa.Column("receipt_id", sa.Text(), fk("finished_goods_receipt.id"), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("posting_state", sa.Text(), nullable=False),
        sa.Column("authorization_basis", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("confirmation_snapshot", sa.Text(), nullable=False),
        sa.Column("confirmation_received_at", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.Text(), nullable=False), sa.Column("posted_by", sa.Text(), fk("actor.id"), nullable=False),
        sa.CheckConstraint("posting_state IN ('PREPARING','POSTED') AND authorization_basis='LOCAL_DEMO_MANUAL' "
            "AND typeof(source_version)='integer' AND source_version>0 AND trim(request_fingerprint)<>'' "
            "AND trim(confirmation_digest)<>'' AND json_valid(confirmation_snapshot)"),
        utc("created_at"), utc("confirmation_received_at"), utc("posted_at"))
    op.create_table("finished_goods_entry", *receiving.created(),
        sa.Column("posting_id", sa.Text(), fk("finished_goods_posting.id"), nullable=False),
        sa.Column("receipt_id", sa.Text(), fk("finished_goods_receipt.id"), nullable=False, unique=True),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False), sa.Column("qty_base_delta", sa.Text(), nullable=False),
        exact("qty_base_delta"), sa.CheckConstraint("typeof(leg_no)='integer' AND leg_no=1 "
            "AND substr(qty_base_delta,1,1)<>'-' AND qty_base_delta<>'0'"), utc("created_at"))
    for table, fields in {
        "finished_goods_receipt": ["completion_id", "warehouse_id", "lot_id", "created_by"],
        "finished_goods_posting": ["receipt_id", "created_by", "posted_by"],
        "finished_goods_entry": ["posting_id", "receipt_id", "position_id", "created_by"],
    }.items():
        for field in fields:
            op.create_index("ix_" + table + "_" + field, table, [field])
    # New tables exist before restored lot/position triggers refer to them.
    extend_lot_infrastructure()
    extend_ledger()
    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER finished_goods_receipt_source_{action.lower()} BEFORE {action} ON finished_goods_receipt BEGIN
          SELECT RAISE(ABORT,'finished_goods receipt requires matching confirmed production') WHERE NOT ({source('NEW')});
          SELECT RAISE(ABORT,'finished_goods receipt untracked product cannot acquire a fictitious lot') WHERE EXISTS(
            SELECT 1 FROM production_completion c JOIN item i ON i.id=c.product_id
            WHERE c.id=NEW.completion_id AND i.lot_tracking=0 AND (NEW.lot_id IS NOT NULL OR NEW.lot_code IS NOT NULL));
          SELECT RAISE(ABORT,'finished_goods receipt existing lot mismatch') WHERE NEW.lot_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM production_completion c JOIN item i ON i.id=c.product_id
            JOIN inventory_lot lot ON lot.id=NEW.lot_id WHERE c.id=NEW.completion_id AND i.lot_tracking=1
              AND lot.item_id=c.product_id AND lot.lot_code=NEW.lot_code AND lot.origin_kind='MANUFACTURED'
              AND lot.supplier_id IS NULL AND lot.supplier_lot_code IS NULL);
          END""")
    op.execute("""CREATE TRIGGER finished_goods_receipt_update_audit BEFORE UPDATE ON finished_goods_receipt BEGIN
      SELECT RAISE(ABORT,'finished_goods receipt creation identity immutable') WHERE NEW.id<>OLD.id OR NEW.rowid<>OLD.rowid
        OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by;
      SELECT RAISE(ABORT,'finished_goods receipt version must advance once') WHERE NEW.row_version<>OLD.row_version+1;
      SELECT RAISE(ABORT,'finished_goods receipt cannot replace another receipt') WHERE EXISTS(
        SELECT 1 FROM finished_goods_receipt WHERE id<>OLD.id AND (document_no=NEW.document_no OR rowid=NEW.rowid));
      END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER finished_goods_receipt_posted_{action.lower()} BEFORE {action} ON finished_goods_receipt
          WHEN EXISTS(SELECT 1 FROM finished_goods_posting WHERE receipt_id=OLD.id)
          BEGIN SELECT RAISE(ABORT,'finished_goods receipt posted history immutable'); END""")
        op.execute(f"""CREATE TRIGGER finished_goods_entry_immutable_{action.lower()} BEFORE {action} ON finished_goods_entry
          BEGIN SELECT RAISE(ABORT,'finished_goods movement immutable'); END""")
    for table, extra in {
        "finished_goods_receipt": "OR document_no=NEW.document_no",
        "finished_goods_posting": "OR receipt_id=NEW.receipt_id OR idempotency_key=NEW.idempotency_key",
        "finished_goods_entry": "OR receipt_id=NEW.receipt_id OR (posting_id=NEW.posting_id AND leg_no=NEW.leg_no)",
    }.items():
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id OR rowid=NEW.rowid {extra})
          BEGIN SELECT RAISE(ABORT,'finished_goods existing identity cannot be replaced'); END""")
    op.execute("""CREATE TRIGGER finished_goods_lot_no_replace BEFORE INSERT ON inventory_lot
      WHEN EXISTS(SELECT 1 FROM inventory_lot old JOIN item i ON i.id=old.item_id
        WHERE (i.item_kind='FINISHED' OR EXISTS(SELECT 1 FROM item n WHERE n.id=NEW.item_id AND n.item_kind='FINISHED'))
          AND (old.id=NEW.id OR old.rowid=NEW.rowid OR (old.item_id=NEW.item_id AND old.lot_code=NEW.lot_code)))
      BEGIN SELECT RAISE(ABORT,'finished_goods lot history cannot be replaced'); END""")
    op.execute("""CREATE TRIGGER finished_goods_position_no_replace BEFORE INSERT ON stock_position
      WHEN EXISTS(SELECT 1 FROM stock_position old JOIN item i ON i.id=old.item_id
        WHERE (i.item_kind='FINISHED' OR EXISTS(SELECT 1 FROM item n WHERE n.id=NEW.item_id AND n.item_kind='FINISHED'))
          AND (old.id=NEW.id OR old.rowid=NEW.rowid OR (old.item_id=NEW.item_id
            AND old.warehouse_id=NEW.warehouse_id AND old.lot_id IS NEW.lot_id)))
      BEGIN SELECT RAISE(ABORT,'finished_goods stock-position history cannot be replaced'); END""")
    op.execute("""CREATE TRIGGER finished_goods_item_basis BEFORE UPDATE OF id,code,item_kind,base_unit_id,lot_tracking ON item
      WHEN (NEW.id<>OLD.id OR NEW.code<>OLD.code OR NEW.item_kind<>OLD.item_kind
        OR NEW.base_unit_id<>OLD.base_unit_id OR NEW.lot_tracking<>OLD.lot_tracking)
        AND EXISTS(SELECT 1 FROM finished_goods_receipt r JOIN production_completion c ON c.id=r.completion_id WHERE c.product_id=OLD.id)
      BEGIN SELECT RAISE(ABORT,'finished_goods receipt product identity and tracking basis protected'); END""")
    op.execute("""CREATE TRIGGER finished_goods_posting_delete BEFORE DELETE ON finished_goods_posting
      BEGIN SELECT RAISE(ABORT,'finished_goods posting immutable'); END""")
    for other in ["stock_posting", "supplier_return_posting", "material_issue_posting", "material_return_posting"]:
        op.execute(f"""CREATE TRIGGER finished_goods_key_{other} BEFORE INSERT ON finished_goods_posting
          WHEN EXISTS(SELECT 1 FROM {other} WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'finished_goods posting key belongs to another domain'); END""")
        op.execute(f"""CREATE TRIGGER {other}_finished_goods_key BEFORE INSERT ON {other}
          WHEN EXISTS(SELECT 1 FROM finished_goods_posting WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'posting key belongs to finished_goods'); END""")
    op.execute(f"""CREATE TRIGGER finished_goods_posting_prepare BEFORE INSERT ON finished_goods_posting BEGIN
      SELECT RAISE(ABORT,'finished_goods posting authorization denied') WHERE NOT ({authorization('NEW')});
      SELECT RAISE(ABORT,'finished_goods posting must prepare') WHERE NEW.posting_state<>'PREPARING';
      SELECT RAISE(ABORT,'finished_goods stale receipt version') WHERE NOT EXISTS(
        SELECT 1 FROM finished_goods_receipt r WHERE r.id=NEW.receipt_id AND r.row_version=NEW.source_version+1);
      {posting_source_guard('NEW.receipt_id')}{quantity_guard('NEW.receipt_id')}
      END""")
    op.execute(f"""CREATE TRIGGER finished_goods_entry_inbound BEFORE INSERT ON finished_goods_entry BEGIN
      SELECT RAISE(ABORT,'finished_goods invalid authorized inbound movement') WHERE NOT EXISTS(
        SELECT 1 FROM finished_goods_posting p JOIN finished_goods_receipt r ON r.id=p.receipt_id
        JOIN production_completion c ON c.id=r.completion_id JOIN item i ON i.id=c.product_id
        JOIN stock_position position ON position.id=NEW.position_id
        WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING' AND ({authorization('p')})
          AND r.id=NEW.receipt_id AND r.row_version=p.source_version+1 AND ({source()}) AND ({lot_matches()})
          AND NEW.leg_no=1 AND NEW.qty_base_delta=r.quantity AND erp_decimal_cmp(NEW.qty_base_delta,'0')>0
          AND NEW.created_by=p.created_by AND position.item_id=c.product_id
          AND position.warehouse_id=r.warehouse_id AND position.lot_id IS r.lot_id);
      SELECT RAISE(ABORT,'finished_goods preparing movements exceed confirmed good') WHERE EXISTS(
        SELECT 1 FROM finished_goods_receipt r JOIN production_completion c ON c.id=r.completion_id
        WHERE r.id=NEW.receipt_id AND erp_decimal_cmp(erp_decimal_sub(c.good_qty_base,{received()}),
          (SELECT erp_decimal_sum(value) FROM (
            SELECT NEW.qty_base_delta AS value UNION ALL SELECT e.qty_base_delta
            FROM finished_goods_entry e JOIN finished_goods_posting pending ON pending.id=e.posting_id
            JOIN finished_goods_receipt other ON other.id=e.receipt_id
            WHERE pending.posting_state='PREPARING' AND other.completion_id=c.id)))<0);
      END""")
    protected = ["id", "rowid", "receipt_id", "idempotency_key", "request_fingerprint", "source_version",
                 "authorization_basis", "confirmation_digest", "confirmation_snapshot", "confirmation_received_at",
                 "posted_at", "posted_by", "created_at", "created_by"]
    unchanged = " AND ".join("NEW." + field + " IS OLD." + field for field in protected)
    op.execute(f"""CREATE TRIGGER finished_goods_posting_finalize BEFORE UPDATE ON finished_goods_posting BEGIN
      SELECT RAISE(ABORT,'finished_goods posting authorization denied') WHERE NOT ({authorization('OLD')});
      SELECT RAISE(ABORT,'finished_goods posting immutable') WHERE NOT (
        OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {unchanged});
      SELECT RAISE(ABORT,'finished_goods receipt version changed') WHERE NOT EXISTS(
        SELECT 1 FROM finished_goods_receipt r WHERE r.id=OLD.receipt_id AND r.row_version=OLD.source_version+1);
      SELECT RAISE(ABORT,'finished_goods posting requires exactly one actual movement') WHERE
        (SELECT count(*) FROM finished_goods_entry WHERE posting_id=OLD.id)<>1 OR NOT EXISTS(
          SELECT 1 FROM finished_goods_entry e JOIN finished_goods_receipt r ON r.id=e.receipt_id
          JOIN production_completion c ON c.id=r.completion_id JOIN stock_position s ON s.id=e.position_id
          WHERE e.posting_id=OLD.id AND e.receipt_id=OLD.receipt_id AND e.leg_no=1
            AND e.qty_base_delta=r.quantity AND erp_decimal_cmp(e.qty_base_delta,'0')>0
            AND e.created_by=OLD.created_by AND s.item_id=c.product_id
            AND s.warehouse_id=r.warehouse_id AND s.lot_id IS r.lot_id);
      {posting_source_guard('OLD.receipt_id')}{quantity_guard('OLD.receipt_id')}
      END""")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Finished-goods migration produced invalid foreign-key references")
    if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
        raise RuntimeError("Finished-goods migration failed SQLite integrity checking")


def downgrade():
    raise RuntimeError("Finished-goods downgrade disabled; preserve immutable history and verified backup recovery.")
