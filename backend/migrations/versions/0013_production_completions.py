"""Add noninventory production reports, immutable after explicit confirmation."""
from importlib import import_module
from alembic import op
import sqlalchemy as sa

revision = "0013_production_completions"
down_revision = "0012_material_returns"
branch_labels = None
depends_on = None
foundation = import_module("backend.migrations.versions.0004_purchase_orders")


def upgrade():
    confirmation_fields = ["confirmation_id", "confirmation_fingerprint", "confirmation_snapshot",
                           "confirmed_at", "confirmed_by", "authorization_basis"]
    empty = " AND ".join(c + " IS NULL" for c in confirmation_fields)
    complete = " AND ".join(c + " IS NOT NULL" for c in confirmation_fields)
    decimals = []
    for field in ["good_qty_base", "defective_qty_base"]:
        check = foundation.decimal_check(field)
        check.name = "ck_completion_" + field
        decimals.append(check)
    op.create_table("production_completion", *foundation.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("work_order_id", sa.Text(), foundation.fk("work_order.id"), nullable=False),
        sa.Column("product_id", sa.Text(), foundation.fk("item.id"), nullable=False),
        sa.Column("unit_id", sa.Text(), foundation.fk("unit.id"), nullable=False),
        sa.Column("report_date", sa.Text(), nullable=False),
        sa.Column("good_qty_base", sa.Text(), nullable=False),
        sa.Column("defective_qty_base", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("confirmation_id", sa.Text(), unique=True),
        sa.Column("confirmation_fingerprint", sa.Text()),
        sa.Column("confirmation_snapshot", sa.Text()),
        sa.Column("confirmed_at", sa.Text()),
        sa.Column("confirmed_by", sa.Text(), foundation.fk("actor.id")),
        sa.Column("authorization_basis", sa.Text()),
        *decimals, foundation.date_check("report_date"),
        sa.CheckConstraint("substr(report_date,1,4)<>'0000' AND trim(document_no)<>''"),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0"),
        sa.CheckConstraint(f"(state='DRAFT' AND {empty}) OR (state='CONFIRMED' AND {complete} "
            "AND authorization_basis='LOCAL_DEMO_MANUAL' AND length(confirmation_fingerprint)=64 "
            "AND json_valid(confirmation_snapshot) AND length(confirmed_at)=27)"))
    for field in ["work_order_id", "product_id", "unit_id", "created_by", "confirmed_by"]:
        op.create_index("ix_completion_" + field, "production_completion", [field])
    op.create_index("ix_completion_order_state", "production_completion", ["work_order_id", "state"])
    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER completion_source_{action.lower()} BEFORE {action} ON production_completion BEGIN
          SELECT RAISE(ABORT,'completion work order product/base unit mismatch') WHERE NOT EXISTS(
            SELECT 1 FROM work_order w JOIN item i ON i.id=w.product_id
            WHERE w.id=NEW.work_order_id AND i.item_kind='FINISHED'
              AND i.id=NEW.product_id AND i.base_unit_id=NEW.unit_id);
          END""")
    op.execute("""CREATE TRIGGER completion_insert_draft BEFORE INSERT ON production_completion
      WHEN NEW.state<>'DRAFT' BEGIN SELECT RAISE(ABORT,'completion must start as draft'); END""")
    op.execute("""CREATE TRIGGER completion_no_replace BEFORE INSERT ON production_completion
      WHEN EXISTS(SELECT 1 FROM production_completion WHERE id=NEW.id OR document_no=NEW.document_no OR rowid=NEW.rowid)
      BEGIN SELECT RAISE(ABORT,'completion existing identity cannot be replaced'); END""")
    op.execute("""CREATE TRIGGER completion_number_no_replace BEFORE UPDATE ON production_completion
      WHEN EXISTS(SELECT 1 FROM production_completion WHERE document_no=NEW.document_no AND id<>OLD.id)
      BEGIN SELECT RAISE(ABORT,'completion number cannot replace another report'); END""")
    op.execute("""CREATE TRIGGER completion_update_guard BEFORE UPDATE ON production_completion BEGIN
      SELECT RAISE(ABORT,'confirmed completion immutable') WHERE OLD.state='CONFIRMED';
      SELECT RAISE(ABORT,'completion immutable creation identity') WHERE NEW.id<>OLD.id OR NEW.rowid<>OLD.rowid
        OR NEW.created_at<>OLD.created_at OR NEW.created_by<>OLD.created_by;
      SELECT RAISE(ABORT,'completion version must advance once') WHERE NEW.row_version<>OLD.row_version+1;
      END""")
    unchanged = " AND ".join("NEW." + c + " IS OLD." + c for c in [
        "document_no", "work_order_id", "product_id", "unit_id", "report_date", "good_qty_base", "defective_qty_base", "note"])
    op.execute(f"""CREATE TRIGGER completion_confirm_guard BEFORE UPDATE ON production_completion
      WHEN NEW.state='CONFIRMED' BEGIN
      SELECT RAISE(ABORT,'completion confirmation cannot also edit event') WHERE NOT ({unchanged});
      SELECT RAISE(ABORT,'completion requires a nonnegative nonzero productive event') WHERE
        erp_decimal_cmp(NEW.good_qty_base,'0')<0 OR erp_decimal_cmp(NEW.defective_qty_base,'0')<0
        OR (erp_decimal_cmp(NEW.good_qty_base,'0')=0 AND erp_decimal_cmp(NEW.defective_qty_base,'0')=0);
      SELECT RAISE(ABORT,'completion explicit local confirmation required') WHERE NOT (
        erp_completion_document() IS NEW.id AND erp_completion_fingerprint() IS NEW.confirmation_fingerprint
        AND erp_completion_snapshot() IS NEW.confirmation_snapshot AND erp_completion_time() IS NEW.confirmed_at);
      END""")
    op.execute("""CREATE TRIGGER completion_delete_guard BEFORE DELETE ON production_completion
      WHEN OLD.state='CONFIRMED' BEGIN SELECT RAISE(ABORT,'confirmed completion cannot be deleted'); END""")
    op.execute("""CREATE TRIGGER completion_work_order_hold BEFORE UPDATE ON work_order
      WHEN EXISTS(SELECT 1 FROM production_completion WHERE work_order_id=OLD.id)
      BEGIN SELECT RAISE(ABORT,'completion referenced work order held'); END""")


def downgrade():
    raise RuntimeError("Completion downgrade disabled; preserve confirmed history and use verified backup recovery.")
