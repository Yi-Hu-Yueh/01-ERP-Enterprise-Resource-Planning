"""Add current supplier/RAW associations, exact prices and restrictive integrity."""
from alembic import op
import sqlalchemy as sa

revision = "0003_supplier_offers"
down_revision = "0002_business_masters"
branch_labels = None
depends_on = None


def upgrade():
    # Canonical decimal validation uses only SQLite built-ins; no UDF dependency.
    unsigned = "(CASE WHEN substr(unit_price,1,1)='-' THEN substr(unit_price,2) ELSE unit_price END)"
    integer = f"(CASE WHEN instr({unsigned},'.')>0 THEN substr({unsigned},1,instr({unsigned},'.')-1) ELSE {unsigned} END)"
    canonical = f"""typeof(unit_price)='text' AND unit_price NOT GLOB '*[^0-9.-]*'
        AND {unsigned} GLOB '[0-9]*' AND instr({unsigned},'-')=0
        AND length({unsigned})-length(replace({unsigned},'.',''))<=1
        AND ({integer}='0' OR substr({integer},1,1) BETWEEN '1' AND '9')
        AND (instr({unsigned},'.')=0 OR substr({unsigned},-1) BETWEEN '1' AND '9')
        AND unit_price<>'-0'"""
    op.create_table("supplier_offer",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("supplier_id", sa.Text(), sa.ForeignKey("supplier.id", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False),
        sa.Column("item_id", sa.Text(), sa.ForeignKey("item.id", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False),
        sa.Column("unit_price", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(create_constraint=True), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(canonical, name="ck_supplier_offer_decimal_text"))
    op.create_index("uq_supplier_offer_active_pair", "supplier_offer", ["supplier_id", "item_id"],
                    unique=True, sqlite_where=sa.text("is_active=1"))
    for field in ["supplier_id", "item_id", "created_by"]:
        op.create_index("ix_supplier_offer_" + field, "supplier_offer", [field])
    for action in ["INSERT", "UPDATE OF item_id"]:
        suffix = "insert" if action == "INSERT" else "update"
        op.execute(f"""CREATE TRIGGER supplier_offer_raw_{suffix} BEFORE {action} ON supplier_offer
            WHEN NOT EXISTS (SELECT 1 FROM item WHERE id=NEW.item_id AND item_kind='RAW')
            BEGIN SELECT RAISE(ABORT, 'supplier_offer requires an existing RAW item'); END""")
    op.execute("""CREATE TRIGGER supplier_offer_protect_item_kind BEFORE UPDATE OF item_kind ON item
        WHEN NEW.item_kind<>'RAW' AND EXISTS (SELECT 1 FROM supplier_offer WHERE item_id=OLD.id)
        BEGIN SELECT RAISE(ABORT, 'supplier_offer requires RAW item kind'); END""")


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled. Preserve a verified backup and plan an explicit migration.")
