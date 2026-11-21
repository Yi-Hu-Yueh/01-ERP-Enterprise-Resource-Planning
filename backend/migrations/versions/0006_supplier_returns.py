"""Add typed supplier returns without rebuilding accepted receipt/history tables."""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "0006_supplier_returns"
down_revision = "0005_purchase_receiving"
branch_labels = None
depends_on = None

foundation = import_module("backend.migrations.versions.0004_purchase_orders")
receiving = import_module("backend.migrations.versions.0005_purchase_receiving")


def decimal(column):
    value = foundation.decimal_check(column)
    value.name = "ck_supplier_return_"+column
    return value


def quantity_guards(document, origin_alias="origin"):
    # Reused at preparation and finalization; never SQLite SUM/REAL coercion.
    prior_returned = """COALESCE((SELECT erp_decimal_sum(previous.quantity)
        FROM supplier_return_line previous JOIN supplier_return_posting posted ON posted.supplier_return_id=previous.supplier_return_id
        WHERE posted.posting_state='POSTED' AND previous.origin_line_id=origin.id),'0')"""
    remaining = f"""(SELECT erp_decimal_sum(value) FROM
        (SELECT origin.quantity AS value UNION ALL SELECT erp_decimal_neg({prior_returned}) AS value))"""
    balance = """COALESCE((SELECT erp_decimal_sum(delta) FROM (
        SELECT e.qty_base_delta AS delta FROM stock_entry e JOIN stock_posting p ON p.id=e.posting_id
          WHERE p.posting_state='POSTED' AND e.position_id=position.id
        UNION ALL SELECT e.qty_base_delta AS delta FROM supplier_return_entry e JOIN supplier_return_posting p ON p.id=e.posting_id
          WHERE p.posting_state='POSTED' AND e.position_id=position.id)),'0')"""
    return f"""
        SELECT RAISE(ABORT,'supplier_return source quantity exceeded') WHERE EXISTS
          (SELECT 1 FROM supplier_return_line line JOIN stock_document_line origin ON origin.id=line.origin_line_id
           WHERE line.supplier_return_id={document} GROUP BY origin.id
           HAVING erp_decimal_cmp(erp_decimal_sum(line.quantity),{remaining})>0);
        SELECT RAISE(ABORT,'supplier_return physical stock insufficient') WHERE EXISTS
          (SELECT 1 FROM supplier_return_line line JOIN stock_document_line origin ON origin.id=line.origin_line_id
           JOIN stock_position position ON position.item_id=origin.item_id AND position.warehouse_id=origin.warehouse_id
             AND position.lot_id IS origin.lot_id
           WHERE line.supplier_return_id={document} GROUP BY position.id
           HAVING erp_decimal_cmp(erp_decimal_sum(line.quantity),{balance})>0);
        """


def upgrade():
    fk = receiving.fk
    op.create_table("supplier_return", *receiving.audit(),
        sa.Column("document_no", sa.Text(), nullable=False, unique=True),
        sa.Column("supplier_id", sa.Text(), fk("supplier.id"), nullable=False),
        sa.Column("party_name_snapshot", sa.Text(), nullable=False),
        sa.Column("business_date", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text()), sa.Column("external_reference", sa.Text()), sa.Column("note", sa.Text()),
        sa.CheckConstraint("trim(document_no)<>''"), foundation.date_check("business_date"))
    op.create_table("supplier_return_line", *receiving.audit(),
        sa.Column("supplier_return_id", sa.Text(), fk("supplier_return.id"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("origin_line_id", sa.Text(), fk("stock_document_line.id"), nullable=False),
        sa.Column("origin_line_version", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Text(), nullable=False), sa.Column("note", sa.Text()),
        sa.UniqueConstraint("supplier_return_id", "line_no"), decimal("quantity"))
    op.create_table("supplier_return_posting", *receiving.created(),
        sa.Column("supplier_return_id", sa.Text(), fk("supplier_return.id"), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.Text(), nullable=False), sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("posting_state", sa.Text(), nullable=False), sa.Column("posted_at", sa.Text(), nullable=False),
        sa.Column("posted_by", sa.Text(), fk("actor.id"), nullable=False),
        sa.CheckConstraint("posting_state IN ('PREPARING','POSTED')"))
    op.create_table("supplier_return_entry", *receiving.created(),
        sa.Column("posting_id", sa.Text(), fk("supplier_return_posting.id"), nullable=False),
        sa.Column("return_line_id", sa.Text(), fk("supplier_return_line.id"), nullable=False),
        sa.Column("position_id", sa.Text(), fk("stock_position.id"), nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False), sa.Column("qty_base_delta", sa.Text(), nullable=False),
        sa.UniqueConstraint("return_line_id", "leg_no"), decimal("qty_base_delta"),
        sa.CheckConstraint("qty_base_delta LIKE '-%'", name="ck_supplier_return_outbound"))
    for table, fields in {"supplier_return": ["supplier_id", "created_by"],
        "supplier_return_line": ["supplier_return_id", "origin_line_id", "created_by"],
        "supplier_return_posting": ["supplier_return_id", "posted_by", "created_by"],
        "supplier_return_entry": ["posting_id", "return_line_id", "position_id", "created_by"]}.items():
        for field in fields:
            op.create_index("ix_"+table+"_"+field, table, [field])
    for action in ["INSERT", "UPDATE"]:
        op.execute(f"""CREATE TRIGGER supplier_return_line_source_{action.lower()} BEFORE {action} ON supplier_return_line
          WHEN NOT EXISTS(SELECT 1 FROM stock_document_line origin
            JOIN stock_document receipt ON receipt.id=origin.stock_document_id
            JOIN stock_posting posting ON posting.stock_document_id=receipt.id AND posting.posting_state='POSTED'
            JOIN supplier_return document ON document.id=NEW.supplier_return_id AND document.supplier_id=receipt.supplier_id
            JOIN item material ON material.id=origin.item_id AND material.item_kind='RAW' AND material.base_unit_id=origin.unit_id
            JOIN stock_entry inbound ON inbound.document_line_id=origin.id AND inbound.posting_id=posting.id AND inbound.leg_no=1
            JOIN stock_position position ON position.id=inbound.position_id AND position.item_id=origin.item_id
              AND position.warehouse_id=origin.warehouse_id AND position.lot_id IS origin.lot_id
            WHERE origin.id=NEW.origin_line_id AND origin.row_version=NEW.origin_line_version
              AND origin.base_factor_snapshot='1' AND inbound.qty_base_delta=origin.quantity AND receipt.document_kind='PURCHASE_RECEIPT')
          BEGIN SELECT RAISE(ABORT,'supplier_return source must be a matching posted receipt line'); END""")
    op.execute("""CREATE TRIGGER supplier_return_line_no_reparent BEFORE UPDATE OF supplier_return_id ON supplier_return_line
        WHEN NEW.supplier_return_id<>OLD.supplier_return_id BEGIN SELECT RAISE(ABORT,'supplier_return cannot reparent line'); END""")
    op.execute("""CREATE TRIGGER supplier_return_supplier_frozen BEFORE UPDATE OF supplier_id ON supplier_return
        WHEN NEW.supplier_id<>OLD.supplier_id AND EXISTS(SELECT 1 FROM supplier_return_line WHERE supplier_return_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'supplier_return supplier has source lines'); END""")
    for table, condition in [("supplier_return", "supplier_return_id=OLD.id"),
                             ("supplier_return_line", "supplier_return_id=OLD.supplier_return_id")]:
        for action in ["UPDATE", "DELETE"]:
            op.execute(f"""CREATE TRIGGER {table}_posted_{action.lower()} BEFORE {action} ON {table}
                WHEN EXISTS(SELECT 1 FROM supplier_return_posting WHERE {condition})
                BEGIN SELECT RAISE(ABORT,'supplier_return posted history immutable'); END""")
    op.execute("""CREATE TRIGGER supplier_return_line_posted_insert BEFORE INSERT ON supplier_return_line
        WHEN EXISTS(SELECT 1 FROM supplier_return_posting WHERE supplier_return_id=NEW.supplier_return_id)
        BEGIN SELECT RAISE(ABORT,'supplier_return posted history immutable'); END""")
    for action in ["UPDATE", "DELETE"]:
        op.execute(f"""CREATE TRIGGER supplier_return_entry_immutable_{action.lower()} BEFORE {action} ON supplier_return_entry
          BEGIN SELECT RAISE(ABORT,'supplier_return entry immutable'); END""")
    op.execute("""CREATE TRIGGER supplier_return_posting_immutable_delete BEFORE DELETE ON supplier_return_posting
        BEGIN SELECT RAISE(ABORT,'supplier_return posting immutable'); END""")
    # Global posting-key exclusion across the two typed domains, inside the write transaction.
    op.execute("""CREATE TRIGGER supplier_return_posting_cross_key BEFORE INSERT ON supplier_return_posting
        WHEN EXISTS(SELECT 1 FROM stock_posting WHERE idempotency_key=NEW.idempotency_key)
        BEGIN SELECT RAISE(ABORT,'supplier_return posting key belongs to inbound receipt'); END""")
    op.execute("""CREATE TRIGGER stock_posting_return_cross_key BEFORE INSERT ON stock_posting
        WHEN EXISTS(SELECT 1 FROM supplier_return_posting WHERE idempotency_key=NEW.idempotency_key)
        BEGIN SELECT RAISE(ABORT,'stock_posting key belongs to supplier_return'); END""")
    op.execute("""CREATE TRIGGER supplier_return_posting_prepare BEFORE INSERT ON supplier_return_posting BEGIN
        SELECT RAISE(ABORT,'supplier_return posting must prepare') WHERE NEW.posting_state<>'PREPARING';
        SELECT RAISE(ABORT,'supplier_return invalid document version') WHERE NOT EXISTS
          (SELECT 1 FROM supplier_return d WHERE d.id=NEW.supplier_return_id AND d.row_version=NEW.source_version+1);
        SELECT RAISE(ABORT,'supplier_return empty document') WHERE NOT EXISTS
          (SELECT 1 FROM supplier_return_line WHERE supplier_return_id=NEW.supplier_return_id);
        SELECT RAISE(ABORT,'supplier_return quantity must be outward positive') WHERE EXISTS
          (SELECT 1 FROM supplier_return_line WHERE supplier_return_id=NEW.supplier_return_id AND erp_decimal_cmp(quantity,'0')<=0);
        SELECT RAISE(ABORT,'supplier_return invalid source ledger') WHERE EXISTS
          (SELECT 1 FROM supplier_return_line line JOIN stock_document_line origin ON origin.id=line.origin_line_id
           JOIN stock_document receipt ON receipt.id=origin.stock_document_id
           JOIN supplier_return document ON document.id=line.supplier_return_id
           WHERE line.supplier_return_id=NEW.supplier_return_id AND
             (line.origin_line_version<>origin.row_version OR document.supplier_id<>receipt.supplier_id
              OR NOT EXISTS(SELECT 1 FROM stock_posting p JOIN stock_entry e ON e.posting_id=p.id
                JOIN stock_position position ON position.id=e.position_id
                WHERE p.stock_document_id=receipt.id AND p.posting_state='POSTED' AND e.document_line_id=origin.id
                  AND e.leg_no=1 AND e.qty_base_delta=origin.quantity AND position.item_id=origin.item_id
                  AND position.warehouse_id=origin.warehouse_id AND position.lot_id IS origin.lot_id)));
        """+quantity_guards("NEW.supplier_return_id")+" END")
    op.execute("""CREATE TRIGGER supplier_return_entry_outbound BEFORE INSERT ON supplier_return_entry
        WHEN NOT EXISTS(SELECT 1 FROM supplier_return_posting posting
          JOIN supplier_return_line line ON line.supplier_return_id=posting.supplier_return_id
          JOIN stock_document_line origin ON origin.id=line.origin_line_id
          JOIN stock_position position ON position.id=NEW.position_id
          WHERE posting.id=NEW.posting_id AND posting.posting_state='PREPARING' AND line.id=NEW.return_line_id
            AND position.item_id=origin.item_id AND position.warehouse_id=origin.warehouse_id AND position.lot_id IS origin.lot_id
            AND NEW.leg_no=1 AND origin.base_factor_snapshot='1' AND erp_decimal_cmp(line.quantity,'0')>0
            AND NEW.qty_base_delta=erp_decimal_neg(line.quantity))
        BEGIN SELECT RAISE(ABORT,'supplier_return invalid outbound movement'); END""")
    unchanged = " AND ".join("NEW."+field+" IS OLD."+field for field in ["id", "supplier_return_id", "idempotency_key",
        "request_fingerprint", "source_version", "posted_at", "posted_by", "created_at", "created_by"])
    op.execute(f"""CREATE TRIGGER supplier_return_posting_finalize BEFORE UPDATE ON supplier_return_posting BEGIN
        SELECT RAISE(ABORT,'supplier_return posting immutable') WHERE NOT
          (OLD.posting_state='PREPARING' AND NEW.posting_state='POSTED' AND {unchanged});
        SELECT RAISE(ABORT,'supplier_return incomplete movements') WHERE EXISTS
          (SELECT 1 FROM supplier_return_line line WHERE line.supplier_return_id=OLD.supplier_return_id AND NOT EXISTS
            (SELECT 1 FROM supplier_return_entry e WHERE e.posting_id=OLD.id AND e.return_line_id=line.id AND e.leg_no=1))
          OR (SELECT count(*) FROM supplier_return_entry WHERE posting_id=OLD.id)<>
            (SELECT count(*) FROM supplier_return_line WHERE supplier_return_id=OLD.supplier_return_id);
        """+quantity_guards("OLD.supplier_return_id")+" END")


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled. Preserve a verified backup and plan an explicit migration.")
