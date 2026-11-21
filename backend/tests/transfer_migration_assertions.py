"""Validate the exact transfer tail and narrowly extended position insert guard."""

NEW_TABLES = {"inventory_transfer", "inventory_transfer_posting", "inventory_transfer_entry"}
OLD_DOMAINS = ["PURCHASE_RECEIPT", "SUPPLIER_RETURN", "MATERIAL_ISSUE", "MATERIAL_RETURN",
    "FINISHED_GOODS_RECEIPT", "SALES_SHIPMENT", "CUSTOMER_RETURN", "STOCKTAKE", "INVENTORY_ADJUSTMENT"]


def compact(sql):
    return "".join(sql.strip().rstrip(";").split())


def assert_transfer_ledger_branch(sql):
    assert sql.count("'INVENTORY_TRANSFER'") == 1
    suffix = """,e.position_id,e.qty_base_delta,NULL
      FROM inventory_transfer_entry e JOIN inventory_transfer_posting p ON p.id=e.posting_id
      JOIN inventory_transfer a ON a.id=e.transfer_id AND a.id=p.transfer_id
      WHERE p.posting_state='POSTED' AND a.state='POSTED'"""
    assert compact(sql).split("'INVENTORY_TRANSFER'", 1)[1] == compact(suffix), "Transfer ledger branch changed"


def without_transfer_ledger_branch(sql):
    if "'INVENTORY_TRANSFER'" not in sql:
        return sql
    assert_transfer_ledger_branch(sql)
    marker = compact("UNION ALL SELECT e.id,e.posting_id,e.transfer_id,'INVENTORY_TRANSFER'")
    normalized = compact(sql)
    assert normalized.count(marker) == 1
    return normalized.rsplit(marker, 1)[0]


def without_transfer_position_extension(sql):
    """Return the exact preceding trigger only after checking the complete new alternative."""
    if "erp_transfer_" not in sql:
        return sql
    before, separator, body = sql.partition("BEGIN")
    prefix, marker, expression = before.partition("WHEN NOT ")
    assert separator and marker
    delimiter = " OR EXISTS(SELECT 1 FROM inventory_transfer a JOIN inventory_transfer_posting p ON p.transfer_id=a.id"
    old, found, tail = expression.partition(delimiter)
    assert found and old.startswith("(")
    extension = delimiter + tail
    for required in ["p.posting_state='PREPARING'", "a.state='DRAFT'", "a.row_version=p.source_version",
        "a.destination_position_idISNULL", "erp_transfer_authorized()=1", "erp_transfer_document()ISp.transfer_id",
        "erp_transfer_digest()ISp.confirmation_digest", "erp_transfer_snapshot()ISp.confirmation_snapshot",
        "erp_transfer_time()ISp.confirmation_received_at", "NEW.item_id=a.item_id",
        "NEW.warehouse_id=a.destination_warehouse_id", "NEW.lot_idISa.lot_id", "NEW.created_by=p.created_by",
        "a.source_warehouse_id", "a.destination_warehouse_id", "freeze_active=1",
        "erp_decimal_cmp(a.quantity,'0')>0", "erp_decimal_cmp(a.quantity,", "erp_decimal_sum(qty_base_delta)"]:
        assert required in compact(extension), "Transfer position permission missing " + required
    assert extension.rstrip().endswith(")")
    return prefix + marker + old[1:] + " BEGIN" + body


def assert_transfer_upgrade_objects(before, after):
    objects = {(obj[0], obj[1]): obj for obj in after["objects"]}
    for original in before["objects"]:
        kind, name, table, sql = original
        current = objects[(kind, name)]
        assert current[:3] == original[:3]
        if name == "physical_stock_ledger":
            assert compact(without_transfer_ledger_branch(current[3])) == compact(sql)
            for domain in OLD_DOMAINS:
                assert current[3].count("'" + domain + "'") == 1
        elif name == "stock_position_identity":
            assert compact(without_transfer_position_extension(current[3])) == compact(sql)
        else:
            assert current == original, "Unrelated original schema object changed: " + name
    assert set(after["rows"]) - set(before["rows"]) == NEW_TABLES
    for table, rows in before["rows"].items():
        if table != "alembic_version":
            assert after["rows"][table] == rows, "Original rows/rowids changed: " + table
