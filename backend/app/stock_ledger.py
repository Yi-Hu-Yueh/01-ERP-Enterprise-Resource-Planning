"""One exact physical-stock balance for immutable inbound and outbound ledgers."""
from .decimal_text import canonical_decimal, exact_difference, exact_product, exact_sum


def negate(value):
    value = canonical_decimal(value)
    return value[1:] if value.startswith("-") else ("-"+value if value != "0" else "0")


def compare(left, right):
    difference = exact_difference(left, right)
    return 0 if difference == "0" else (-1 if difference.startswith("-") else 1)


class ExactSum:
    def __init__(self):
        self.values = []

    def step(self, value):
        if value is not None:
            self.values.append(canonical_decimal(value))

    def finalize(self):
        return exact_sum(self.values)


def register_exact_sql(connection):
    connection.create_function("erp_issue_test_authorized", 0, lambda: 0)
    connection.create_function("erp_issue_manual_authorized", 0, lambda: 0)
    for name in ["erp_issue_manual_document","erp_issue_manual_digest","erp_issue_manual_snapshot","erp_issue_manual_time"]:
        connection.create_function(name,0,lambda:None)
    connection.create_function("erp_material_return_authorized",0,lambda:0)
    for name in ["basis","document","digest","snapshot","time"]:
        connection.create_function("erp_material_return_"+name,0,lambda:None)
    # Triggers deliberately fail closed on unsupported raw SQLite connections.
    connection.create_aggregate("erp_decimal_sum", 1, ExactSum)
    connection.create_function("erp_decimal_cmp", 2, compare, deterministic=True)
    connection.create_function("erp_decimal_neg", 1, negate, deterministic=True)
    connection.create_function("erp_decimal_mul", 2, exact_product, deterministic=True)
    connection.create_function("erp_decimal_sub", 2, exact_difference, deterministic=True)


def on_hand(session, position_id):
    values = session.connection().exec_driver_sql("SELECT qty_base_delta FROM physical_stock_ledger WHERE position_id=?", (position_id,)).scalars()
    return exact_sum(values)


def returned(session, origin_line_id):
    return exact_sum(session.connection().exec_driver_sql("""SELECT l.quantity FROM supplier_return_line l
        JOIN supplier_return_posting p ON p.supplier_return_id=l.supplier_return_id
        WHERE p.posting_state='POSTED' AND l.origin_line_id=?""", (str(origin_line_id),)).scalars())
