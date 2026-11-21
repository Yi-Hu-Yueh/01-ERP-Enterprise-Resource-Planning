"""Add four independent business masters and required currency identity support."""
from alembic import op
import sqlalchemy as sa

revision = "0002_business_masters"
down_revision = "0001_master"
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
        sa.CheckConstraint("trim(code) <> ''"),
        sa.CheckConstraint("trim(name) <> ''"),
    ]


def party():
    return [
        sa.Column("tax_identifier", sa.Text()),
        sa.Column("contact_name", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("payment_terms", sa.Text()),
        sa.Column("currency_id", sa.Text(), sa.ForeignKey("currency.id", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False),
    ]


def upgrade():
    # Financial policy fields remain NULL until Owner D04 decisions are available.
    op.create_table("currency", *common(),
                    sa.Column("rounding_scale", sa.Integer()), sa.Column("rounding_mode", sa.Text()))
    op.create_table("customer", *common(), *party(),
                    sa.Column("billing_address", sa.Text()), sa.Column("default_ship_address", sa.Text()))
    op.create_table("supplier", *common(), *party(), sa.Column("address", sa.Text()))
    op.create_table("warehouse", *common(), sa.Column("address", sa.Text()),
                    sa.Column("mrp_included", sa.Boolean(create_constraint=True), nullable=False))
    op.create_table("work_center", *common(), sa.Column("description", sa.Text()))

    for table in ["currency", "customer", "supplier", "warehouse", "work_center"]:
        op.create_index("ix_" + table + "_created_by", table, ["created_by"])
    for table in ["customer", "supplier"]:
        op.create_index("ix_" + table + "_currency_id", table, ["currency_id"])


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled. Preserve a verified backup and plan an explicit migration.")
