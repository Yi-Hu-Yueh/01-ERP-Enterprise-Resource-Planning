"""Add one draft work-order table and source-reference holds, without release snapshots."""
from importlib import import_module
from alembic import op
import sqlalchemy as sa

revision = "0009_draft_work_orders"
down_revision = "0008_manufacturing_routing"
branch_labels = None
depends_on = None
foundation = import_module("backend.migrations.versions.0004_purchase_orders")


def upgrade():
    quantity = foundation.decimal_check("planned_qty_base")
    quantity.name = "ck_work_order_planned_qty_base"
    op.create_table("work_order", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("product_id", sa.Text(), foundation.fk("item.id"), nullable=False),
        sa.Column("planned_qty_base", sa.Text(), nullable=False),
        sa.Column("planned_start", sa.Text(), nullable=False),
        sa.Column("planned_finish", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("approval_state", sa.Text(), nullable=False),
        sa.Column("bom_id", sa.Text(), foundation.fk("bom.id")),
        sa.Column("routing_id", sa.Text(), foundation.fk("routing.id")),
        sa.CheckConstraint("trim(document_no)<>'' AND approval_state='DRAFT'"),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0"),
        quantity, foundation.date_check("planned_start"), foundation.date_check("planned_finish"))
    for field in ["product_id", "bom_id", "routing_id", "created_by"]:
        op.create_index("ix_work_order_"+field, "work_order", [field])
    for event, suffix in [("INSERT", "insert"), ("UPDATE", "update")]:
        op.execute(f"""CREATE TRIGGER work_order_refs_{suffix} BEFORE {event} ON work_order BEGIN
            SELECT RAISE(ABORT,'work_order product must be FINISHED') WHERE
                NOT EXISTS(SELECT 1 FROM item WHERE id=NEW.product_id AND item_kind='FINISHED');
            SELECT RAISE(ABORT,'work_order BOM product mismatch') WHERE NEW.bom_id IS NOT NULL AND
                NOT EXISTS(SELECT 1 FROM bom WHERE id=NEW.bom_id AND product_id=NEW.product_id);
            SELECT RAISE(ABORT,'work_order routing product mismatch') WHERE NEW.routing_id IS NOT NULL AND
                NOT EXISTS(SELECT 1 FROM routing WHERE id=NEW.routing_id AND product_id=NEW.product_id);
            END""")
    op.execute("""CREATE TRIGGER work_order_no_replace BEFORE INSERT ON work_order
        WHEN EXISTS(SELECT 1 FROM work_order WHERE id=NEW.id OR document_no=NEW.document_no OR rowid=NEW.rowid)
        BEGIN SELECT RAISE(ABORT,'work_order existing identity cannot be replaced'); END""")
    op.execute("""CREATE TRIGGER work_order_number_no_replace BEFORE UPDATE OF document_no ON work_order
        WHEN EXISTS(SELECT 1 FROM work_order WHERE document_no=NEW.document_no AND id<>OLD.id)
        BEGIN SELECT RAISE(ABORT,'work_order business number cannot replace another order'); END""")
    op.execute("""CREATE TRIGGER work_order_audit_immutable BEFORE UPDATE ON work_order
        WHEN NEW.id<>OLD.id OR NEW.rowid<>OLD.rowid OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by
        BEGIN SELECT RAISE(ABORT,'work_order creation identity immutable'); END""")
    op.execute("""CREATE TRIGGER work_order_item_basis_guard BEFORE UPDATE OF id,code,item_kind,base_unit_id ON item
        WHEN (NEW.id<>OLD.id OR NEW.code<>OLD.code OR NEW.item_kind<>OLD.item_kind OR NEW.base_unit_id<>OLD.base_unit_id)
        AND EXISTS(SELECT 1 FROM work_order WHERE product_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'work_order product identity or planned quantity unit protected'); END""")
    for parent, child, fk in [("bom", "bom_component", "bom_id"), ("routing", "routing_step", "routing_id")]:
        op.execute(f"""CREATE TRIGGER work_order_{parent}_hold_update BEFORE UPDATE ON {parent}
            WHEN EXISTS(SELECT 1 FROM work_order WHERE {fk}=OLD.id)
            BEGIN SELECT RAISE(ABORT,'work_order referenced {parent} held'); END""")
        # FK RESTRICT blocks parent deletion/replacement. Child references are indirect:
        # cover both sides and INSERT OR REPLACE with recursive_triggers OFF.
        op.execute(f"""CREATE TRIGGER work_order_{child}_hold_insert BEFORE INSERT ON {child}
            WHEN EXISTS(SELECT 1 FROM work_order WHERE {fk}=NEW.{fk})
              OR EXISTS(SELECT 1 FROM {child} c JOIN work_order w ON w.{fk}=c.{fk} WHERE c.id=NEW.id OR c.rowid=NEW.rowid)
            BEGIN SELECT RAISE(ABORT,'work_order referenced {child} held'); END""")
        op.execute(f"""CREATE TRIGGER work_order_{child}_hold_update BEFORE UPDATE ON {child}
            WHEN EXISTS(SELECT 1 FROM work_order WHERE {fk}=OLD.{fk} OR {fk}=NEW.{fk})
              OR EXISTS(SELECT 1 FROM {child} c JOIN work_order w ON w.{fk}=c.{fk} WHERE c.rowid=NEW.rowid)
            BEGIN SELECT RAISE(ABORT,'work_order referenced {child} held'); END""")
        op.execute(f"""CREATE TRIGGER work_order_{child}_hold_delete BEFORE DELETE ON {child}
            WHEN EXISTS(SELECT 1 FROM work_order WHERE {fk}=OLD.{fk})
            BEGIN SELECT RAISE(ABORT,'work_order referenced {child} held'); END""")


def downgrade():
    raise RuntimeError("Work-order downgrade is disabled; safeguard newer records before verified backup recovery.")
