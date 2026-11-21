"""Shipment-linked returns; only explicitly restocked returns enter physical stock."""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "0017_customer_returns"
down_revision = "0016_sales_shipments"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")
finished = import_module("backend.migrations.versions.0014_finished_goods_receipts")
shipment = import_module("backend.migrations.versions.0016_sales_shipments")


def exact(column):
    check = foundation.decimal_check(column)
    check.name = "ck_customer_return_" + column
    return check


def utc(column):
    check = finished.utc(column)
    check.name = "ck_customer_return_utc_" + column
    return check


def source(alias="r"):
    """A real immutable negative movement, not a draft or a mutable order version."""
    return f"""EXISTS(SELECT 1 FROM sales_shipment s
      JOIN sales_shipment_posting sp ON sp.shipment_id=s.id
      JOIN sales_shipment_entry se ON se.posting_id=sp.id AND se.shipment_id=s.id
      JOIN sales_order_line l ON l.id=s.sales_order_line_id
      JOIN sales_order o ON o.id=l.sales_order_id
      JOIN customer c ON c.id=o.customer_id
      JOIN item i ON i.id=s.product_id JOIN unit u ON u.id=s.unit_id
      WHERE s.id={alias}.shipment_id AND sp.posting_state='POSTED'
        AND s.row_version=sp.source_version+1 AND s.row_version={alias}.shipment_version
        AND erp_decimal_cmp(s.quantity,'0')>0 AND se.leg_no=1
        AND se.qty_base_delta=erp_decimal_neg(s.quantity) AND se.created_by=sp.created_by
        AND se.position_id=s.position_id AND ({shipment.position('s')})
        AND l.sales_order_id=s.sales_order_id AND o.customer_id=s.customer_id
        AND l.item_id=s.product_id AND l.unit_id=s.unit_id
        AND i.item_kind='FINISHED' AND i.base_unit_id=s.unit_id
        AND {alias}.sales_order_line_id=s.sales_order_line_id
        AND {alias}.sales_order_id=s.sales_order_id AND {alias}.customer_id=s.customer_id
        AND {alias}.product_id=s.product_id AND {alias}.unit_id=s.unit_id
        AND {alias}.position_id=s.position_id AND {alias}.warehouse_id=s.warehouse_id
        AND {alias}.lot_id IS s.lot_id)"""


def returned(shipment_id="s.id"):
    """Both completed dispositions consume one common per-shipment quantity pool."""
    return f"""COALESCE((SELECT erp_decimal_sum(previous.quantity)
      FROM customer_return previous
      JOIN customer_return_finalization done ON done.return_id=previous.id
      WHERE previous.shipment_id={shipment_id}
        AND previous.row_version=done.source_version+1
        AND erp_decimal_cmp(previous.quantity,'0')>0 AND ({source('previous')})
        AND ((done.finalization_state='RECORDED_NO_RESTOCK' AND previous.restock_eligible=0
          AND NOT EXISTS(SELECT 1 FROM customer_return_entry WHERE finalization_id=done.id))
          OR (done.finalization_state='POSTED' AND previous.restock_eligible=1
            AND EXISTS(SELECT 1 FROM customer_return_entry effect
              WHERE effect.finalization_id=done.id AND effect.return_id=previous.id
                AND effect.position_id=previous.position_id AND effect.leg_no=1
                AND effect.qty_base_delta=previous.quantity AND effect.created_by=done.created_by)))),'0')"""


def authorization(alias):
    return f"""COALESCE((erp_customer_return_authorized()=1
      AND {alias}.authorization_basis='LOCAL_DEMO_MANUAL'
      AND erp_customer_return_document() IS {alias}.return_id
      AND erp_customer_return_digest() IS {alias}.confirmation_digest
      AND erp_customer_return_snapshot() IS {alias}.confirmation_snapshot
      AND erp_customer_return_time() IS {alias}.confirmation_received_at),0)=1"""


def source_guard(document):
    return f"""
      SELECT RAISE(ABORT,'customer_return requires genuine posted shipment lineage') WHERE NOT EXISTS(
        SELECT 1 FROM customer_return r WHERE r.id={document} AND ({source()}));
    """


def quantity_guard(document):
    # Count all other PREPARING documents too. Record-only preparations have no
    # entry, but must compete with restocks for this same finite source pool.
    return f"""
      SELECT RAISE(ABORT,'customer_return requires positive finalized quantity') WHERE EXISTS(
        SELECT 1 FROM customer_return r WHERE r.id={document} AND erp_decimal_cmp(r.quantity,'0')<=0);
      SELECT RAISE(ABORT,'customer_return remaining returnable quantity exceeded') WHERE EXISTS(
        SELECT 1 FROM customer_return r JOIN sales_shipment s ON s.id=r.shipment_id
        WHERE r.id={document} AND erp_decimal_cmp(
          (SELECT erp_decimal_sum(value) FROM (
            SELECT r.quantity AS value UNION ALL
            SELECT other.quantity FROM customer_return other
            JOIN customer_return_finalization pending ON pending.return_id=other.id
            WHERE other.shipment_id=r.shipment_id AND other.id<>r.id
              AND pending.finalization_state='PREPARING')),
          erp_decimal_sub(s.quantity,{returned()}))>0);
    """


def extend_ledger():
    connection = op.get_bind()
    original = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='physical_stock_ledger'").scalar_one()
    for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN",
                   "FINISHED_GOODS_RECEIPT", "SALES_SHIPMENT"]:
        if original.count("'" + domain + "'") != 1:
            raise RuntimeError("Unexpected original physical-ledger domain " + domain)
    if "'CUSTOMER_RETURN'" in original:
        raise RuntimeError("Customer return already occurs in shared physical ledger")
    op.execute("DROP VIEW physical_stock_ledger")
    op.execute(original.rstrip().rstrip(";") + """
      UNION ALL SELECT e.id,e.finalization_id,e.return_id,'CUSTOMER_RETURN',e.position_id,e.qty_base_delta,NULL
      FROM customer_return_entry e JOIN customer_return_finalization f ON f.id=e.finalization_id
      JOIN customer_return r ON r.id=f.return_id
      WHERE f.finalization_state='POSTED' AND r.restock_eligible=1
    """)


def upgrade():
    connection = op.get_bind()
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise RuntimeError("Customer return migration requires foreign_keys=ON")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Existing foreign-key violations prevent customer-return migration")
    fk = foundation.fk
    op.create_table("customer_return", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("shipment_id", sa.Text(), fk("sales_shipment.id"), nullable=False),
        sa.Column("shipment_version", sa.Integer(), nullable=False),
        sa.Column("sales_order_line_id", sa.Text(), fk("sales_order_line.id"), nullable=False),
        sa.Column("sales_order_id", sa.Text(), fk("sales_order.id"), nullable=False),
        sa.Column("customer_id", sa.Text(), fk("customer.id"), nullable=False),
        sa.Column("product_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("unit_id", sa.Text(), fk("unit.id"), nullable=False),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("lot_id", sa.Text(), fk("inventory_lot.id")),
        sa.Column("business_date", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("restock_eligible", sa.Boolean(create_constraint=False), nullable=False),
        sa.Column("reason", sa.Text()), sa.Column("note", sa.Text()),
        exact("quantity"), foundation.date_check("business_date"),
        sa.CheckConstraint("trim(document_no)<>'' AND substr(business_date,1,4)<>'0000' "
            "AND typeof(row_version)='integer' AND row_version>0 "
            "AND typeof(shipment_version)='integer' AND shipment_version>0 "
            "AND typeof(restock_eligible)='integer' AND restock_eligible IN (0,1)"),
        utc("created_at"), utc("updated_at"))
    op.create_table("customer_return_finalization", *receiving.created(),
        sa.Column("return_id", sa.Text(), fk("customer_return.id"), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("finalization_state", sa.Text(), nullable=False),
        sa.Column("authorization_basis", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("confirmation_snapshot", sa.Text(), nullable=False),
        sa.Column("confirmation_received_at", sa.Text(), nullable=False),
        sa.Column("finalized_at", sa.Text(), nullable=False),
        sa.Column("finalized_by", sa.Text(), fk("actor.id"), nullable=False),
        sa.CheckConstraint("finalization_state IN ('PREPARING','POSTED','RECORDED_NO_RESTOCK') "
            "AND authorization_basis='LOCAL_DEMO_MANUAL' "
            "AND typeof(source_version)='integer' AND source_version>0 AND trim(request_fingerprint)<>'' "
            "AND trim(confirmation_digest)<>'' AND json_valid(confirmation_snapshot)"),
        utc("created_at"), utc("confirmation_received_at"), utc("finalized_at"))
    op.create_table("customer_return_entry", *receiving.created(),
        sa.Column("finalization_id", sa.Text(), fk("customer_return_finalization.id"), nullable=False),
        sa.Column("return_id", sa.Text(), fk("customer_return.id"), nullable=False, unique=True),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False),
        sa.Column("qty_base_delta", sa.Text(), nullable=False),
        sa.UniqueConstraint("finalization_id", "leg_no"), exact("qty_base_delta"),
        sa.CheckConstraint("typeof(leg_no)='integer' AND leg_no=1 "
            "AND substr(qty_base_delta,1,1)<>'-' AND qty_base_delta<>'0'"), utc("created_at"))
    for table, fields in {
        "customer_return": ["shipment_id", "sales_order_line_id", "sales_order_id", "customer_id", "product_id",
                            "unit_id", "position_id", "warehouse_id", "lot_id", "created_by"],
        "customer_return_finalization": ["return_id", "created_by", "finalized_by"],
        "customer_return_entry": ["finalization_id", "return_id", "position_id", "created_by"],
    }.items():
        for field in fields:
            op.create_index("ix_" + table + "_" + field, table, [field])
    extend_ledger()
    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER customer_return_source_{action.lower()} BEFORE {action} ON customer_return
          WHEN NOT ({source('NEW')})
          BEGIN SELECT RAISE(ABORT,'customer_return requires genuine posted shipment lineage'); END""")
    op.execute("""CREATE TRIGGER customer_return_update_audit BEFORE UPDATE ON customer_return BEGIN
      SELECT RAISE(ABORT,'customer_return creation identity immutable') WHERE NEW.id<>OLD.id OR NEW.rowid<>OLD.rowid
        OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by;
      SELECT RAISE(ABORT,'customer_return version must advance once') WHERE NEW.row_version<>OLD.row_version+1;
      SELECT RAISE(ABORT,'customer_return cannot replace another return') WHERE EXISTS(
        SELECT 1 FROM customer_return WHERE id<>OLD.id AND (document_no=NEW.document_no OR rowid=NEW.rowid));
      END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER customer_return_finalized_{action.lower()} BEFORE {action} ON customer_return
          WHEN EXISTS(SELECT 1 FROM customer_return_finalization WHERE return_id=OLD.id)
          BEGIN SELECT RAISE(ABORT,'customer_return finalized history immutable'); END""")
        op.execute(f"""CREATE TRIGGER customer_return_entry_immutable_{action.lower()} BEFORE {action} ON customer_return_entry
          BEGIN SELECT RAISE(ABORT,'customer_return movement immutable'); END""")
    for table, extra in {
        "customer_return": "OR document_no=NEW.document_no",
        "customer_return_finalization": "OR return_id=NEW.return_id OR idempotency_key=NEW.idempotency_key",
        "customer_return_entry": "OR return_id=NEW.return_id OR (finalization_id=NEW.finalization_id AND leg_no=NEW.leg_no)",
    }.items():
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id OR rowid=NEW.rowid {extra})
          BEGIN SELECT RAISE(ABORT,'customer_return existing identity cannot be replaced'); END""")
    op.execute("""CREATE TRIGGER customer_return_finalization_delete BEFORE DELETE ON customer_return_finalization
      BEGIN SELECT RAISE(ABORT,'customer_return finalization immutable'); END""")
    for other in ["stock_posting", "supplier_return_posting", "material_issue_posting", "material_return_posting",
                  "finished_goods_posting", "sales_shipment_posting"]:
        op.execute(f"""CREATE TRIGGER customer_return_key_{other} BEFORE INSERT ON customer_return_finalization
          WHEN EXISTS(SELECT 1 FROM {other} WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'customer_return finalization key belongs to another domain'); END""")
        op.execute(f"""CREATE TRIGGER {other}_customer_return_key BEFORE INSERT ON {other}
          WHEN EXISTS(SELECT 1 FROM customer_return_finalization WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'posting key belongs to customer_return'); END""")
    op.execute(f"""CREATE TRIGGER customer_return_finalization_prepare BEFORE INSERT ON customer_return_finalization BEGIN
      SELECT RAISE(ABORT,'customer_return confirmation authorization denied') WHERE NOT ({authorization('NEW')});
      SELECT RAISE(ABORT,'customer_return finalization must prepare') WHERE NEW.finalization_state<>'PREPARING';
      SELECT RAISE(ABORT,'customer_return finalization audit mismatch') WHERE
        NEW.finalized_at IS NOT NEW.confirmation_received_at OR NEW.finalized_by<>NEW.created_by;
      SELECT RAISE(ABORT,'customer_return stale document version') WHERE NOT EXISTS(
        SELECT 1 FROM customer_return r WHERE r.id=NEW.return_id AND r.row_version=NEW.source_version+1);
      {source_guard('NEW.return_id')}{quantity_guard('NEW.return_id')}
      END""")
    op.execute(f"""CREATE TRIGGER customer_return_entry_inbound BEFORE INSERT ON customer_return_entry BEGIN
      SELECT RAISE(ABORT,'customer_return invalid authorized restock movement') WHERE NOT EXISTS(
        SELECT 1 FROM customer_return_finalization f JOIN customer_return r ON r.id=f.return_id
        WHERE f.id=NEW.finalization_id AND f.finalization_state='PREPARING' AND ({authorization('f')})
          AND r.id=NEW.return_id AND r.row_version=f.source_version+1 AND ({source()})
          AND r.restock_eligible=1 AND NEW.position_id=r.position_id AND NEW.leg_no=1
          AND NEW.qty_base_delta=r.quantity AND erp_decimal_cmp(r.quantity,'0')>0
          AND NEW.created_by=f.created_by);
      {quantity_guard('NEW.return_id')}
      END""")
    protected = ["id", "rowid", "return_id", "idempotency_key", "request_fingerprint", "source_version",
                 "authorization_basis", "confirmation_digest", "confirmation_snapshot", "confirmation_received_at",
                 "finalized_at", "finalized_by", "created_at", "created_by"]
    unchanged = " AND ".join("NEW." + field + " IS OLD." + field for field in protected)
    op.execute(f"""CREATE TRIGGER customer_return_finalization_finish BEFORE UPDATE ON customer_return_finalization BEGIN
      SELECT RAISE(ABORT,'customer_return confirmation authorization denied') WHERE NOT ({authorization('OLD')});
      SELECT RAISE(ABORT,'customer_return finalization immutable') WHERE NOT (
        OLD.finalization_state='PREPARING' AND NEW.finalization_state IN ('POSTED','RECORDED_NO_RESTOCK')
        AND {unchanged});
      SELECT RAISE(ABORT,'customer_return document version changed') WHERE NOT EXISTS(
        SELECT 1 FROM customer_return r WHERE r.id=OLD.return_id AND r.row_version=OLD.source_version+1);
      SELECT RAISE(ABORT,'customer_return disposition must match explicit restock designation') WHERE NOT EXISTS(
        SELECT 1 FROM customer_return r WHERE r.id=OLD.return_id
          AND ((r.restock_eligible=0 AND NEW.finalization_state='RECORDED_NO_RESTOCK'
            AND NOT EXISTS(SELECT 1 FROM customer_return_entry WHERE finalization_id=OLD.id))
          OR (r.restock_eligible=1 AND NEW.finalization_state='POSTED'
            AND (SELECT count(*) FROM customer_return_entry WHERE finalization_id=OLD.id)=1
            AND EXISTS(SELECT 1 FROM customer_return_entry e WHERE e.finalization_id=OLD.id
              AND e.return_id=r.id AND e.leg_no=1 AND e.position_id=r.position_id
              AND e.qty_base_delta=r.quantity AND erp_decimal_cmp(e.qty_base_delta,'0')>0
              AND e.created_by=OLD.created_by))));
      {source_guard('OLD.return_id')}{quantity_guard('OLD.return_id')}
      END""")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Customer return migration produced invalid foreign-key references")
    if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
        raise RuntimeError("Customer return migration failed integrity checking")


def downgrade():
    raise RuntimeError("Customer return downgrade disabled; preserve immutable history and verified backups.")
