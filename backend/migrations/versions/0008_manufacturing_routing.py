"""Add routing and reusable operations without altering any prior table or BOM."""
from importlib import import_module
from alembic import op
import sqlalchemy as sa

revision = "0008_manufacturing_routing"
down_revision = "0007_product_bom"
branch_labels = None
depends_on = None
foundation = import_module("backend.migrations.versions.0004_purchase_orders")


def optional_decimal(column):
    return sa.CheckConstraint(f"{column} IS NULL OR ({foundation.decimal_check(column).sqltext})",
                              name="ck_routing_"+column)


def upgrade():
    op.create_table("operation_definition", *foundation.audit(),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.CheckConstraint("trim(code)<>'' AND trim(name)<>'' AND is_active IN (0,1)"),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0"))
    op.create_table("routing", *foundation.audit(),
        sa.Column("product_id", sa.Text(), foundation.fk("item.id"), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Text(), nullable=False),
        sa.Column("valid_to", sa.Text()),
        sa.Column("release_state", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("product_id", "revision", name="uq_routing_product_revision"),
        sa.CheckConstraint("trim(revision)<>'' AND release_state='DRAFT' AND is_active IN (0,1)"),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0"),
        foundation.date_check("valid_from"),
        sa.CheckConstraint("valid_to IS NULL OR ("+str(foundation.date_check("valid_to").sqltext)+" AND valid_to>=valid_from)"))
    op.create_table("routing_step", *foundation.audit(),
        sa.Column("routing_id", sa.Text(), foundation.fk("routing.id"), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Text(), foundation.fk("operation_definition.id"), nullable=False),
        sa.Column("work_center_id", sa.Text(), foundation.fk("work_center.id"), nullable=False),
        sa.Column("setup_minutes", sa.Text()),
        sa.Column("run_minutes_per_base", sa.Text()),
        sa.UniqueConstraint("routing_id", "sequence_no", name="uq_routing_step_sequence"),
        sa.CheckConstraint("typeof(sequence_no)='integer'"),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0"),
        optional_decimal("setup_minutes"), optional_decimal("run_minutes_per_base"))
    for table, fields in {"operation_definition": ["created_by"], "routing": ["product_id", "created_by"],
                          "routing_step": ["routing_id", "operation_id", "work_center_id", "created_by"]}.items():
        for field in fields:
            op.create_index("ix_"+table+"_"+field, table, [field])
    for event, suffix in [("INSERT", "insert"), ("UPDATE", "update")]:
        op.execute(f"""CREATE TRIGGER routing_finished_{suffix} BEFORE {event} ON routing
            WHEN NOT EXISTS(SELECT 1 FROM item WHERE id=NEW.product_id AND item_kind='FINISHED')
            BEGIN SELECT RAISE(ABORT,'routing product must be FINISHED'); END""")
    # Existing inactive references may be retained; a new assignment must be applicable.
    for event, suffix in [("INSERT", "insert"), ("UPDATE", "update")]:
        condition = "" if event == "INSERT" else "AND NEW.operation_id<>OLD.operation_id"
        center_condition = "" if event == "INSERT" else "AND NEW.work_center_id<>OLD.work_center_id"
        op.execute(f"""CREATE TRIGGER routing_step_applicable_{suffix} BEFORE {event} ON routing_step BEGIN
            SELECT RAISE(ABORT,'routing inactive operation') WHERE
                EXISTS(SELECT 1 FROM operation_definition WHERE id=NEW.operation_id AND is_active=0) {condition};
            SELECT RAISE(ABORT,'routing inactive work center') WHERE
                EXISTS(SELECT 1 FROM work_center WHERE id=NEW.work_center_id AND is_active=0) {center_condition};
            END""")
    op.execute("""CREATE TRIGGER routing_no_product_reinterpretation BEFORE UPDATE OF product_id ON routing
        WHEN NEW.product_id<>OLD.product_id AND EXISTS(SELECT 1 FROM routing_step WHERE routing_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'routing product with steps cannot change'); END""")
    op.execute("""CREATE TRIGGER routing_step_no_reparent BEFORE UPDATE OF routing_id ON routing_step
        WHEN NEW.routing_id<>OLD.routing_id BEGIN SELECT RAISE(ABORT,'routing step cannot reparent'); END""")
    op.execute("""CREATE TRIGGER routing_item_basis_guard BEFORE UPDATE OF id,code,item_kind,base_unit_id ON item
        WHEN (NEW.id<>OLD.id OR NEW.code<>OLD.code OR NEW.item_kind<>OLD.item_kind OR NEW.base_unit_id<>OLD.base_unit_id)
        AND EXISTS(SELECT 1 FROM routing WHERE product_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'routing product identity or time basis protected'); END""")
    for table in ["operation_definition", "routing", "routing_step"]:
        op.execute(f"""CREATE TRIGGER {table}_audit_immutable BEFORE UPDATE ON {table}
            WHEN NEW.id<>OLD.id OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by
            BEGIN SELECT RAISE(ABORT,'routing original audit immutable'); END""")


def downgrade():
    raise RuntimeError("Routing downgrade is disabled; safeguard newer records before verified backup recovery.")
