"""Source-linked sales outflows in the existing exact physical-stock ledger."""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "0016_sales_shipments"
down_revision = "0015_sales_orders"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")
finished = import_module("backend.migrations.versions.0014_finished_goods_receipts")


def exact(column):
    check = foundation.decimal_check(column)
    check.name = "ck_sales_shipment_" + column
    return check


def utc(column):
    check = finished.utc(column)
    check.name = "ck_sales_shipment_utc_" + column
    return check


def source(alias="r", versions=True):
    version = (f"AND l.row_version={alias}.line_version AND o.row_version={alias}.order_version"
               if versions else "")
    return f"""EXISTS(SELECT 1 FROM sales_order_line l
      JOIN sales_order o ON o.id=l.sales_order_id JOIN customer c ON c.id=o.customer_id
      JOIN item i ON i.id=l.item_id JOIN unit u ON u.id=l.unit_id
      WHERE l.id={alias}.sales_order_line_id AND l.sales_order_id={alias}.sales_order_id
        AND o.customer_id={alias}.customer_id AND l.item_id={alias}.product_id
        AND l.unit_id={alias}.unit_id AND i.item_kind='FINISHED' AND i.base_unit_id=l.unit_id
        AND o.state='DRAFT' {version})"""


def position(alias="r"):
    return f"""EXISTS(SELECT 1 FROM stock_position s JOIN item i ON i.id=s.item_id
      JOIN warehouse w ON w.id=s.warehouse_id
      WHERE s.id={alias}.position_id AND s.item_id={alias}.product_id
        AND s.warehouse_id={alias}.warehouse_id AND s.lot_id IS {alias}.lot_id
        AND i.item_kind='FINISHED' AND i.base_unit_id={alias}.unit_id
        AND ((i.lot_tracking=0 AND s.lot_id IS NULL)
          OR (i.lot_tracking=1 AND EXISTS(SELECT 1 FROM inventory_lot lot
            WHERE lot.id=s.lot_id AND lot.item_id=i.id AND lot.origin_kind='MANUFACTURED'
              AND lot.supplier_id IS NULL AND lot.supplier_lot_code IS NULL))))"""


def shipped(line="l.id"):
    """Historical outflows remain genuine after safe source versions advance."""
    return f"""COALESCE((SELECT erp_decimal_sum(previous.quantity)
      FROM sales_shipment previous
      JOIN sales_shipment_posting posted ON posted.shipment_id=previous.id
      JOIN sales_shipment_entry effect ON effect.posting_id=posted.id AND effect.shipment_id=previous.id
      JOIN stock_position location ON location.id=effect.position_id
      WHERE previous.sales_order_line_id={line} AND posted.posting_state='POSTED'
        AND previous.row_version=posted.source_version+1 AND effect.leg_no=1
        AND effect.qty_base_delta=erp_decimal_neg(previous.quantity)
        AND erp_decimal_cmp(previous.quantity,'0')>0 AND effect.created_by=posted.created_by
        AND effect.position_id=previous.position_id AND location.item_id=previous.product_id
        AND location.warehouse_id=previous.warehouse_id AND location.lot_id IS previous.lot_id),'0')"""


def authorization(alias):
    return f"""COALESCE((erp_sales_shipment_authorized()=1
      AND {alias}.authorization_basis='LOCAL_DEMO_MANUAL'
      AND erp_sales_shipment_document() IS {alias}.shipment_id
      AND erp_sales_shipment_digest() IS {alias}.confirmation_digest
      AND erp_sales_shipment_snapshot() IS {alias}.confirmation_snapshot
      AND erp_sales_shipment_time() IS {alias}.confirmation_received_at),0)=1"""


def source_guard(document):
    return f"""
      SELECT RAISE(ABORT,'sales_shipment source or physical-position identity mismatch') WHERE NOT EXISTS(
        SELECT 1 FROM sales_shipment r WHERE r.id={document} AND ({source()}) AND ({position()}));
    """


def quantity_guard(document):
    return f"""
      SELECT RAISE(ABORT,'sales_shipment requires positive quantity') WHERE EXISTS(
        SELECT 1 FROM sales_shipment r WHERE r.id={document} AND erp_decimal_cmp(r.quantity,'0')<=0);
      SELECT RAISE(ABORT,'sales_shipment remaining ordered quantity exceeded') WHERE EXISTS(
        SELECT 1 FROM sales_shipment r JOIN sales_order_line l ON l.id=r.sales_order_line_id
        WHERE r.id={document} AND erp_decimal_cmp(r.quantity,erp_decimal_sub(l.ordered_qty,{shipped()}))>0);
      SELECT RAISE(ABORT,'sales_shipment physical stock insufficient') WHERE EXISTS(
        SELECT 1 FROM sales_shipment r WHERE r.id={document} AND erp_decimal_cmp(r.quantity,
          COALESCE((SELECT erp_decimal_sum(qty_base_delta) FROM physical_stock_ledger
            WHERE position_id=r.position_id),'0'))>0);
    """


def extend_ledger():
    connection = op.get_bind()
    original = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='physical_stock_ledger'").scalar_one()
    for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN", "FINISHED_GOODS_RECEIPT"]:
        if original.count("'" + domain + "'") != 1:
            raise RuntimeError("Unexpected original physical-ledger domain " + domain)
    if "'SALES_SHIPMENT'" in original:
        raise RuntimeError("Sales shipment already occurs in shared physical ledger")
    op.execute("DROP VIEW physical_stock_ledger")
    op.execute(original.rstrip().rstrip(";") + """
      UNION ALL SELECT e.id,e.posting_id,e.shipment_id,'SALES_SHIPMENT',e.position_id,e.qty_base_delta,NULL
      FROM sales_shipment_entry e JOIN sales_shipment_posting p ON p.id=e.posting_id
      JOIN sales_shipment r ON r.id=p.shipment_id
      WHERE p.posting_state='POSTED'
    """)


def source_protections():
    op.execute("""CREATE TRIGGER sales_shipment_source_line_delete BEFORE DELETE ON sales_order_line
      WHEN EXISTS(SELECT 1 FROM sales_shipment r JOIN sales_shipment_posting p ON p.shipment_id=r.id
        WHERE r.sales_order_line_id=OLD.id AND p.posting_state='POSTED')
      BEGIN SELECT RAISE(ABORT,'sales_shipment posted source line cannot be deleted'); END""")
    op.execute("""CREATE TRIGGER sales_shipment_source_order_delete BEFORE DELETE ON sales_order
      WHEN EXISTS(SELECT 1 FROM sales_shipment r JOIN sales_shipment_posting p ON p.shipment_id=r.id
        WHERE r.sales_order_id=OLD.id AND p.posting_state='POSTED')
      BEGIN SELECT RAISE(ABORT,'sales_shipment posted source order cannot be deleted'); END""")
    op.execute("""CREATE TRIGGER sales_shipment_protect_order BEFORE UPDATE OF id,customer_id ON sales_order
      WHEN (NEW.id<>OLD.id OR NEW.customer_id<>OLD.customer_id) AND EXISTS(
        SELECT 1 FROM sales_shipment r JOIN sales_shipment_posting p ON p.shipment_id=r.id
        WHERE r.sales_order_id=OLD.id AND p.posting_state='POSTED')
      BEGIN SELECT RAISE(ABORT,'sales_shipment posted customer and order identity protected'); END""")
    op.execute(f"""CREATE TRIGGER sales_shipment_protect_line BEFORE UPDATE ON sales_order_line
      WHEN EXISTS(SELECT 1 FROM sales_shipment r JOIN sales_shipment_posting p ON p.shipment_id=r.id
        WHERE r.sales_order_line_id=OLD.id AND p.posting_state='POSTED') BEGIN
      SELECT RAISE(ABORT,'sales_shipment posted line lineage protected') WHERE
        NEW.id<>OLD.id OR NEW.rowid<>OLD.rowid OR NEW.sales_order_id<>OLD.sales_order_id
        OR NEW.item_id<>OLD.item_id OR NEW.unit_id<>OLD.unit_id;
      SELECT RAISE(ABORT,'sales_shipment source ordered quantity below posted shipments') WHERE
        erp_decimal_cmp(NEW.ordered_qty,{shipped('OLD.id')})<0;
      END""")
    op.execute("""CREATE TRIGGER sales_shipment_line_no_replace BEFORE INSERT ON sales_order_line
      WHEN EXISTS(SELECT 1 FROM sales_order_line old JOIN sales_shipment r ON r.sales_order_line_id=old.id
        JOIN sales_shipment_posting p ON p.shipment_id=r.id WHERE p.posting_state='POSTED'
          AND (old.id=NEW.id OR old.rowid=NEW.rowid
            OR (old.sales_order_id=NEW.sales_order_id AND old.line_no=NEW.line_no)))
      BEGIN SELECT RAISE(ABORT,'sales_shipment posted source line cannot be replaced'); END""")
    op.execute("""CREATE TRIGGER sales_shipment_order_no_replace BEFORE INSERT ON sales_order
      WHEN EXISTS(SELECT 1 FROM sales_order old JOIN sales_shipment r ON r.sales_order_id=old.id
        JOIN sales_shipment_posting p ON p.shipment_id=r.id WHERE p.posting_state='POSTED'
          AND (old.id=NEW.id OR old.rowid=NEW.rowid OR old.document_no=NEW.document_no))
      BEGIN SELECT RAISE(ABORT,'sales_shipment posted source order cannot be replaced'); END""")


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Sales shipment migration requires foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing foreign-key violations prevent shipment migration")
    fk = foundation.fk
    op.create_table("sales_shipment", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("sales_order_line_id", sa.Text(), fk("sales_order_line.id"), nullable=False),
        sa.Column("line_version", sa.Integer(), nullable=False),
        sa.Column("order_version", sa.Integer(), nullable=False),
        sa.Column("sales_order_id", sa.Text(), fk("sales_order.id"), nullable=False),
        sa.Column("customer_id", sa.Text(), fk("customer.id"), nullable=False),
        sa.Column("product_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("unit_id", sa.Text(), fk("unit.id"), nullable=False),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("lot_id", sa.Text(), fk("inventory_lot.id")),
        sa.Column("business_date", sa.Text(), nullable=False), sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()), exact("quantity"), foundation.date_check("business_date"),
        sa.CheckConstraint("trim(document_no)<>'' AND substr(business_date,1,4)<>'0000' "
            "AND typeof(row_version)='integer' AND row_version>0 "
            "AND typeof(line_version)='integer' AND line_version>0 "
            "AND typeof(order_version)='integer' AND order_version>0"), utc("created_at"), utc("updated_at"))
    op.create_table("sales_shipment_posting", *receiving.created(),
        sa.Column("shipment_id", sa.Text(), fk("sales_shipment.id"), nullable=False, unique=True),
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
    op.create_table("sales_shipment_entry", *receiving.created(),
        sa.Column("posting_id", sa.Text(), fk("sales_shipment_posting.id"), nullable=False),
        sa.Column("shipment_id", sa.Text(), fk("sales_shipment.id"), nullable=False, unique=True),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False), sa.Column("qty_base_delta", sa.Text(), nullable=False),
        sa.UniqueConstraint("posting_id", "leg_no"), exact("qty_base_delta"),
        sa.CheckConstraint("typeof(leg_no)='integer' AND leg_no=1 AND substr(qty_base_delta,1,1)='-' "
            "AND qty_base_delta<>'0'"), utc("created_at"))
    for table, fields in {
        "sales_shipment": ["sales_order_line_id", "sales_order_id", "customer_id", "product_id", "unit_id",
                           "position_id", "warehouse_id", "lot_id", "created_by"],
        "sales_shipment_posting": ["shipment_id", "created_by", "posted_by"],
        "sales_shipment_entry": ["posting_id", "shipment_id", "position_id", "created_by"],
    }.items():
        for field in fields:
            op.create_index("ix_" + table + "_" + field, table, [field])
    extend_ledger()
    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER sales_shipment_source_{action.lower()} BEFORE {action} ON sales_shipment
          WHEN NOT (({source('NEW')}) AND ({position('NEW')}))
          BEGIN SELECT RAISE(ABORT,'sales_shipment source or existing position mismatch'); END""")
    op.execute("""CREATE TRIGGER sales_shipment_update_audit BEFORE UPDATE ON sales_shipment BEGIN
      SELECT RAISE(ABORT,'sales_shipment creation identity immutable') WHERE NEW.id<>OLD.id OR NEW.rowid<>OLD.rowid
        OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by;
      SELECT RAISE(ABORT,'sales_shipment version must advance once') WHERE NEW.row_version<>OLD.row_version+1;
      SELECT RAISE(ABORT,'sales_shipment cannot replace another shipment') WHERE EXISTS(
        SELECT 1 FROM sales_shipment WHERE id<>OLD.id AND (document_no=NEW.document_no OR rowid=NEW.rowid));
      END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER sales_shipment_posted_{action.lower()} BEFORE {action} ON sales_shipment
          WHEN EXISTS(SELECT 1 FROM sales_shipment_posting WHERE shipment_id=OLD.id)
          BEGIN SELECT RAISE(ABORT,'sales_shipment posted history immutable'); END""")
        op.execute(f"""CREATE TRIGGER sales_shipment_entry_immutable_{action.lower()} BEFORE {action} ON sales_shipment_entry
          BEGIN SELECT RAISE(ABORT,'sales_shipment movement immutable'); END""")
    for table, extra in {
        "sales_shipment": "OR document_no=NEW.document_no",
        "sales_shipment_posting": "OR shipment_id=NEW.shipment_id OR idempotency_key=NEW.idempotency_key",
        "sales_shipment_entry": "OR shipment_id=NEW.shipment_id OR (posting_id=NEW.posting_id AND leg_no=NEW.leg_no)",
    }.items():
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id OR rowid=NEW.rowid {extra})
          BEGIN SELECT RAISE(ABORT,'sales_shipment existing identity cannot be replaced'); END""")
    source_protections()
    op.execute("""CREATE TRIGGER sales_shipment_posting_delete BEFORE DELETE ON sales_shipment_posting
      BEGIN SELECT RAISE(ABORT,'sales_shipment posting immutable'); END""")
    for other in ["stock_posting", "supplier_return_posting", "material_issue_posting", "material_return_posting", "finished_goods_posting"]:
        op.execute(f"""CREATE TRIGGER sales_shipment_key_{other} BEFORE INSERT ON sales_shipment_posting
          WHEN EXISTS(SELECT 1 FROM {other} WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'sales_shipment posting key belongs to another domain'); END""")
        op.execute(f"""CREATE TRIGGER {other}_sales_shipment_key BEFORE INSERT ON {other}
          WHEN EXISTS(SELECT 1 FROM sales_shipment_posting WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'posting key belongs to sales_shipment'); END""")
    op.execute(f"""CREATE TRIGGER sales_shipment_posting_prepare BEFORE INSERT ON sales_shipment_posting BEGIN
      SELECT RAISE(ABORT,'sales_shipment posting authorization denied') WHERE NOT ({authorization('NEW')});
      SELECT RAISE(ABORT,'sales_shipment posting must prepare') WHERE NEW.posting_state<>'PREPARING';
      SELECT RAISE(ABORT,'sales_shipment posting audit mismatch') WHERE
        NEW.posted_at IS NOT NEW.confirmation_received_at OR NEW.posted_by<>NEW.created_by;
      SELECT RAISE(ABORT,'sales_shipment stale document version') WHERE NOT EXISTS(
        SELECT 1 FROM sales_shipment r WHERE r.id=NEW.shipment_id AND r.row_version=NEW.source_version+1);
      {source_guard('NEW.shipment_id')}{quantity_guard('NEW.shipment_id')}
      END""")
    op.execute(f"""CREATE TRIGGER sales_shipment_entry_outbound BEFORE INSERT ON sales_shipment_entry BEGIN
      SELECT RAISE(ABORT,'sales_shipment invalid authorized outbound movement') WHERE NOT EXISTS(
        SELECT 1 FROM sales_shipment_posting p JOIN sales_shipment r ON r.id=p.shipment_id
        WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING' AND ({authorization('p')})
          AND r.id=NEW.shipment_id AND r.row_version=p.source_version+1 AND ({source()}) AND ({position()})
          AND NEW.position_id=r.position_id AND NEW.leg_no=1
          AND NEW.qty_base_delta=erp_decimal_neg(r.quantity) AND erp_decimal_cmp(r.quantity,'0')>0
          AND NEW.created_by=p.created_by);
      SELECT RAISE(ABORT,'sales_shipment preparing movements exceed ordered quantity') WHERE EXISTS(
        SELECT 1 FROM sales_shipment r JOIN sales_order_line l ON l.id=r.sales_order_line_id
        WHERE r.id=NEW.shipment_id AND erp_decimal_cmp(erp_decimal_sub(l.ordered_qty,{shipped()}),
          (SELECT erp_decimal_sum(value) FROM (
            SELECT erp_decimal_neg(NEW.qty_base_delta) AS value UNION ALL
            SELECT erp_decimal_neg(e.qty_base_delta) FROM sales_shipment_entry e
            JOIN sales_shipment_posting pending ON pending.id=e.posting_id
            JOIN sales_shipment other ON other.id=e.shipment_id
            WHERE pending.posting_state='PREPARING' AND other.sales_order_line_id=l.id)))<0);
      SELECT RAISE(ABORT,'sales_shipment preparing movements exhaust physical stock') WHERE
        erp_decimal_cmp((SELECT erp_decimal_sum(value) FROM (
          SELECT qty_base_delta AS value FROM physical_stock_ledger WHERE position_id=NEW.position_id
          UNION ALL SELECT NEW.qty_base_delta UNION ALL
          SELECT e.qty_base_delta FROM sales_shipment_entry e
          JOIN sales_shipment_posting pending ON pending.id=e.posting_id
          WHERE pending.posting_state='PREPARING' AND e.position_id=NEW.position_id)),'0')<0;
      END""")
    protected = ["id", "rowid", "shipment_id", "idempotency_key", "request_fingerprint", "source_version",
                 "authorization_basis", "confirmation_digest", "confirmation_snapshot", "confirmation_received_at",
                 "posted_at", "posted_by", "created_at", "created_by"]
    unchanged = " AND ".join("NEW." + field + " IS OLD." + field for field in protected)
    op.execute(f"""CREATE TRIGGER sales_shipment_posting_finalize BEFORE UPDATE ON sales_shipment_posting BEGIN
      SELECT RAISE(ABORT,'sales_shipment posting authorization denied') WHERE NOT ({authorization('OLD')});
      SELECT RAISE(ABORT,'sales_shipment posting immutable') WHERE NOT (
        OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {unchanged});
      SELECT RAISE(ABORT,'sales_shipment document version changed') WHERE NOT EXISTS(
        SELECT 1 FROM sales_shipment r WHERE r.id=OLD.shipment_id AND r.row_version=OLD.source_version+1);
      SELECT RAISE(ABORT,'sales_shipment requires exactly one genuine negative movement') WHERE
        (SELECT count(*) FROM sales_shipment_entry WHERE posting_id=OLD.id)<>1 OR NOT EXISTS(
          SELECT 1 FROM sales_shipment_entry e JOIN sales_shipment r ON r.id=e.shipment_id
          WHERE e.posting_id=OLD.id AND e.shipment_id=OLD.shipment_id AND e.leg_no=1
            AND e.position_id=r.position_id AND e.qty_base_delta=erp_decimal_neg(r.quantity)
            AND erp_decimal_cmp(e.qty_base_delta,'0')<0 AND e.created_by=OLD.created_by AND ({position()}));
      {source_guard('OLD.shipment_id')}{quantity_guard('OLD.shipment_id')}
      END""")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Sales shipment migration produced invalid foreign-key references")
    if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
        raise RuntimeError("Sales shipment migration failed integrity checking")


def downgrade():
    raise RuntimeError("Sales shipment downgrade disabled; preserve immutable history and verified backups.")
