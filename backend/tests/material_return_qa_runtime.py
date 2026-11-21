"""Explicit return-only QA runtime and fresh synthetic seed, never an Owner copy."""
from pathlib import Path
import sqlite3
import tempfile

from fastapi.testclient import TestClient

from backend.app.db import ROOT
from backend.app.main import create_app
from backend.tests.issue_qa_runtime import new_qa_application
from backend.tests.test_purchasing import po
from backend.tests.test_supplier_returns import returns
from backend.tests.test_outstanding import inquiry
from backend.tests.test_mrp import seed
from backend.tests.test_material_issues import header, add, post, current, balance


def artifact_path(path):
    supplied = Path(path).absolute()
    resolved = supplied.resolve()
    if supplied != resolved or not resolved.is_relative_to((ROOT / "artifacts").resolve()):
        raise ValueError("Material-return QA requires a non-aliased project artifact database")
    if resolved == (ROOT / "data/erp.sqlite").resolve():
        raise ValueError("Owner database cannot receive a material-return QA capability")
    return resolved


def seed_material_return_qa_database(database_path, prepare=None):
    """Create fresh purchasing/BOM/work-order data and a real posted 1.75 issue.

    The optional callback prepares additional *synthetic* test sources while the
    fresh issue-only QA runtime is alive. It is never an HTTP authorization flag.
    The returned database contains no manufacturing return or return movement.
    """
    destination = artifact_path(database_path)
    if destination.exists():
        raise ValueError("QA seeding refuses an existing database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    app, fresh = new_qa_application(destination.parent)
    with TestClient(app) as client:
        r = po.__wrapped__((client, app.state.db, fresh))
        r = seed(inquiry.__wrapped__(returns.__wrapped__(r)))
        issue = header(r, "ISSUE-TEST-001")
        add(r, issue, "1.75")
        response = post(r, issue)
        assert response.status_code == 201, response.text
        r["issue"] = current(r, issue)
        assert balance(r) == "2.25"
        if prepare is not None:
            prepare(r)
        metadata = {key: r[key] for key in ["issue", "orders", "raws", "warehouse", "warehouse2", "boms", "products", "origins", "receipts", "order", "other_order", "suppliers", "unit", "sources"]}
    # Close the issue grant and every engine before moving this newly seeded
    # synthetic file. Checkpoint avoids leaving any committed bytes in its WAL.
    app.state.db.engine.dispose()
    connection = sqlite3.connect(fresh)
    try:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert result[0] == 0
    finally:
        connection.close()
    assert not destination.exists()
    assert fresh.resolve().is_relative_to((ROOT / "artifacts").resolve())
    fresh.rename(destination)
    return dict(database=str(destination), **metadata)


def create_material_return_qa_app(database_path, *, backend_port=8026, frontend_port=5189, bind_host="127.0.0.1"):
    """Controlled operation-specific test runtime; ordinary create_app stays closed."""
    from backend.app.material_return_authorization import attach_material_return_qa_runtime
    path = artifact_path(database_path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Material-return QA requires an existing single-link synthetic database")
    owner = ROOT / "data/erp.sqlite"
    if owner.exists() and path.samefile(owner):
        raise ValueError("Owner database cannot receive a material-return QA capability")
    return attach_material_return_qa_runtime(create_app(path), path,
        backend_port=backend_port, frontend_port=frontend_port, bind_host=bind_host)


def new_material_return_qa_application(parent, prepare=None):
    """Reserve a fresh artifact destination and return app/path/seed metadata."""
    parent = artifact_path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="material-return-qa-", dir=parent))
    path = directory / "isolated.sqlite"
    metadata = seed_material_return_qa_database(path, prepare=prepare)
    return create_material_return_qa_app(path), path, metadata
