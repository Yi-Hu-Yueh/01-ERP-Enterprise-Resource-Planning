"""Schema-only no-login organization and versioned approval demonstration."""
from alembic import op
from backend.app.approvals_schema import TABLES, statements

revision = "0021_electronic_approvals"
down_revision = "0020_inventory_transfers"
branch_labels = None
depends_on = None


def upgrade():
    for statement in statements():
        op.execute(statement)


def downgrade():
    # Historical approval evidence must not be discarded by an incidental downgrade.
    connection = op.get_bind()
    if any(connection.exec_driver_sql(f'SELECT 1 FROM "{table}" LIMIT 1').first() for table in TABLES):
        raise RuntimeError("Approval/organization data exists; destructive downgrade is forbidden")
    for table in reversed(TABLES):
        op.execute(f'DROP TABLE "{table}"')
