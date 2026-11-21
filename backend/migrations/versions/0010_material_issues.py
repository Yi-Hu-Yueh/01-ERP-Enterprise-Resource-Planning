"""Add issues and a common physical-ledger view; Owner execution defaults closed."""
from importlib import import_module
import re
from alembic import op
import sqlalchemy as sa

revision = "0010_material_issues"
down_revision = "0009_draft_work_orders"
branch_labels = None
depends_on = None
foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")

LEDGER_VIEW = """
CREATE VIEW physical_stock_ledger AS
 SELECT e.id AS entry_id,e.posting_id,e.document_line_id,'PURCHASE_RECEIPT' AS domain,
        e.position_id,e.qty_base_delta,NULL AS work_order_id
 FROM stock_entry e JOIN stock_posting p ON p.id=e.posting_id WHERE p.posting_state='POSTED'
 UNION ALL
 SELECT e.id,e.posting_id,e.return_line_id,'SUPPLIER_RETURN',e.position_id,e.qty_base_delta,NULL
 FROM supplier_return_entry e JOIN supplier_return_posting p ON p.id=e.posting_id WHERE p.posting_state='POSTED'
 UNION ALL
 SELECT e.id,e.posting_id,e.issue_line_id,'MATERIAL_ISSUE',e.position_id,e.qty_base_delta,h.work_order_id
 FROM material_issue_entry e JOIN material_issue_posting p ON p.id=e.posting_id
 JOIN material_issue h ON h.id=p.material_issue_id WHERE p.posting_state='POSTED'
"""


def decimal(column):
    value=foundation.decimal_check(column)
    value.name="ck_material_issue_"+column
    return value


def source_guard(document):
    return f"""
      SELECT RAISE(ABORT,'material_issue stale work order or source') WHERE NOT EXISTS(
       SELECT 1 FROM material_issue h JOIN work_order w ON w.id=h.work_order_id
       LEFT JOIN bom b ON b.id=w.bom_id LEFT JOIN routing r ON r.id=w.routing_id
       WHERE h.id={document} AND w.row_version=h.work_order_version
        AND w.bom_id IS h.bom_id AND b.row_version IS h.bom_version
        AND w.routing_id IS h.routing_id AND r.row_version IS h.routing_version);
    """


def quantity_guards(document):
    return f"""
      SELECT RAISE(ABORT,'material_issue controlled QA outward quantities must be positive') WHERE EXISTS(
       SELECT 1 FROM material_issue_line WHERE material_issue_id={document} AND erp_decimal_cmp(quantity,'0')<=0);
      SELECT RAISE(ABORT,'material_issue physical stock insufficient') WHERE EXISTS(
       SELECT 1 FROM material_issue_line line WHERE line.material_issue_id={document} GROUP BY line.position_id
       HAVING erp_decimal_cmp(erp_decimal_sum(line.quantity),
        COALESCE((SELECT erp_decimal_sum(qty_base_delta) FROM physical_stock_ledger
          WHERE position_id=line.position_id),'0'))>0);
    """


def extend_supplier_return_stock_guards():
    # Replace only the old balance expression; retain every origin/immutability
    # guard from 0006. Historical migration source files are never edited.
    con=op.get_bind()
    pattern=r"COALESCE\(\(SELECT erp_decimal_sum\(delta\) FROM \([\s\S]+?position\.id\)\),'0'\)"
    replacement="COALESCE((SELECT erp_decimal_sum(qty_base_delta) FROM physical_stock_ledger WHERE position_id=position.id),'0')"
    for name in ["supplier_return_posting_prepare","supplier_return_posting_finalize"]:
        sql=con.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).scalar_one()
        revised,count=re.subn(pattern,replacement,sql)
        if count!=1:
            raise RuntimeError("Expected one historical physical balance guard: "+name)
        op.execute("DROP TRIGGER "+name)
        op.execute(revised)


def upgrade():
    fk=foundation.fk
    op.create_table("material_issue",*foundation.audit(),
        sa.Column("document_no",sa.Text(),nullable=False,unique=True),
        sa.Column("work_order_id",sa.Text(),fk("work_order.id"),nullable=False),
        sa.Column("work_order_version",sa.Integer(),nullable=False),
        sa.Column("bom_id",sa.Text(),fk("bom.id")),sa.Column("bom_version",sa.Integer()),
        sa.Column("routing_id",sa.Text(),fk("routing.id")),sa.Column("routing_version",sa.Integer()),
        sa.Column("business_date",sa.Text(),nullable=False),sa.Column("note",sa.Text()),
        sa.CheckConstraint("trim(document_no)<>'' AND typeof(row_version)='integer' AND row_version>0"),
        foundation.date_check("business_date"))
    op.create_table("material_issue_line",*foundation.audit(),
        sa.Column("material_issue_id",sa.Text(),fk("material_issue.id"),nullable=False),
        sa.Column("line_no",sa.Integer(),nullable=False),
        sa.Column("material_id",sa.Text(),fk("item.id"),nullable=False),
        sa.Column("warehouse_id",sa.Text(),fk("warehouse.id"),nullable=False),
        sa.Column("position_id",sa.Text(),fk("stock_position.id"),nullable=False),
        sa.Column("unit_id",sa.Text(),fk("unit.id"),nullable=False),
        sa.Column("bom_component_id",sa.Text(),fk("bom_component.id")),
        sa.Column("bom_component_version",sa.Integer()),
        sa.Column("quantity",sa.Text(),nullable=False),sa.Column("note",sa.Text()),
        sa.UniqueConstraint("material_issue_id","line_no"),decimal("quantity"),
        sa.CheckConstraint("typeof(row_version)='integer' AND row_version>0"))
    op.create_table("material_issue_posting",*receiving.created(),
        sa.Column("material_issue_id",sa.Text(),fk("material_issue.id"),nullable=False,unique=True),
        sa.Column("idempotency_key",sa.Text(),nullable=False,unique=True),
        sa.Column("request_fingerprint",sa.Text(),nullable=False),
        sa.Column("source_version",sa.Integer(),nullable=False),
        sa.Column("work_order_version",sa.Integer(),nullable=False),
        sa.Column("posting_state",sa.Text(),nullable=False),
        sa.Column("authorization_basis",sa.Text(),nullable=False),
        sa.Column("posted_at",sa.Text(),nullable=False),sa.Column("posted_by",sa.Text(),fk("actor.id"),nullable=False),
        sa.CheckConstraint("posting_state IN ('PREPARING','POSTED') AND authorization_basis='CONTROLLED_QA_ONLY'"))
    op.create_table("material_issue_entry",*receiving.created(),
        sa.Column("posting_id",sa.Text(),fk("material_issue_posting.id"),nullable=False),
        sa.Column("issue_line_id",sa.Text(),fk("material_issue_line.id"),nullable=False),
        sa.Column("position_id",sa.Text(),fk("stock_position.id"),nullable=False),
        sa.Column("leg_no",sa.Integer(),nullable=False),
        sa.Column("qty_base_delta",sa.Text(),nullable=False),decimal("qty_base_delta"),
        sa.UniqueConstraint("issue_line_id","leg_no"),
        sa.CheckConstraint("qty_base_delta LIKE '-%' AND leg_no=1"))
    for table,columns in {
        "material_issue":["work_order_id","bom_id","routing_id","created_by"],
        "material_issue_line":["material_issue_id","material_id","warehouse_id","position_id","unit_id","bom_component_id","created_by"],
        "material_issue_posting":["material_issue_id","created_by","posted_by"],
        "material_issue_entry":["posting_id","issue_line_id","position_id","created_by"]}.items():
        for column in columns:
            op.create_index("ix_"+table+"_"+column,table,[column])
    op.execute(LEDGER_VIEW)
    extend_supplier_return_stock_guards()
    for action in ["INSERT","UPDATE"]:
        op.execute(f"""CREATE TRIGGER material_issue_refs_{action.lower()} BEFORE {action} ON material_issue BEGIN
          SELECT RAISE(ABORT,'material_issue source mismatch') WHERE NOT EXISTS(
           SELECT 1 FROM work_order w LEFT JOIN bom b ON b.id=w.bom_id LEFT JOIN routing r ON r.id=w.routing_id
           WHERE w.id=NEW.work_order_id AND w.row_version=NEW.work_order_version
            AND w.bom_id IS NEW.bom_id AND b.row_version IS NEW.bom_version
            AND w.routing_id IS NEW.routing_id AND r.row_version IS NEW.routing_version); END""")
        op.execute(f"""CREATE TRIGGER material_issue_line_refs_{action.lower()} BEFORE {action} ON material_issue_line BEGIN
          SELECT RAISE(ABORT,'material_issue material warehouse position unit mismatch') WHERE NOT EXISTS(
           SELECT 1 FROM item i JOIN stock_position p ON p.item_id=i.id
           WHERE i.id=NEW.material_id AND i.item_kind='RAW' AND i.base_unit_id=NEW.unit_id
            AND p.id=NEW.position_id AND p.warehouse_id=NEW.warehouse_id);
          SELECT RAISE(ABORT,'material_issue component mismatch') WHERE
           (NEW.bom_component_id IS NULL AND NEW.bom_component_version IS NOT NULL) OR
           (NEW.bom_component_id IS NOT NULL AND NOT EXISTS(
             SELECT 1 FROM bom_component c JOIN material_issue h ON h.bom_id=c.bom_id
             WHERE h.id=NEW.material_issue_id AND c.id=NEW.bom_component_id
              AND c.material_id=NEW.material_id AND c.row_version=NEW.bom_component_version)); END""")
    op.execute("""CREATE TRIGGER material_issue_work_order_hold BEFORE UPDATE ON work_order
      WHEN EXISTS(SELECT 1 FROM material_issue WHERE work_order_id=OLD.id)
      BEGIN SELECT RAISE(ABORT,'material_issue referenced work_order held'); END""")
    op.execute("""CREATE TRIGGER material_issue_work_order_change BEFORE UPDATE OF work_order_id ON material_issue
      WHEN NEW.work_order_id<>OLD.work_order_id AND EXISTS(SELECT 1 FROM material_issue_line WHERE material_issue_id=OLD.id)
      BEGIN SELECT RAISE(ABORT,'material_issue cannot reassign work order with lines'); END""")
    op.execute("""CREATE TRIGGER material_issue_line_no_reparent BEFORE UPDATE OF material_issue_id ON material_issue_line
      WHEN NEW.material_issue_id<>OLD.material_issue_id
      BEGIN SELECT RAISE(ABORT,'material_issue cannot reparent line'); END""")
    op.execute("""CREATE TRIGGER material_issue_item_basis BEFORE UPDATE OF id,code,item_kind,base_unit_id,lot_tracking ON item
      WHEN (NEW.id<>OLD.id OR NEW.code<>OLD.code OR NEW.item_kind<>OLD.item_kind OR NEW.base_unit_id<>OLD.base_unit_id OR NEW.lot_tracking<>OLD.lot_tracking)
       AND EXISTS(SELECT 1 FROM material_issue_line WHERE material_id=OLD.id)
      BEGIN SELECT RAISE(ABORT,'material_issue material identity unit protected'); END""")
    for table,extra in {
        "material_issue":"OR document_no=NEW.document_no",
        "material_issue_line":"OR (material_issue_id=NEW.material_issue_id AND line_no=NEW.line_no)",
        "material_issue_posting":"OR material_issue_id=NEW.material_issue_id OR idempotency_key=NEW.idempotency_key",
        "material_issue_entry":"OR (issue_line_id=NEW.issue_line_id AND leg_no=NEW.leg_no)"}.items():
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id OR rowid=NEW.rowid {extra})
          BEGIN SELECT RAISE(ABORT,'material_issue existing identity cannot be replaced'); END""")
    for table,condition in [
        ("material_issue","document_no=NEW.document_no"),
        ("material_issue_line","material_issue_id=NEW.material_issue_id AND line_no=NEW.line_no")]:
        op.execute(f"""CREATE TRIGGER {table}_update_no_replace BEFORE UPDATE ON {table}
          WHEN EXISTS(SELECT 1 FROM {table} WHERE ({condition} OR rowid=NEW.rowid) AND id<>OLD.id)
          BEGIN SELECT RAISE(ABORT,'material_issue existing identity cannot be replaced'); END""")
        op.execute(f"""CREATE TRIGGER {table}_audit BEFORE UPDATE ON {table}
          WHEN NEW.id<>OLD.id OR NEW.rowid<>OLD.rowid OR NEW.created_by<>OLD.created_by OR NEW.created_at<>OLD.created_at
          BEGIN SELECT RAISE(ABORT,'material_issue creation audit immutable'); END""")
    for table,condition in [("material_issue","material_issue_id=OLD.id"),
                             ("material_issue_line","material_issue_id=OLD.material_issue_id")]:
        for action in ["UPDATE","DELETE"]:
            op.execute(f"""CREATE TRIGGER {table}_posted_{action.lower()} BEFORE {action} ON {table}
              WHEN EXISTS(SELECT 1 FROM material_issue_posting WHERE {condition})
              BEGIN SELECT RAISE(ABORT,'material_issue posted history immutable'); END""")
    op.execute("""CREATE TRIGGER material_issue_line_posted_insert BEFORE INSERT ON material_issue_line
      WHEN EXISTS(SELECT 1 FROM material_issue_posting WHERE material_issue_id=NEW.material_issue_id)
      BEGIN SELECT RAISE(ABORT,'material_issue posted history immutable'); END""")
    for action in ["UPDATE","DELETE"]:
        op.execute(f"""CREATE TRIGGER material_issue_entry_immutable_{action.lower()} BEFORE {action} ON material_issue_entry
          BEGIN SELECT RAISE(ABORT,'material_issue entry immutable'); END""")
    op.execute("""CREATE TRIGGER material_issue_posting_immutable_delete BEFORE DELETE ON material_issue_posting
      BEGIN SELECT RAISE(ABORT,'material_issue posting immutable'); END""")
    for other in ["stock_posting","supplier_return_posting"]:
        op.execute(f"""CREATE TRIGGER material_issue_key_{other} BEFORE INSERT ON material_issue_posting
          WHEN EXISTS(SELECT 1 FROM {other} WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'material_issue posting key belongs to another domain'); END""")
        op.execute(f"""CREATE TRIGGER {other}_material_issue_key BEFORE INSERT ON {other}
          WHEN EXISTS(SELECT 1 FROM material_issue_posting WHERE idempotency_key=NEW.idempotency_key)
          BEGIN SELECT RAISE(ABORT,'posting key belongs to material_issue'); END""")
    op.execute("""CREATE TRIGGER material_issue_posting_prepare BEFORE INSERT ON material_issue_posting BEGIN
      SELECT RAISE(ABORT,'material_issue policy pending') WHERE erp_issue_test_authorized()<>1;
      SELECT RAISE(ABORT,'material_issue must prepare') WHERE NEW.posting_state<>'PREPARING';
      SELECT RAISE(ABORT,'material_issue invalid document version') WHERE NOT EXISTS(
       SELECT 1 FROM material_issue h WHERE h.id=NEW.material_issue_id AND h.row_version=NEW.source_version+1
        AND h.work_order_version=NEW.work_order_version);
      SELECT RAISE(ABORT,'material_issue empty document') WHERE NOT EXISTS(
       SELECT 1 FROM material_issue_line WHERE material_issue_id=NEW.material_issue_id);
      """+source_guard("NEW.material_issue_id")+quantity_guards("NEW.material_issue_id")+" END")
    op.execute("""CREATE TRIGGER material_issue_entry_outbound BEFORE INSERT ON material_issue_entry
      WHEN erp_issue_test_authorized()<>1 OR NOT EXISTS(
       SELECT 1 FROM material_issue_posting p JOIN material_issue_line l ON l.material_issue_id=p.material_issue_id
       WHERE p.id=NEW.posting_id AND p.posting_state='PREPARING' AND l.id=NEW.issue_line_id
        AND l.position_id=NEW.position_id AND NEW.leg_no=1 AND erp_decimal_cmp(l.quantity,'0')>0
        AND NEW.qty_base_delta=erp_decimal_neg(l.quantity) AND NEW.created_by=p.created_by)
      BEGIN SELECT RAISE(ABORT,'material_issue invalid outbound movement or policy pending'); END""")
    unchanged=" AND ".join("NEW."+field+" IS OLD."+field for field in [
        "id","rowid","material_issue_id","idempotency_key","request_fingerprint","source_version",
        "work_order_version","authorization_basis","posted_at","posted_by","created_at","created_by"])
    op.execute(f"""CREATE TRIGGER material_issue_posting_finalize BEFORE UPDATE ON material_issue_posting BEGIN
      SELECT RAISE(ABORT,'material_issue policy pending') WHERE erp_issue_test_authorized()<>1;
      SELECT RAISE(ABORT,'material_issue posting immutable') WHERE NOT
       (OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {unchanged});
      SELECT RAISE(ABORT,'material_issue incomplete movements') WHERE EXISTS(
       SELECT 1 FROM material_issue_line l WHERE l.material_issue_id=OLD.material_issue_id AND NOT EXISTS(
        SELECT 1 FROM material_issue_entry e WHERE e.posting_id=OLD.id AND e.issue_line_id=l.id AND e.leg_no=1))
        OR (SELECT count(*) FROM material_issue_entry WHERE posting_id=OLD.id)<>
           (SELECT count(*) FROM material_issue_line WHERE material_issue_id=OLD.material_issue_id);
      """+source_guard("OLD.material_issue_id")+quantity_guards("OLD.material_issue_id")+" END")


def downgrade():
    raise RuntimeError("Material-issue downgrade is disabled; use an explicitly verified pre-migration backup recovery.")
