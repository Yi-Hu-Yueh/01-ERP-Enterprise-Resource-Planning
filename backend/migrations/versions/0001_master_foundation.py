"""Four authorized master tables, plus Alembic's technical revision table."""
from alembic import op
import sqlalchemy as sa

revision = "0001_master"
down_revision = None
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(create_constraint=True), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("actor.id", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("trim(code) <> ''", name=None),
        sa.CheckConstraint("trim(name) <> ''", name=None),
    ]


def upgrade():
    op.create_table("actor",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("login_key", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("external_subject", sa.Text(), nullable=True, unique=True),
        sa.Column("is_active", sa.Boolean(create_constraint=True), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False))
    op.create_table("unit", *common(), sa.Column("quantity_rule", sa.Text(), nullable=False))
    op.create_table("item_category", *common(),
        sa.Column("parent_id", sa.Text(), sa.ForeignKey("item_category.id", ondelete="RESTRICT", onupdate="RESTRICT")),
        sa.CheckConstraint("parent_id IS NULL OR parent_id <> id"))
    op.create_table("item", *common(),
        sa.Column("specification", sa.Text()),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("category_id", sa.Text(), sa.ForeignKey("item_category.id", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False),
        sa.Column("base_unit_id", sa.Text(), sa.ForeignKey("unit.id", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False),
        sa.Column("lot_tracking", sa.Boolean(create_constraint=True), nullable=False),
        sa.CheckConstraint("item_kind IN ('RAW','FINISHED')"))
    # Hierarchy protection also applies to direct database writes, not just the helper UI.
    op.execute("""
    CREATE TRIGGER category_cycle BEFORE UPDATE OF parent_id ON item_category
    WHEN NEW.parent_id IS NOT NULL
    BEGIN
      SELECT RAISE(ABORT, 'category cycle')
      WHERE EXISTS (
        WITH RECURSIVE ancestors(id) AS (
          SELECT NEW.parent_id
          UNION SELECT c.parent_id FROM item_category c JOIN ancestors a ON c.id=a.id
          WHERE c.parent_id IS NOT NULL
        ) SELECT 1 FROM ancestors WHERE id=NEW.id
      );
    END
    """)


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled. Preserve a verified backup and plan an explicit migration.")

