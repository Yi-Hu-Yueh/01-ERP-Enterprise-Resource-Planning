"""Synthetic source documents for isolated approval QA; never an Owner copy.

All business records are created through their existing APIs. The baseline has
genuine synthetic posted ledger history as well as separate editable PO/WO/SO
documents. Organization initialization remains an explicit UI/API action.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.tests.material_return_qa_runtime import seed_material_return_qa_database
from backend.tests.test_purchasing import create_header, add_line, get as purchase_get
from backend.tests.test_work_orders import create as work_create
from backend.tests.test_sales import create as sales_create, add as sales_add, get as sales_get

ARTIFACTS = (ROOT / "artifacts" / "electronic-approval-2026-11-21").resolve()
PORT, UI_PORT = 8051, 5214
ORG_TABLES = {"erp_department", "erp_employee", "department", "employee", "employee_role", "approval_role", "employee_role_assignment"}


def isolated(path):
    supplied = Path(path).absolute()
    resolved = supplied.resolve()
    if supplied != resolved or not resolved.is_relative_to(ARTIFACTS):
        raise ValueError("Approval QA must use a non-aliased task artifact path")
    if resolved.exists() and (not resolved.is_file() or resolved.stat().st_nlink != 1):
        raise ValueError("Approval QA database must be a regular independent file")
    return resolved


def business_rows(path, tables=None):
    """Read deterministic persisted values, including ledger and row versions."""
    with closing(sqlite3.connect(isolated(path).as_uri() + "?mode=ro", uri=True)) as con:
        if tables is None:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
                      if r[0] != "alembic_version" and r[0] not in ORG_TABLES and not r[0].startswith("approval_")]
        return {table: sorted([list(row) for row in con.execute('SELECT * FROM "' + table.replace('"', '""') + '"')], key=repr)
                for table in tables}


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")).hexdigest()


def seed(path):
    path = isolated(path)
    if path.exists():
        raise ValueError("Approval source seeding refuses an existing destination")
    prepared = {}

    def prepare(r):
        c = r["client"]
        purchase = create_header(r, "PO-APPROVAL-SYNTHETIC", note="Synthetic approval QA source; no Owner data")
        add_line(r, purchase, "2.5", "12.75")
        purchase = purchase_get(r, purchase)
        work = work_create(r, "WO-APPROVAL-SYNTHETIC", bom=0, routing=0, planned_qty_base="2.5", note="Synthetic approval QA source; no Owner data")
        response = c.post("/api/customers", json=dict(code="APPROVAL-QA-CUSTOMER", name="簽核合成測試客戶", currency_id=r["currency"]["id"], is_active=True))
        assert response.status_code == 201, response.text
        r["customers"] = [response.json()]
        sales = sales_create(r, "SO-APPROVAL-SYNTHETIC", note="Synthetic approval QA source; no Owner data")
        product_unit = next(unit for unit in c.get("/api/units").json() if unit["id"] == r["products"][0]["base_unit_id"])
        sales_add({**r, "unit": product_unit}, sales, "1.25", "40")
        sales = sales_get(r, sales)
        prepared.update(sources={"PURCHASE_ORDER": purchase, "WORK_ORDER": work, "SALES_ORDER": sales})

    inherited = seed_material_return_qa_database(path, prepare=prepare)
    baseline = business_rows(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        for table in ORG_TABLES:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                assert con.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0] == 0
    return dict(database=str(path), synthetic_only=True, sources=prepared["sources"],
                historical_issue_id=inherited["issue"]["id"], business_tables=list(baseline),
                expected_business_fingerprint=fingerprint(baseline))


def clone(source, destination, metadata):
    source, destination = isolated(source), isolated(destination)
    if not source.is_file() or destination.exists():
        raise ValueError("Clone requires an existing synthetic template and a fresh destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as target:
            original.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
    assert not destination.samefile(source) and destination.stat().st_nlink == 1
    result = deepcopy(metadata)
    result["database"] = str(destination)
    assert fingerprint(business_rows(destination, result["business_tables"])) == result["expected_business_fingerprint"]
    return result


if __name__ == "__main__":
    metadata = seed(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print("Fresh synthetic approval PO/WO/SO; original synthetic ledger preserved; no demo organization initialized.")
