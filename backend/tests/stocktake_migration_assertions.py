"""Stocktaking adds its tables/guards and one branch without rewriting old objects."""
from backend.tests.transfer_migration_assertions import (NEW_TABLES as TRANSFER_TABLES, without_transfer_ledger_branch, without_transfer_position_extension)
from backend.tests.adjustment_migration_assertions import NEW_TABLES as ADJUSTMENT_TABLES, without_adjustment_ledger_branch

NEW_TABLES = {"stocktake", "stocktake_line", "stocktake_exception", "stocktake_posting", "stocktake_entry"}


def compact(sql):
    return "".join(sql.strip().rstrip(";").split())


def assert_stocktake_ledger_branch(sql):
    sql = without_transfer_ledger_branch(sql)
    sql = without_adjustment_ledger_branch(sql)
    assert sql.count("'STOCKTAKE'") == 1
    suffix = """,e.position_id,e.qty_base_delta,NULL
      FROM stocktake_entry e JOIN stocktake_posting p ON p.id=e.posting_id
      JOIN stocktake s ON s.id=e.stocktake_id AND s.id=p.stocktake_id
      WHERE p.posting_state='POSTED' AND s.state='CLOSED'"""
    assert compact(sql).split("'STOCKTAKE'", 1)[1] == compact(suffix), "Stocktake ledger branch changed"


def without_stocktake_ledger_branch(sql):
    """Validate the exact new tail before applying historical branch assertions."""
    sql = without_transfer_ledger_branch(sql)
    sql = without_adjustment_ledger_branch(sql)
    if "'STOCKTAKE'" not in sql:
        return sql
    assert_stocktake_ledger_branch(sql)
    marker = compact("UNION ALL SELECT e.id,e.posting_id,e.stocktake_id,'STOCKTAKE'")
    normalized = compact(sql)
    assert normalized.count(marker) == 1
    return normalized.rsplit(marker, 1)[0]


def assert_stocktake_upgrade_objects(before, after):
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
        assert_stocktake_ledger_branch(current[3])
        for domain in ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN",
                       "FINISHED_GOODS_RECEIPT", "SALES_SHIPMENT", "CUSTOMER_RETURN"]:
            assert current[3].count("'" + domain + "'") == 1
    expected_tables = set(NEW_TABLES)
    if ADJUSTMENT_TABLES.intersection(after["rows"]):
        assert ADJUSTMENT_TABLES.issubset(after["rows"]), "Incomplete adjustment schema"
        expected_tables |= ADJUSTMENT_TABLES
    if TRANSFER_TABLES.intersection(after["rows"]):
        assert TRANSFER_TABLES.issubset(after["rows"]), "Incomplete transfer schema"
        expected_tables |= TRANSFER_TABLES
    assert set(after["rows"]) - set(before["rows"]) == expected_tables
    for table, rows in before["rows"].items():
        if table != "alembic_version":
            assert after["rows"][table] == rows, "Original rows/rowids changed: " + table
