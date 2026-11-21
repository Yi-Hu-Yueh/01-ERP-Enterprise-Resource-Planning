"""Add customer demand headers, independent lines and durable creates only."""
from importlib import import_module
from alembic import op
import sqlalchemy as sa

revision = "0015_sales_orders"
down_revision = "0014_finished_goods_receipts"
branch_labels = None
depends_on = None

conventions = import_module("backend.migrations.versions.0004_purchase_orders")


def upgrade():
    fk, audit = conventions.fk, conventions.audit
    op.create_table("sales_order", *audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("customer_id", sa.Text(), fk("customer.id"), nullable=False),
        sa.Column("order_date", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()), sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint("trim(document_no)<>''"), sa.CheckConstraint("state='DRAFT'"),
        conventions.date_check("order_date"), sa.CheckConstraint("row_version>0"))
    op.create_table("sales_order_line", *audit(),
        sa.Column("sales_order_id", sa.Text(), fk("sales_order.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("description_snapshot", sa.Text(), nullable=False),
        sa.Column("unit_id", sa.Text(), fk("unit.id"), nullable=False),
        sa.Column("ordered_qty", sa.Text(), nullable=False), sa.Column("unit_price", sa.Text(), nullable=False),
        sa.UniqueConstraint("sales_order_id", "line_no"),
        sa.CheckConstraint("trim(description_snapshot)<>'' AND row_version>0 AND line_no>0"),
        conventions.decimal_check("ordered_qty"), conventions.decimal_check("unit_price"))
    op.create_table("sales_create_receipt",
        sa.Column("request_key", sa.Text(), primary_key=True),
        sa.Column("resource_kind", sa.Text(), nullable=False), sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False), sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), fk("actor.id"), nullable=False),
        sa.CheckConstraint("resource_kind IN ('header','line')"))
    for table, fields in {"sales_order":["customer_id","created_by"],
            "sales_order_line":["sales_order_id","item_id","unit_id","created_by"],
            "sales_create_receipt":["created_by"]}.items():
        for field in fields:op.create_index("ix_"+table+"_"+field, table, [field])
    for action,suffix in [("INSERT","insert"),("UPDATE OF item_id,unit_id","update")]:
        op.execute(f"""CREATE TRIGGER sales_line_refs_{suffix} BEFORE {action} ON sales_order_line
        WHEN NOT EXISTS(SELECT 1 FROM item WHERE id=NEW.item_id AND item_kind='FINISHED' AND base_unit_id=NEW.unit_id)
        BEGIN SELECT RAISE(ABORT,'sales_line requires FINISHED base unit'); END""")
    op.execute("""CREATE TRIGGER sales_line_no_reparent BEFORE UPDATE OF sales_order_id ON sales_order_line
        WHEN NEW.sales_order_id<>OLD.sales_order_id
        BEGIN SELECT RAISE(ABORT,'sales_line cannot reparent'); END""")
    op.execute("""CREATE TRIGGER sales_line_protect_item BEFORE UPDATE OF item_kind,base_unit_id ON item
        WHEN EXISTS(SELECT 1 FROM sales_order_line WHERE item_id=OLD.id AND (NEW.item_kind<>'FINISHED' OR unit_id<>NEW.base_unit_id))
        BEGIN SELECT RAISE(ABORT,'sales_line referenced item'); END""")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; preserve verified backups.")
