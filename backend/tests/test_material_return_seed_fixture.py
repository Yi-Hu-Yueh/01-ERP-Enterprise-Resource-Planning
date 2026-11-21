"""Prove fixture reuse preserves database, metadata and capability isolation."""
from contextlib import closing
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.app.db import ROOT
from backend.app.local_demo import create_local_demo_app
from backend.app.main import create_app
from backend.app.stock_ledger import register_exact_sql
from backend.tests.material_return_qa_runtime import create_material_return_qa_app
from backend.tests.material_return_seed_fixture import clone_material_return_seed
from backend.tests.test_material_returns import header, add, post_data


def snapshot(path):
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as connection:
        register_exact_sql(connection)
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return dict(
            rows={name: connection.execute('SELECT rowid,* FROM "' + name + '" ORDER BY rowid').fetchall() for name in names},
            objects=connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
            integrity=connection.execute("PRAGMA integrity_check").fetchall(),
            foreign_keys=connection.execute("PRAGMA foreign_key_check").fetchall(),
        )


def destinations(sources):
    # The session directory is unique and contains only this run's synthetic
    # baseline. Each test obtains a unique child through mkdtemp.
    import tempfile
    directory = Path(tempfile.mkdtemp(prefix="isolation-", dir=sources[0].parent))
    return directory / "first.sqlite", directory / "second.sqlite"


def client_for(app):
    return TestClient(app, base_url="http://127.0.0.1:8026", client=("127.0.0.1", 50000))


def test_clones_preserve_all_rows_rowids_schema_and_use_distinct_physical_files(material_return_sources):
    source, _ = material_return_sources
    first, second = destinations(material_return_sources)
    a = clone_material_return_seed(material_return_sources, first)
    b = clone_material_return_seed(material_return_sources, second)
    assert snapshot(source) == snapshot(first) == snapshot(second)
    assert not first.samefile(source) and not second.samefile(source) and not first.samefile(second)
    assert source.stat().st_nlink == first.stat().st_nlink == second.stat().st_nlink == 1
    assert a["database"] == str(first) and b["database"] == str(second)
    assert snapshot(first)["integrity"] == [("ok",)] and snapshot(first)["foreign_keys"] == []


def test_clone_database_and_nested_metadata_mutations_do_not_escape_the_case(material_return_sources):
    source, original_metadata = material_return_sources
    first, second = destinations(material_return_sources)
    a = clone_material_return_seed(material_return_sources, first)
    b = clone_material_return_seed(material_return_sources, second)
    baseline = snapshot(source)
    original_name = original_metadata["products"][0]["name"]
    a["products"][0]["name"] = "changed only in first fixture metadata"
    a["products"].append({"id": "metadata-only-new-item"})
    assert b["products"][0]["name"] == original_metadata["products"][0]["name"] == original_name
    assert len(a["products"]) == len(b["products"]) + 1 == len(original_metadata["products"]) + 1
    with TestClient(create_app(first)) as client:
        response = client.post("/api/units", json={"code": "CLONE-ONLY", "name": "僅此隔離資料庫"})
        assert response.status_code == 201, response.text
    assert snapshot(first) != baseline
    assert snapshot(second) == snapshot(source) == baseline


def test_clones_keep_fresh_runtime_authority_and_cannot_reuse_a_previous_confirmation(material_return_sources):
    first, _ = destinations(material_return_sources)
    metadata = clone_material_return_seed(material_return_sources, first)
    qa_app = create_material_return_qa_app(first)
    with client_for(qa_app) as client:
        r = dict(metadata, client=client, app=qa_app, db=qa_app.state.db, path=first)
        assert client.get("/api/material-returns/policy").json()["authorization_basis"] == "CONTROLLED_QA_ONLY"
        assert client.get("/api/material-issues/policy").json()["posting_allowed"] is False
        document = header(r, "FIXTURE-RUNTIME-ISOLATION")
        add(r, document)
        confirmation = post_data(r, document)
        driver = qa_app.state.db.engine.raw_connection()
        try:
            assert driver.execute("SELECT erp_material_return_authorized()").fetchone()[0] == 0
        finally:
            driver.close()
    before = snapshot(first)
    with TestClient(create_app(first)) as ordinary:
        assert ordinary.get("/api/material-returns/policy").json()["posting_allowed"] is False
        assert ordinary.post("/api/material-returns/" + document["id"] + "/post", json=confirmation).status_code == 403
    demo_app = create_local_demo_app(first, backend_port=8026, frontend_port=5189)
    with client_for(demo_app) as client:
        assert client.get("/api/material-returns/policy").json()["authorization_basis"] == "LOCAL_DEMO_MANUAL"
        assert client.post("/api/material-returns/" + document["id"] + "/post", json=confirmation).status_code == 403
        with demo_app.state.db.read() as session:
            assert session.connection().exec_driver_sql("PRAGMA synchronous").scalar() == 2
    assert snapshot(first) == before


def test_clone_refuses_existing_owner_and_nonartifact_paths_without_overwriting(material_return_sources):
    first, _ = destinations(material_return_sources)
    clone_material_return_seed(material_return_sources, first)
    baseline = snapshot(first)
    with pytest.raises(ValueError, match="existing destination"):
        clone_material_return_seed(material_return_sources, first)
    for destination in [ROOT / "data/erp.sqlite", ROOT / "backend/nonartifact-fixture.sqlite"]:
        with pytest.raises(ValueError, match="artifact"):
            clone_material_return_seed(material_return_sources, destination)
    assert snapshot(first) == baseline
