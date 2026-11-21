"""Add two BOM tables and guards; never rebuild existing business tables."""
from importlib import import_module
from alembic import op
import sqlalchemy as sa

revision = "0007_product_bom"
down_revision = "0006_supplier_returns"
branch_labels = None
depends_on = None
foundation = import_module("backend.migrations.versions.0004_purchase_orders")


def decimal(column):
    check = foundation.decimal_check(column)
    check.name = "ck_bom_" + column
    return check


def upgrade():
    op.create_table("bom", *foundation.audit(),
        sa.Column("product_id", sa.Text(), foundation.fk("item.id"), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("output_basis_qty", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Text(), nullable=False),
        sa.Column("valid_to", sa.Text()),
        sa.Column("release_state", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("product_id", "revision", name="uq_bom_product_revision"),
        sa.CheckConstraint("trim(revision)<>'' AND release_state IN ('DRAFT','RELEASED','RETIRED') AND is_active IN (0,1)"),
        sa.CheckConstraint("output_basis_qty<>'0' AND substr(output_basis_qty,1,1)<>'-'"),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0"),
        decimal("output_basis_qty"), foundation.date_check("valid_from"),
        sa.CheckConstraint("valid_to IS NULL OR ("+str(foundation.date_check("valid_to").sqltext)+" AND valid_to>=valid_from)"))
    op.create_table("bom_component", *foundation.audit(),
        sa.Column("bom_id", sa.Text(), foundation.fk("bom.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Text(), foundation.fk("item.id"), nullable=False),
        sa.Column("usage_qty_base", sa.Text(), nullable=False),
        sa.Column("purpose_note", sa.Text()),
        sa.UniqueConstraint("bom_id", "line_no", name="uq_bom_component_line"),
        sa.CheckConstraint("typeof(line_no)='integer' AND line_no>0 AND typeof(row_version)='integer' AND row_version>0"),
        decimal("usage_qty_base"))
    for table, fields in {"bom": ["product_id", "created_by"], "bom_component": ["bom_id", "material_id", "created_by"]}.items():
        for field in fields:
            op.create_index("ix_"+table+"_"+field, table, [field])
    for table, field, kind in [("bom", "product_id", "FINISHED"), ("bom_component", "material_id", "RAW")]:
        for event, suffix in [("INSERT", "insert"), ("UPDATE", "update")]:
            op.execute(f"""CREATE TRIGGER {table}_item_kind_{suffix} BEFORE {event} ON {table}
                WHEN NOT EXISTS(SELECT 1 FROM item WHERE id=NEW.{field} AND item_kind='{kind}')
                BEGIN SELECT RAISE(ABORT,'bom item kind mismatch'); END""")
    op.execute("""CREATE TRIGGER bom_product_with_components BEFORE UPDATE OF product_id ON bom
        WHEN NEW.product_id<>OLD.product_id AND EXISTS(SELECT 1 FROM bom_component WHERE bom_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'bom product with components cannot change'); END""")
    op.execute("""CREATE TRIGGER bom_component_no_reparent BEFORE UPDATE OF bom_id ON bom_component
        WHEN NEW.bom_id<>OLD.bom_id BEGIN SELECT RAISE(ABORT,'bom component cannot reparent'); END""")
    op.execute("""CREATE TRIGGER bom_item_identity_guard BEFORE UPDATE OF id,code,item_kind,base_unit_id ON item
        WHEN (NEW.id<>OLD.id OR NEW.code<>OLD.code OR NEW.item_kind<>OLD.item_kind OR NEW.base_unit_id<>OLD.base_unit_id)
        AND (EXISTS(SELECT 1 FROM bom WHERE product_id=OLD.id) OR EXISTS(SELECT 1 FROM bom_component WHERE material_id=OLD.id))
        BEGIN SELECT RAISE(ABORT,'bom referenced item identity protected'); END""")
    # No release endpoint or automatic release selection. Preserve any future sealed history.
    for event, suffix in [("UPDATE", "update"), ("DELETE", "delete")]:
        op.execute(f"""CREATE TRIGGER bom_sealed_{suffix} BEFORE {event} ON bom WHEN OLD.release_state<>'DRAFT'
            BEGIN SELECT RAISE(ABORT,'bom historical version immutable'); END""")
        op.execute(f"""CREATE TRIGGER bom_component_sealed_{suffix} BEFORE {event} ON bom_component
            WHEN EXISTS(SELECT 1 FROM bom WHERE id=OLD.bom_id AND release_state<>'DRAFT')
            BEGIN SELECT RAISE(ABORT,'bom historical component immutable'); END""")
    op.execute("""CREATE TRIGGER bom_component_sealed_insert BEFORE INSERT ON bom_component
        WHEN EXISTS(SELECT 1 FROM bom WHERE id=NEW.bom_id AND release_state<>'DRAFT')
        BEGIN SELECT RAISE(ABORT,'bom historical component immutable'); END""")
    for table in ["bom", "bom_component"]:
        op.execute(f"""CREATE TRIGGER {table}_audit_immutable BEFORE UPDATE ON {table}
            WHEN NEW.id<>OLD.id OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by
            BEGIN SELECT RAISE(ABORT,'bom original audit immutable'); END""")


def downgrade():
    # Recovery is a verified backup, never a destructive application downgrade.
    raise RuntimeError("BOM downgrade is disabled; restore a verified backup only after safeguarding newer records.")
