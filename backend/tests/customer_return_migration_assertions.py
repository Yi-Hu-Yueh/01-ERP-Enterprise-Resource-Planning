"""The return migration may append only its new branch to the existing ledger."""
from backend.tests.transfer_migration_assertions import (NEW_TABLES as TRANSFER_TABLES, without_transfer_ledger_branch, without_transfer_position_extension)
from backend.tests.stocktake_migration_assertions import NEW_TABLES as STOCKTAKE_TABLES, without_stocktake_ledger_branch
from backend.tests.adjustment_migration_assertions import NEW_TABLES as ADJUSTMENT_TABLES


def compact(sql):
    return "".join(sql.strip().rstrip(";").split())


def assert_customer_return_ledger_branch(sql):
    sql = without_transfer_ledger_branch(sql)
    sql = without_stocktake_ledger_branch(sql)
    assert sql.count("'CUSTOMER_RETURN'") == 1
    expected = """,e.position_id,e.qty_base_delta,NULL
      FROM customer_return_entry e JOIN customer_return_finalization f ON f.id=e.finalization_id
      JOIN customer_return r ON r.id=f.return_id
      WHERE f.finalization_state='POSTED' AND r.restock_eligible=1"""
    assert compact(sql).split("'CUSTOMER_RETURN'", 1)[1] == compact(expected), "Customer return ledger branch changed"


def assert_customer_return_upgrade_objects(before, after):
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
        assert_customer_return_ledger_branch(current[3])
        for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN",
                       "FINISHED_GOODS_RECEIPT", "SALES_SHIPMENT"]:
            assert current[3].count("'" + domain + "'") == 1
        suffix = compact(current[3]).split("'CUSTOMER_RETURN'", 1)[1]
        for required in ["e.position_id,e.qty_base_delta,NULL", "FROMcustomer_return_entrye",
                         "JOINcustomer_return_finalizationfONf.id=e.finalization_id",
                         "JOINcustomer_returnrONr.id=f.return_id",
                         "WHEREf.finalization_state='POSTED'ANDr.restock_eligible=1"]:
            assert required in suffix
    expected_tables = {"customer_return", "customer_return_finalization", "customer_return_entry"}
    for following in [STOCKTAKE_TABLES, ADJUSTMENT_TABLES, TRANSFER_TABLES]:
        if following.intersection(after["rows"]):
            assert following.issubset(after["rows"]), "Incomplete later inventory schema"
            expected_tables |= following
    assert set(after["rows"]) - set(before["rows"]) == expected_tables
    for table, rows in before["rows"].items():
        if table != "alembic_version":
            assert after["rows"][table] == rows, "Old rows changed: " + table
