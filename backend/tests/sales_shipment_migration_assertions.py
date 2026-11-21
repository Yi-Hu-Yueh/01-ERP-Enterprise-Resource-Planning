"""Only one existing view may change in the additive shipment migration."""
from backend.tests.transfer_migration_assertions import (NEW_TABLES as TRANSFER_TABLES, without_transfer_ledger_branch, without_transfer_position_extension)
from backend.tests.customer_return_migration_assertions import assert_customer_return_ledger_branch
from backend.tests.stocktake_migration_assertions import NEW_TABLES as STOCKTAKE_TABLES, assert_stocktake_ledger_branch
from backend.tests.adjustment_migration_assertions import NEW_TABLES as ADJUSTMENT_TABLES, assert_adjustment_ledger_branch


def compact(sql):
    return "".join(sql.strip().rstrip(";").split())


def assert_sales_shipment_upgrade_objects(before, after):
    objects = {(obj[0], obj[1]): obj for obj in after["objects"]}
    for original in before["objects"]:
        kind, name, table, sql = original
        current = objects[(kind, name)]
        if name == "stock_position_identity" and "erp_transfer_" not in sql:
            assert current[:3] == original[:3]
            assert compact(without_transfer_position_extension(current[3])) == compact(sql)
            continue
        if name != "physical_stock_ledger":
            assert current == original, "Unrelated existing object changed: " + name
            continue
        assert current[:3] == original[:3]
        assert compact(current[3]).startswith(compact(sql)), "Previous ledger branches changed"
        if "'INVENTORY_ADJUSTMENT'" in current[3]:
            assert_adjustment_ledger_branch(current[3])
        if "'STOCKTAKE'" in current[3]:
            assert_stocktake_ledger_branch(current[3])
        if "'CUSTOMER_RETURN'" in current[3]:
            assert_customer_return_ledger_branch(current[3])
        assert current[3].count("'SALES_SHIPMENT'") == 1
        for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN", "FINISHED_GOODS_RECEIPT"]:
            assert current[3].count("'" + domain + "'") == 1
        suffix = compact(current[3]).split("'SALES_SHIPMENT'", 1)[1]
        for required in ["e.position_id,e.qty_base_delta,NULL", "FROMsales_shipment_entrye",
                         "JOINsales_shipment_postingpONp.id=e.posting_id",
                         "JOINsales_shipmentrONr.id=p.shipment_id", "WHEREp.posting_state='POSTED'"]:
            assert required in suffix
    expected_tables = {"sales_shipment", "sales_shipment_posting", "sales_shipment_entry"}
    customer_tables = {"customer_return", "customer_return_finalization", "customer_return_entry"}
    for following in [customer_tables, STOCKTAKE_TABLES, ADJUSTMENT_TABLES, TRANSFER_TABLES]:
        if following.intersection(after["rows"]):
            assert following.issubset(after["rows"]), "Incomplete later inventory schema"
            expected_tables |= following
    assert set(after["rows"])-set(before["rows"]) == expected_tables
    for table, rows in before["rows"].items():
        if table != "alembic_version":
            assert after["rows"][table] == rows, "Old rows changed: " + table
