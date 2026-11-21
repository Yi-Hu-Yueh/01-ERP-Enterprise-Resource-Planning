"""A separate adjustment document adds one exact authoritative-ledger branch."""
from backend.tests.transfer_migration_assertions import (NEW_TABLES as TRANSFER_TABLES, without_transfer_ledger_branch, without_transfer_position_extension)

NEW_TABLES = {"inventory_adjustment", "inventory_adjustment_posting", "inventory_adjustment_entry"}


def compact(sql):
    return "".join(sql.strip().rstrip(";").split())


def assert_adjustment_ledger_branch(sql):
    sql = without_transfer_ledger_branch(sql)
    assert sql.count("'INVENTORY_ADJUSTMENT'") == 1
    suffix = """,e.position_id,e.qty_base_delta,NULL
      FROM inventory_adjustment_entry e JOIN inventory_adjustment_posting p ON p.id=e.posting_id
      JOIN inventory_adjustment a ON a.id=e.adjustment_id AND a.id=p.adjustment_id
      WHERE p.posting_state='POSTED' AND a.state='POSTED'"""
    assert compact(sql).split("'INVENTORY_ADJUSTMENT'", 1)[1] == compact(suffix), "Adjustment ledger branch changed"


def without_adjustment_ledger_branch(sql):
    sql = without_transfer_ledger_branch(sql)
    if "'INVENTORY_ADJUSTMENT'" not in sql:
        return sql
    assert_adjustment_ledger_branch(sql)
    marker = compact("UNION ALL SELECT e.id,e.posting_id,e.adjustment_id,'INVENTORY_ADJUSTMENT'")
    normalized = compact(sql)
    assert normalized.count(marker) == 1
    return normalized.rsplit(marker, 1)[0]


def assert_adjustment_upgrade_objects(before, after):
    objects = {(obj[0], obj[1]): obj for obj in after["objects"]}
    for original in before["objects"]:
        kind, name, table, sql = original
        current = objects[(kind, name)]
        if name == "stock_position_identity" and "erp_transfer_" not in sql:
            assert current[:3] == original[:3]
            assert compact(without_transfer_position_extension(current[3])) == compact(sql)
            continue
        if name != "physical_stock_ledger":
            assert current == original, "Unrelated original schema object changed: " + name
            continue
        assert current[:3] == original[:3]
        assert compact(current[3]).startswith(compact(sql)), "Previous physical-ledger branches changed"
        assert_adjustment_ledger_branch(current[3])
        for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN",
                       "FINISHED_GOODS_RECEIPT", "SALES_SHIPMENT", "CUSTOMER_RETURN", "STOCKTAKE"]:
            assert current[3].count("'" + domain + "'") == 1
    expected_tables = set(NEW_TABLES)
    if TRANSFER_TABLES.intersection(after["rows"]):
        assert TRANSFER_TABLES.issubset(after["rows"]), "Incomplete transfer schema"
        expected_tables |= TRANSFER_TABLES
    assert set(after["rows"]) - set(before["rows"]) == expected_tables
    for table, rows in before["rows"].items():
        if table != "alembic_version":
            assert after["rows"][table] == rows, "Original rows/rowids changed: " + table
