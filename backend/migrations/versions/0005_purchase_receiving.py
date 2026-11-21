"""Add draft receiving, real lots/positions and atomic immutable inventory postings."""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "0005_purchase_receiving"
down_revision = "0004_purchase_orders"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")


def fk(target):
    return sa.ForeignKey(target, ondelete="RESTRICT", onupdate="RESTRICT")


def created():
    return [sa.Column("id", sa.Text(), primary_key=True), sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("created_by", sa.Text(), fk("actor.id"), nullable=False)]


def audit():
    return created()+[sa.Column("updated_at", sa.Text(), nullable=False), sa.Column("row_version", sa.Integer(), nullable=False)]


def decimal(column):
    constraint = foundation.decimal_check(column)
    constraint.name = "ck_stock_"+column
    return constraint


def optional_date(column):
    return sa.CheckConstraint(f"{column} IS NULL OR ({foundation.date_check(column).sqltext})", name="ck_stock_"+column)


def upgrade():
    op.create_table("inventory_lot", *created(),
        sa.Column("item_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("lot_code", sa.Text(), nullable=False),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("supplier_id", sa.Text(), fk("supplier.id"), nullable=False),
        sa.Column("supplier_lot_code", sa.Text()), sa.Column("manufactured_date", sa.Text()),
        sa.Column("expiry_date", sa.Text()), sa.Column("traceability_note", sa.Text()),
        sa.UniqueConstraint("item_id", "lot_code"), sa.CheckConstraint("trim(lot_code)<>'' AND origin_kind='PURCHASED'"),
        optional_date("manufactured_date"), optional_date("expiry_date"))
    op.create_table("stock_document", *audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True), sa.Column("document_kind", sa.Text(), nullable=False),
        sa.Column("purchase_order_id", sa.Text(), fk("purchase_order.id"), nullable=False),
        sa.Column("purchase_order_version", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Text(), nullable=False),
        sa.Column("supplier_id", sa.Text(), fk("supplier.id"), nullable=False),
        sa.Column("party_name_snapshot", sa.Text(), nullable=False),
        sa.Column("external_reference", sa.Text()), sa.Column("note", sa.Text()),
        sa.Column("approval_state", sa.Text()), sa.Column("approved_at", sa.Text()),
        sa.Column("approved_by", sa.Text(), fk("actor.id")),
        sa.CheckConstraint("trim(document_no)<>'' AND document_kind='PURCHASE_RECEIPT'"), foundation.date_check("business_date"))
    op.create_table("stock_document_line", *audit(),
        sa.Column("stock_document_id", sa.Text(), fk("stock_document.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("purchase_order_line_id", sa.Text(), fk("purchase_order_line.id"), nullable=False),
        sa.Column("purchase_order_line_version", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("description_snapshot", sa.Text(), nullable=False), sa.Column("quantity", sa.Text(), nullable=False),
        sa.Column("unit_id", sa.Text(), fk("unit.id"), nullable=False),
        sa.Column("base_factor_snapshot", sa.Text(), nullable=False),
        sa.Column("warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("lot_id", sa.Text(), fk("inventory_lot.id")), sa.Column("lot_code", sa.Text()),
        sa.Column("supplier_lot_code", sa.Text()), sa.Column("manufactured_date", sa.Text()), sa.Column("expiry_date", sa.Text()),
        sa.Column("note", sa.Text()), sa.UniqueConstraint("stock_document_id", "line_no"),
        sa.CheckConstraint("base_factor_snapshot='1' AND typeof(base_factor_snapshot)='text'"), decimal("quantity"),
        optional_date("manufactured_date"), optional_date("expiry_date"))
    op.create_table("stock_position", *created(),
        sa.Column("item_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("lot_id", sa.Text(), fk("inventory_lot.id")))
    # A normal nullable UNIQUE is insufficient in SQLite; both cases are covered.
    op.create_index("uq_stock_position_without_lot", "stock_position", ["item_id", "warehouse_id"],
                    unique=True, sqlite_where=sa.text("lot_id IS NULL"))
    op.create_index("uq_stock_position_with_lot", "stock_position", ["item_id", "warehouse_id", "lot_id"],
                    unique=True, sqlite_where=sa.text("lot_id IS NOT NULL"))
    op.create_table("stock_posting", *created(),
        sa.Column("stock_document_id", sa.Text(), fk("stock_document.id"), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.Text(), nullable=False), sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("posting_state", sa.Text(), nullable=False), sa.Column("posted_at", sa.Text(), nullable=False),
        sa.Column("posted_by", sa.Text(), fk("actor.id"), nullable=False),
        sa.CheckConstraint("posting_state IN ('PREPARING','POSTED')"))
    op.create_table("stock_entry", *created(),
        sa.Column("posting_id", sa.Text(), fk("stock_posting.id"), nullable=False),
        sa.Column("document_line_id", sa.Text(), fk("stock_document_line.id"), nullable=False),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False), sa.Column("qty_base_delta", sa.Text(), nullable=False),
        sa.UniqueConstraint("document_line_id", "leg_no"), decimal("qty_base_delta"))
    op.create_table("stock_create_receipt",
        sa.Column("request_key", sa.Text(), primary_key=True), sa.Column("resource_kind", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=False), sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False), sa.Column("created_by", sa.Text(), fk("actor.id"), nullable=False),
        sa.CheckConstraint("resource_kind IN ('header','line')"))
    indexes = {"inventory_lot": ["item_id", "supplier_id", "created_by"],
        "stock_document": ["purchase_order_id", "supplier_id", "approved_by", "created_by"],
        "stock_document_line": ["stock_document_id", "purchase_order_line_id", "item_id", "unit_id", "warehouse_id", "lot_id", "created_by"],
        "stock_position": ["item_id", "warehouse_id", "lot_id", "created_by"],
        "stock_posting": ["stock_document_id", "posted_by", "created_by"],
        "stock_entry": ["posting_id", "document_line_id", "position_id", "created_by"], "stock_create_receipt": ["created_by"]}
    for table, fields in indexes.items():
        for field in fields:
            op.create_index("ix_"+table+"_"+field, table, [field])
    for action, suffix in [("INSERT", "insert"), ("UPDATE", "update")]:
        op.execute(f"""CREATE TRIGGER stock_document_source_{suffix} BEFORE {action} ON stock_document
          WHEN NOT EXISTS(SELECT 1 FROM purchase_order WHERE id=NEW.purchase_order_id AND supplier_id=NEW.supplier_id)
          BEGIN SELECT RAISE(ABORT,'stock_document purchase source mismatch'); END""")
        op.execute(f"""CREATE TRIGGER stock_line_source_{suffix} BEFORE {action} ON stock_document_line BEGIN
          SELECT RAISE(ABORT,'stock_line source mismatch') WHERE NOT EXISTS
            (SELECT 1 FROM purchase_order_line p JOIN stock_document d ON d.id=NEW.stock_document_id JOIN item i ON i.id=p.item_id
             WHERE p.id=NEW.purchase_order_line_id AND p.purchase_order_id=d.purchase_order_id AND p.item_id=NEW.item_id
             AND p.unit_id=NEW.unit_id AND p.base_factor_snapshot=NEW.base_factor_snapshot AND i.item_kind='RAW');
          SELECT RAISE(ABORT,'stock_line nontracked lot') WHERE EXISTS(SELECT 1 FROM item WHERE id=NEW.item_id AND lot_tracking=0)
             AND (NEW.lot_id IS NOT NULL OR NEW.lot_code IS NOT NULL OR NEW.supplier_lot_code IS NOT NULL
                  OR NEW.manufactured_date IS NOT NULL OR NEW.expiry_date IS NOT NULL);
          SELECT RAISE(ABORT,'stock_line lot mismatch') WHERE NEW.lot_id IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM inventory_lot l JOIN stock_document d ON d.id=NEW.stock_document_id
             WHERE l.id=NEW.lot_id AND l.item_id=NEW.item_id AND l.supplier_id=d.supplier_id AND l.lot_code=NEW.lot_code
               AND l.supplier_lot_code IS NEW.supplier_lot_code AND l.manufactured_date IS NEW.manufactured_date
               AND l.expiry_date IS NEW.expiry_date);
          END""")
    op.execute("""CREATE TRIGGER stock_line_no_reparent BEFORE UPDATE OF stock_document_id ON stock_document_line
        WHEN NEW.stock_document_id<>OLD.stock_document_id BEGIN SELECT RAISE(ABORT,'stock_line cannot reparent'); END""")
    op.execute("""CREATE TRIGGER stock_line_posted_insert BEFORE INSERT ON stock_document_line
        WHEN EXISTS(SELECT 1 FROM stock_posting WHERE stock_document_id=NEW.stock_document_id)
        BEGIN SELECT RAISE(ABORT,'stock_document posted history immutable'); END""")
    op.execute("""CREATE TRIGGER stock_document_no_source_change BEFORE UPDATE OF purchase_order_id ON stock_document
        WHEN NEW.purchase_order_id<>OLD.purchase_order_id AND EXISTS(SELECT 1 FROM stock_document_line WHERE stock_document_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'stock_document has source lines'); END""")
    for table, condition in [("stock_document", "stock_document_id=OLD.id"),
                             ("stock_document_line", "stock_document_id=OLD.stock_document_id")]:
        for action in ["UPDATE", "DELETE"]:
            op.execute(f"""CREATE TRIGGER {table}_posted_{action.lower()} BEFORE {action} ON {table}
                WHEN EXISTS(SELECT 1 FROM stock_posting WHERE {condition})
                BEGIN SELECT RAISE(ABORT,'stock_document posted history immutable'); END""")
    for table in ["inventory_lot", "stock_position", "stock_entry"]:
        for action in ["UPDATE", "DELETE"]:
            op.execute(f"""CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table}
              BEGIN SELECT RAISE(ABORT,'stock_inventory history immutable'); END""")
    op.execute("""CREATE TRIGGER inventory_lot_raw BEFORE INSERT ON inventory_lot
        WHEN NOT EXISTS(SELECT 1 FROM item WHERE id=NEW.item_id AND item_kind='RAW' AND lot_tracking=1)
        BEGIN SELECT RAISE(ABORT,'stock_lot requires tracked RAW item'); END""")
    op.execute("""CREATE TRIGGER stock_position_identity BEFORE INSERT ON stock_position
        WHEN NOT EXISTS(SELECT 1 FROM item i WHERE i.id=NEW.item_id AND i.item_kind='RAW' AND
          ((i.lot_tracking=0 AND NEW.lot_id IS NULL) OR (i.lot_tracking=1 AND EXISTS
            (SELECT 1 FROM inventory_lot l WHERE l.id=NEW.lot_id AND l.item_id=NEW.item_id))))
        BEGIN SELECT RAISE(ABORT,'stock_position item lot mismatch'); END""")
    op.execute("""CREATE TRIGGER stock_protect_item BEFORE UPDATE OF item_kind,base_unit_id,lot_tracking ON item
        WHEN EXISTS(SELECT 1 FROM stock_document_line WHERE item_id=OLD.id)
          AND (NEW.item_kind<>OLD.item_kind OR NEW.base_unit_id<>OLD.base_unit_id OR NEW.lot_tracking<>OLD.lot_tracking)
        BEGIN SELECT RAISE(ABORT,'stock_receiving referenced item identity'); END""")
    op.execute("""CREATE TRIGGER stock_protect_purchase_line BEFORE UPDATE OF purchase_order_id,item_id,unit_id,base_factor_snapshot ON purchase_order_line
        WHEN EXISTS(SELECT 1 FROM stock_document_line WHERE purchase_order_line_id=OLD.id)
          AND (NEW.purchase_order_id<>OLD.purchase_order_id OR NEW.item_id<>OLD.item_id OR NEW.unit_id<>OLD.unit_id OR NEW.base_factor_snapshot<>OLD.base_factor_snapshot)
        BEGIN SELECT RAISE(ABORT,'stock_receiving referenced purchase line'); END""")
    op.execute("""CREATE TRIGGER stock_protect_purchase_supplier BEFORE UPDATE OF supplier_id ON purchase_order
        WHEN NEW.supplier_id<>OLD.supplier_id AND EXISTS(SELECT 1 FROM stock_document WHERE purchase_order_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'stock_receiving referenced supplier'); END""")
    op.execute("""CREATE TRIGGER stock_posting_prepare BEFORE INSERT ON stock_posting BEGIN
        SELECT RAISE(ABORT,'stock_posting must prepare') WHERE NEW.posting_state<>'PREPARING';
        SELECT RAISE(ABORT,'stock_posting invalid header version') WHERE NOT EXISTS
          (SELECT 1 FROM stock_document d JOIN purchase_order p ON p.id=d.purchase_order_id
           WHERE d.id=NEW.stock_document_id AND d.row_version=NEW.source_version+1 AND d.purchase_order_version=p.row_version
           AND d.document_kind='PURCHASE_RECEIPT' AND d.approval_state IS NULL AND p.approval_state IS NULL);
        SELECT RAISE(ABORT,'stock_posting empty receipt') WHERE NOT EXISTS(SELECT 1 FROM stock_document_line WHERE stock_document_id=NEW.stock_document_id);
        SELECT RAISE(ABORT,'stock_posting invalid inbound line') WHERE EXISTS
          (SELECT 1 FROM stock_document_line l JOIN purchase_order_line p ON p.id=l.purchase_order_line_id JOIN item i ON i.id=l.item_id
           WHERE l.stock_document_id=NEW.stock_document_id AND (l.purchase_order_line_version<>p.row_version OR l.quantity LIKE '-%'
             OR (i.lot_tracking=1 AND l.lot_id IS NULL) OR (i.lot_tracking=0 AND l.lot_id IS NOT NULL)));
        END""")
    op.execute("""CREATE TRIGGER stock_entry_inbound BEFORE INSERT ON stock_entry
        WHEN NOT EXISTS(SELECT 1 FROM stock_posting p JOIN stock_document_line l ON l.stock_document_id=p.stock_document_id
          JOIN stock_position s ON s.id=NEW.position_id WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING'
          AND l.id=NEW.document_line_id AND s.item_id=l.item_id AND s.warehouse_id=l.warehouse_id AND s.lot_id IS l.lot_id
          AND NEW.leg_no=1 AND l.base_factor_snapshot='1' AND NEW.qty_base_delta=l.quantity AND l.quantity NOT LIKE '-%')
        BEGIN SELECT RAISE(ABORT,'stock_entry invalid inbound movement'); END""")
    unchanged = " AND ".join("NEW."+field+" IS OLD."+field for field in ["id","stock_document_id","idempotency_key","request_fingerprint","source_version","posted_at","posted_by","created_at","created_by"])
    op.execute(f"""CREATE TRIGGER stock_posting_finalize BEFORE UPDATE ON stock_posting BEGIN
        SELECT RAISE(ABORT,'stock_posting immutable') WHERE NOT(OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {unchanged});
        SELECT RAISE(ABORT,'stock_posting incomplete movements') WHERE EXISTS
          (SELECT 1 FROM stock_document_line l WHERE l.stock_document_id=OLD.stock_document_id AND NOT EXISTS
            (SELECT 1 FROM stock_entry e WHERE e.posting_id=OLD.id AND e.document_line_id=l.id AND e.leg_no=1))
          OR (SELECT count(*) FROM stock_entry WHERE posting_id=OLD.id)<>(SELECT count(*) FROM stock_document_line WHERE stock_document_id=OLD.stock_document_id);
        END""")
    op.execute("""CREATE TRIGGER stock_posting_immutable_delete BEFORE DELETE ON stock_posting
        BEGIN SELECT RAISE(ABORT,'stock_posting immutable'); END""")


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled. Preserve a verified backup and plan an explicit migration.")
