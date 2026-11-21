"""Narrowly validate the four intentional finished-goods infrastructure extensions."""
from backend.tests.transfer_migration_assertions import (NEW_TABLES as TRANSFER_TABLES, without_transfer_ledger_branch, without_transfer_position_extension)
import re
from backend.tests.customer_return_migration_assertions import assert_customer_return_ledger_branch
from backend.tests.stocktake_migration_assertions import assert_stocktake_ledger_branch
from backend.tests.adjustment_migration_assertions import assert_adjustment_ledger_branch


def compact(sql):
    return "".join(sql.split())


def table_sql(sql):
    # SQLite quotes the name when the atomic replacement is renamed.
    return compact(sql.replace('CREATE TABLE "inventory_lot"', 'CREATE TABLE inventory_lot'))


def assert_finished_goods_upgrade_objects(before, after, exclude=()):
    objects = {(obj[0], obj[1]): obj for obj in after["objects"]}
    excluded = set(exclude)
    intentional = {"inventory_lot", "inventory_lot_raw", "stock_position_identity", "physical_stock_ledger"}
    for original in before["objects"]:
        kind, name, table, sql = original
        if name in excluded:
            continue
        if name not in intentional:
            assert original in after["objects"], "Unexpected changed/deleted original schema object: " + name
            continue
        changed = objects[(kind, name)]
        assert changed[:3] == original[:3]
        current = changed[3]
        if name == "stock_position_identity":
            current = without_transfer_position_extension(current)
        if name == "inventory_lot":
            # Supplier becomes nullable only so a manufactured lot need not
            # fabricate a supplier. Every other column/key/date check stays.
            expected, count = re.subn(r"(?m)^(\s*supplier_id\s+TEXT)\s+NOT NULL(?=,)", r"\1", sql)
            assert count == 1
            assert expected.count("origin_kind='PURCHASED'") == 1
            expected = expected.replace("origin_kind='PURCHASED'", """((origin_kind='PURCHASED' AND supplier_id IS NOT NULL)
              OR (origin_kind='MANUFACTURED' AND supplier_id IS NULL AND supplier_lot_code IS NULL))""")
            assert table_sql(current) == table_sql(expected), "Unrelated inventory-lot definition changed"
            assert "manufactured_dateISNULL" not in compact(current).split("origin_kind='MANUFACTURED'", 1)[1].split(")", 1)[0]
            assert "expiry_dateISNULL" not in compact(current).split("origin_kind='MANUFACTURED'", 1)[1].split(")", 1)[0]
        elif name == "physical_stock_ledger":
            old_branch = compact(sql.strip().rstrip(";"))
            revised = compact(current)
            assert revised.startswith(old_branch), "Original stock-ledger branches changed"
            assert current.count("'FINISHED_GOODS_RECEIPT'") == 1
            for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN"]:
                if "'" + domain + "'" in sql:
                    assert current.count("'" + domain + "'") == 1
            # Operation22 appends one exact negative-sales branch after the unchanged FG branch.
            if "'SALES_SHIPMENT'" in current:
                assert current.count("'SALES_SHIPMENT'") == 1
                sales_branch = revised.split("'SALES_SHIPMENT'", 1)[1]
                for required in ["e.position_id,e.qty_base_delta,NULL", "FROMsales_shipment_entrye",
                                 "JOINsales_shipment_postingpONp.id=e.posting_id",
                                 "JOINsales_shipmentrONr.id=p.shipment_id", "WHEREp.posting_state='POSTED'"]:
                    assert required in sales_branch
            # Operation23 appends a disposition-gated positive branch; all earlier checks stay.
            if "'CUSTOMER_RETURN'" in current:
                assert_customer_return_ledger_branch(current)
            # Operation25 adds only its exact posted-and-closed stocktake tail.
            if "'STOCKTAKE'" in current:
                assert_stocktake_ledger_branch(current)
            if "'INVENTORY_ADJUSTMENT'" in current:
                assert_adjustment_ledger_branch(current)
            branch = revised.split("'FINISHED_GOODS_RECEIPT'", 1)[1]
            for required in ["e.position_id,e.qty_base_delta,c.work_order_id", "FROMfinished_goods_entrye",
                             "JOINfinished_goods_postingpONp.id=e.posting_id",
                             "JOINfinished_goods_receiptrONr.id=p.receipt_id",
                             "JOINproduction_completioncONc.id=r.completion_id", "WHEREp.posting_state='POSTED'"]:
                assert required in branch
        else:
            revised = compact(current)
            # The old RAW identity predicate must remain an unchanged alternative.
            old_predicate = sql.split("WHEN NOT ", 1)[1].split("BEGIN", 1)[0].strip()
            assert compact(old_predicate) in revised, "Original RAW lot/position predicate changed"
            for required in ["erp_finished_goods_authorized(),0)=1", "erp_finished_goods_document()ISr.id",
                             "c.state='CONFIRMED'", "c.row_version=r.completion_version",
                             "i.item_kind='FINISHED'", "i.base_unit_id=c.unit_id",
                             "erp_decimal_cmp(r.quantity,'0')>0"]:
                assert required in revised
            if name == "inventory_lot_raw":
                for required in ["NEW.origin_kind='PURCHASED'", "NEW.origin_kind='MANUFACTURED'",
                                 "NEW.supplier_idISNULL", "NEW.supplier_lot_codeISNULL",
                                 "i.lot_tracking=1", "NEW.item_id=c.product_id", "NEW.lot_code=r.lot_code"]:
                    assert required in revised
            else:
                for required in ["NEW.item_id=c.product_id", "NEW.warehouse_id=r.warehouse_id",
                                 "NEW.lot_idISr.lot_id", "i.lot_tracking=0ANDr.lot_idISNULLANDr.lot_codeISNULL",
                                 "lot.origin_kind='MANUFACTURED'", "lot.supplier_idISNULL"]:
                    assert required in revised
