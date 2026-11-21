from backend.tests.finished_goods_migration_assertions import assert_finished_goods_upgrade_objects
"""Allow only the two documented cross-domain balance-guard replacements."""
def assert_issue_ledger_upgrade_objects(before,after):
    intentional={"supplier_return_posting_prepare","supplier_return_posting_finalize"}
    objects={obj[1]:obj for obj in after["objects"]}
    for obj in before["objects"]:
        if obj[1] in intentional:
            changed=objects[obj[1]]
            assert changed[:3]==obj[:3]
            assert "physical_stock_ledger" in changed[3]
            assert "supplier_return source quantity exceeded" in changed[3]
            assert "supplier_return physical stock insufficient" in changed[3]
    assert_finished_goods_upgrade_objects(before,after,exclude=intentional)
