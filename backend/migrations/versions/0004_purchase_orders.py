"""Add purchase headers/lines and durable create receipts; no stock/approval workflow."""
from alembic import op
import sqlalchemy as sa

revision = "0004_purchase_orders"
down_revision = "0003_supplier_offers"
branch_labels = None
depends_on = None


def fk(target):
    return sa.ForeignKey(target, ondelete="RESTRICT", onupdate="RESTRICT")


def audit():
    return [sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("created_by", sa.Text(), fk("actor.id"), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.Column("row_version", sa.Integer(), nullable=False)]


def decimal_check(column):
    unsigned = f"(CASE WHEN substr({column},1,1)='-' THEN substr({column},2) ELSE {column} END)"
    integer = f"(CASE WHEN instr({unsigned},'.')>0 THEN substr({unsigned},1,instr({unsigned},'.')-1) ELSE {unsigned} END)"
    return sa.CheckConstraint(f"""typeof({column})='text' AND {column} NOT GLOB '*[^0-9.-]*'
        AND {unsigned} GLOB '[0-9]*' AND instr({unsigned},'-')=0
        AND length({unsigned})-length(replace({unsigned},'.',''))<=1
        AND ({integer}='0' OR substr({integer},1,1) BETWEEN '1' AND '9')
        AND (instr({unsigned},'.')=0 OR substr({unsigned},-1) BETWEEN '1' AND '9')
        AND {column}<>'-0'""", name="ck_purchase_"+column)


def date_check(column):
    return sa.CheckConstraint(f"""{column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date({column}) IS NOT NULL AND date({column})={column}""", name="ck_purchase_"+column)


def upgrade():
    op.create_table("purchase_order", *audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("supplier_id", sa.Text(), fk("supplier.id"), nullable=False),
        sa.Column("supplier_name_snapshot", sa.Text(), nullable=False),
        sa.Column("currency_id", sa.Text(), fk("currency.id"), nullable=False),
        sa.Column("order_date", sa.Text(), nullable=False),
        sa.Column("delivery_address_snapshot", sa.Text()), sa.Column("payment_terms_snapshot", sa.Text()),
        sa.Column("note", sa.Text()), sa.Column("approval_state", sa.Text()),
        sa.Column("approved_at", sa.Text()), sa.Column("approved_by", sa.Text(), fk("actor.id")),
        sa.CheckConstraint("trim(document_no)<>'' AND trim(supplier_name_snapshot)<>''"), date_check("order_date"))
    op.create_table("purchase_order_line", *audit(),
        sa.Column("purchase_order_id", sa.Text(), fk("purchase_order.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Text(), fk("item.id"), nullable=False),
        sa.Column("description_snapshot", sa.Text(), nullable=False),
        sa.Column("unit_id", sa.Text(), fk("unit.id"), nullable=False),
        sa.Column("base_factor_snapshot", sa.Text(), nullable=False),
        sa.Column("ordered_qty", sa.Text(), nullable=False), sa.Column("unit_price", sa.Text(), nullable=False),
        sa.Column("due_date", sa.Text(), nullable=False),
        sa.Column("preferred_warehouse_id", sa.Text(), fk("warehouse.id"), nullable=False),
        sa.Column("supplier_offer_id", sa.Text(), fk("supplier_offer.id")),
        sa.UniqueConstraint("purchase_order_id", "line_no", name="uq_purchase_order_line_no"),
        sa.CheckConstraint("trim(description_snapshot)<>''"),
        sa.CheckConstraint("base_factor_snapshot='1' AND typeof(base_factor_snapshot)='text'"),
        decimal_check("ordered_qty"), decimal_check("unit_price"), date_check("due_date"))
    op.create_table("purchase_create_receipt",
        sa.Column("request_key", sa.Text(), primary_key=True),
        sa.Column("resource_kind", sa.Text(), nullable=False), sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False), sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), fk("actor.id"), nullable=False),
        sa.CheckConstraint("resource_kind IN ('header','line')"))
    for table, fields in {"purchase_order": ["supplier_id", "currency_id", "created_by", "approved_by"],
        "purchase_order_line": ["purchase_order_id", "item_id", "unit_id", "preferred_warehouse_id", "supplier_offer_id", "created_by"],
        "purchase_create_receipt": ["created_by"]}.items():
        for field in fields:
            op.create_index("ix_"+table+"_"+field, table, [field])
    for action, suffix in [("INSERT", "insert"), ("UPDATE OF item_id,unit_id,supplier_offer_id,purchase_order_id", "update")]:
        op.execute(f"""CREATE TRIGGER purchase_line_refs_{suffix} BEFORE {action} ON purchase_order_line BEGIN
          SELECT RAISE(ABORT,'purchase_line requires RAW base unit') WHERE NOT EXISTS
            (SELECT 1 FROM item WHERE id=NEW.item_id AND item_kind='RAW' AND base_unit_id=NEW.unit_id);
          SELECT RAISE(ABORT,'purchase_line offer mismatch') WHERE NEW.supplier_offer_id IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM supplier_offer o JOIN purchase_order p ON p.id=NEW.purchase_order_id
             WHERE o.id=NEW.supplier_offer_id AND o.item_id=NEW.item_id AND o.supplier_id=p.supplier_id);
          END""")
    op.execute("""CREATE TRIGGER purchase_line_no_reparent BEFORE UPDATE OF purchase_order_id ON purchase_order_line
        WHEN NEW.purchase_order_id<>OLD.purchase_order_id BEGIN SELECT RAISE(ABORT,'purchase_line cannot reparent'); END""")
    op.execute("""CREATE TRIGGER purchase_line_protect_item BEFORE UPDATE OF item_kind,base_unit_id ON item
        WHEN EXISTS (SELECT 1 FROM purchase_order_line WHERE item_id=OLD.id AND (NEW.item_kind<>'RAW' OR unit_id<>NEW.base_unit_id))
        BEGIN SELECT RAISE(ABORT,'purchase_line referenced item'); END""")
    op.execute("""CREATE TRIGGER purchase_order_protect_supplier BEFORE UPDATE OF supplier_id ON purchase_order
        WHEN EXISTS (SELECT 1 FROM purchase_order_line l JOIN supplier_offer o ON o.id=l.supplier_offer_id
          WHERE l.purchase_order_id=OLD.id AND o.supplier_id<>NEW.supplier_id)
        BEGIN SELECT RAISE(ABORT,'purchase_order offer mismatch'); END""")
    op.execute("""CREATE TRIGGER purchase_line_protect_offer BEFORE UPDATE OF supplier_id,item_id ON supplier_offer
        WHEN EXISTS (SELECT 1 FROM purchase_order_line l JOIN purchase_order p ON p.id=l.purchase_order_id
          WHERE l.supplier_offer_id=OLD.id AND (l.item_id<>NEW.item_id OR p.supplier_id<>NEW.supplier_id))
        BEGIN SELECT RAISE(ABORT,'purchase_line referenced offer'); END""")


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled. Preserve a verified backup and plan an explicit migration.")
