"""Focused maintenance acceptance; destructive calls own independent artifact files.

No fixture opens the Owner database. Full-schema contents come from the existing
synthetic API seed, and every clear/restore target is a new online-backup clone.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.app.db import DEMO_ACTOR_ID, ROOT
from backend.app.local_demo import create_local_demo_app
from backend.app import sqlite_maintenance as maintenance
from backend.app import standalone_export as export


ARTIFACTS = (ROOT / "artifacts" / "sqlite-maintenance-2026-11-21").resolve()
BASE = "/api/sqlite-maintenance"
TOKEN_HEADER = "X-ERP-Maintenance-Token"
ORIGIN = "http://127.0.0.1:5173"


def isolated(path):
    path = Path(path).resolve()
    assert path.is_relative_to(ARTIFACTS), "Maintenance tests require workspace artifact paths"
    assert path != (ROOT / "data" / "erp.sqlite").resolve()
    if path.exists():
        assert path.is_file() and path.stat().st_nlink == 1
    return path


def connect(path):
    return sqlite3.connect(isolated(path), timeout=5, isolation_level=None,
                           check_same_thread=False)


def readonly(path):
    return sqlite3.connect(isolated(path).as_uri() + "?mode=ro", uri=True,
                           isolation_level=None)


def seed_synthetic(path):
    path = isolated(path)
    assert not path.exists()
    with closing(connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE parent(id TEXT PRIMARY KEY, amount TEXT NOT NULL, payload BLOB);
            CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id TEXT REFERENCES parent(id) ON DELETE RESTRICT);
            CREATE TABLE audit(message TEXT);
            CREATE TABLE compact(a TEXT,b TEXT,PRIMARY KEY(a,b)) WITHOUT ROWID;
            CREATE TABLE shadowed(rowid TEXT,value TEXT);
            CREATE TABLE computed(id INTEGER PRIMARY KEY, amount TEXT,
                                  decorated TEXT GENERATED ALWAYS AS ('[' || amount || ']') STORED);
            CREATE INDEX parent_amount ON parent(amount);
            CREATE VIEW parent_view AS SELECT id,amount FROM parent;
            CREATE TRIGGER parent_audit AFTER INSERT ON parent BEGIN
              INSERT INTO audit(message) VALUES(new.id);
            END;
            CREATE TRIGGER parent_immutable BEFORE DELETE ON parent BEGIN
              SELECT RAISE(ABORT,'immutable synthetic history');
            END;
            PRAGMA user_version=20;
            PRAGMA application_id=12345;
            INSERT INTO parent(rowid,id,amount,payload) VALUES(23,'original','000123.4500',x'00FF');
            INSERT INTO child VALUES(81,'original');
            INSERT INTO compact VALUES('exact','內容');
            INSERT INTO shadowed(_rowid_,rowid,value) VALUES(77,'named column','精確');
            INSERT INTO computed VALUES(90,'0001.2500');
        """)
    return path


def client_for(path, *, full=False):
    path = isolated(path)
    if full:
        app = create_local_demo_app(path, backend_port=8000, frontend_port=5173)
    else:
        app = FastAPI()
        app.state.db = SimpleNamespace(path=path)
        app.state.sqlite_maintenance_binding = {"host": "127.0.0.1", "backend_port": 8000, "frontend_port": 5173}
        maintenance.install_sqlite_maintenance_routes(app)
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 51000))


def status(client):
    response = client.get(BASE + "/status")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def headers(client):
    return {TOKEN_HEADER: status(client)["csrf_token"], "Origin": ORIGIN}


def backup(client):
    response = client.post(BASE + "/backups", json={}, headers=headers(client))
    assert response.status_code in {200, 201}, response.text
    return response.json()["backup"]


def review(client, operation, backup_id=None):
    data = {"operation": operation}
    if backup_id is not None:
        data["backup_id"] = backup_id
    response = client.post(BASE + "/review", json=data, headers=headers(client))
    assert response.status_code == 200, response.text
    return response.json()["confirmation_token"]


def clear(client, token=None):
    token = token or review(client, "clear")
    return client.post(BASE + "/clear", json={"confirmation": "清空", "confirmation_token": token},
                       headers=headers(client))


def restore(client, saved, token=None):
    token = token or review(client, "restore", saved["id"])
    return client.post(BASE + "/restore", json={"backup_id": saved["id"], "confirmed": True,
                       "confirmation_token": token}, headers=headers(client))


def backup_file(path, saved):
    candidates = list((isolated(path).parent / "backups" / "sqlite").rglob("*" + saved["id"] + "*.sqlite"))
    assert len(candidates) == 1, candidates
    return isolated(candidates[0])


def snapshot(path):
    """Independent rows/schema inventory, including accessible original rowids."""
    with closing(readonly(path)) as connection:
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name").fetchall()
        tables = {}
        for kind, name, _, sql in schema:
            if kind != "table":
                continue
            quote = export._quote(name)
            columns = connection.execute("PRAGMA table_xinfo(" + quote + ")").fetchall()
            aliases = {c[1].casefold() for c in columns}
            rowid = None if "WITHOUT ROWID" in sql.upper() else next(
                (alias for alias in ("_rowid_", "rowid", "oid") if alias not in aliases), None)
            selection = ([rowid] if rowid else []) + [export._quote(c[1]) for c in columns]
            rows = connection.execute("SELECT " + ",".join(selection) + " FROM " + quote).fetchall()
            tables[name] = sorted(rows, key=export._encoded_row)
        return {"schema": schema, "rows": tables,
                "head": connection.execute("PRAGMA user_version").fetchall(),
                "application_id": connection.execute("PRAGMA application_id").fetchall()}


@pytest.fixture(scope="module")
def full_source(tmp_path_factory):
    from backend.tests.seed_inventory_transfers_browser import seed
    path = isolated(tmp_path_factory.mktemp("maintenance-full-source") / "synthetic.sqlite")
    seed(path)
    return path


@pytest.fixture
def full_database(full_source, tmp_path):
    destination = isolated(tmp_path / "isolated-full.sqlite")
    assert not destination.exists()
    with closing(readonly(full_source)) as source, closing(connect(destination)) as copied:
        source.backup(copied)
    assert not destination.samefile(full_source)
    return destination


def test_initial_state_local_security_and_direct_clear_rejection(tmp_path):
    path = seed_synthetic(tmp_path / "isolated.sqlite")
    before = snapshot(path)
    with client_for(path) as client:
        initial = status(client)
        assert not initial["clear_eligible"] and not initial["restore_available"]
        assert initial["clear_disabled_reason"] and initial["backups"] == []
        valid = headers(client)
        assert client.post(BASE + "/backups", json={}).status_code == 403
        assert client.get(BASE + "/status", headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.get(BASE + "/status", headers={"Host": "evil.invalid"}).status_code == 403
        assert client.get(BASE + "/status", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        with TestClient(client.app, base_url="http://127.0.0.1:8000", client=("192.0.2.1", 1)) as remote:
            assert remote.get(BASE + "/status").status_code == 403
        assert client.post(BASE + "/review", json={"operation": "clear"}, headers=valid).status_code == 409
        assert clear(client, str(uuid4())).status_code in {400, 409}
        assert client.get(BASE + "/clear", headers=valid).status_code == 405
        assert client.post(BASE + "/backups", json={"path": "../../data/erp.sqlite"}, headers=valid).status_code == 422
        wrong_type = client.post(BASE + "/backups", content="{}", headers={**valid, "Content-Type": "text/plain"})
        assert wrong_type.status_code in {415, 422}
    assert snapshot(path) == before


def test_online_backup_preserves_committed_wal_rows_rowids_schema_and_source(tmp_path):
    path = seed_synthetic(tmp_path / "isolated.sqlite")
    with closing(connect(path)) as writer, client_for(path) as client:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO parent(rowid,id,amount) VALUES(44,'committed-wal','-0.0000000000001250')")
        wal = Path(str(path) + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        before = snapshot(path)
        source_bytes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, wal)}
        identity = (path.stat().st_dev, path.stat().st_ino)
        saved = backup(client)
        copied_path = backup_file(path, saved)
        assert copied_path != path and not copied_path.samefile(path)
        assert snapshot(copied_path) == before == snapshot(path)
        with closing(export._readonly(path)) as source, closing(export._readonly(copied_path)) as copied:
            comparison = export.compare_database_views(source, copied)
        assert comparison["all_rows_equal"] and comparison["schema_equal"]
        assert comparison["checks"]["snapshot"] == {"integrity_check": ["ok"], "foreign_key_violations": 0}
        assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_bytes} == source_bytes
        assert (path.stat().st_dev, path.stat().st_ino) == identity
        assert status(client)["clear_eligible"] and status(client)["restore_available"]
    with client_for(path) as reopened:
        assert status(reopened)["clear_eligible"]
        assert status(reopened)["backups"][0]["id"] == saved["id"]
        assert clear(reopened).status_code == 200
        response = restore(reopened, saved)
        assert response.status_code == 200, response.text
        assert snapshot(path) == before
        assert not status(reopened)["clear_eligible"]


def test_missing_corrupt_stale_and_other_database_backups_fail_closed_after_restart(tmp_path, monkeypatch):
    for condition in ("missing", "corrupt", "stale", "different-database", "replaced-file", "registry-lost"):
        folder = tmp_path / condition
        folder.mkdir()
        path = seed_synthetic(folder / "isolated.sqlite")
        with client_for(path) as client:
            saved = backup(client)
            target = backup_file(path, saved)
            if condition == "missing":
                target.unlink()
            elif condition == "corrupt":
                target.write_bytes(b"deliberately incomplete SQLite backup")
            elif condition == "stale":
                with closing(connect(path)) as writer:
                    writer.execute("INSERT INTO parent(id,amount) VALUES('newer','1.000')")
            elif condition == "different-database":
                # A different active database in the same data directory must
                # not inherit another file's persisted clear authorization.
                other = folder / "different.sqlite"
                with closing(readonly(path)) as source, closing(connect(other)) as copied:
                    source.backup(copied)
                path = other
            elif condition == "replaced-file":
                path.rename(folder / "prior-isolated.sqlite")
                seed_synthetic(path)
            else:
                (target.parent / "registry.json").rename(target.parent / "unavailable-registry.saved")
        before = snapshot(path)
        with client_for(path) as reopened:
            assert not status(reopened)["clear_eligible"], condition
            assert reopened.post(BASE + "/review", json={"operation": "clear"},
                                 headers=headers(reopened)).status_code == 409
            assert clear(reopened, str(uuid4())).status_code in {400, 409}
            if condition != "stale":
                assert not status(reopened)["restore_available"]
        assert snapshot(path) == before
    folder = tmp_path / "failed-partial-backup"
    folder.mkdir()
    path = seed_synthetic(folder / "isolated.sqlite")
    before = snapshot(path)
    def incomplete_snapshot(source, destination):
        isolated(destination).write_bytes(b"incomplete snapshot created before simulated disk failure")
        raise OSError("injected snapshot write failure")
    monkeypatch.setattr(maintenance, "snapshot_database", incomplete_snapshot)
    with client_for(path) as client:
        response = client.post(BASE + "/backups", json={}, headers=headers(client))
        assert response.status_code == 409 and "injected" in response.text
        failed = status(client)
        assert not failed["clear_eligible"] and not failed["restore_available"]
        assert failed["last_result"]["success"] is False
    assert snapshot(path) == before


def test_real_full_schema_clear_create_restore_exact_rows_and_restart(full_database):
    path = full_database
    original = snapshot(path)
    assert len(original["rows"]) == 63
    assert original["rows"]["alembic_version"][0][-1] == "0020_inventory_transfers"
    assert original["rows"]["stock_entry"] and original["rows"]["material_issue"]
    assert original["rows"]["sales_order"] and original["rows"]["production_completion"]
    identity = (path.stat().st_dev, path.stat().st_ino)
    resources = ("purchase-orders", "sales-orders", "stock-positions", "work-orders",
                 "production-completions", "finished-goods-receipts", "inventory-adjustments")
    with client_for(path, full=True) as client:
        before_api = {resource: client.get("/api/" + resource).json() for resource in resources}
        saved = backup(client)
        protected_backup = backup_file(path, saved)
        backup_bytes = protected_backup.read_bytes()
        response = clear(client)
        assert response.status_code == 200, response.text
        emptied = snapshot(path)
        assert emptied["schema"] == original["schema"]
        assert emptied["rows"]["alembic_version"] == original["rows"]["alembic_version"]
        assert len(emptied["rows"]["actor"]) == 1
        assert emptied["rows"]["actor"][0][1] == DEMO_ACTOR_ID
        assert all(not rows for name, rows in emptied["rows"].items()
                   if name not in {"actor", "alembic_version"})
        assert not status(client)["clear_eligible"] and status(client)["restore_available"]
        for resource in resources:
            response = client.get("/api/" + resource)
            assert response.status_code == 200 and response.json() == [], response.text
        created = client.post("/api/categories", json={"code": "AFTER-CLEAR", "name": "清空後可新增"})
        assert created.status_code == 201, created.text
        pre_restore = snapshot(path)
    # A genuine application lifespan stop/start, including migrations and demo
    # operator binding, must preserve the consumed-backup state.
    with client_for(path, full=True) as reopened:
        assert snapshot(path) == pre_restore
        assert not status(reopened)["clear_eligible"] and status(reopened)["restore_available"]
        response = restore(reopened, saved)
        assert response.status_code == 200, response.text
        assert snapshot(path) == original
        assert {resource: reopened.get("/api/" + resource).json() for resource in resources} == before_api
        assert not status(reopened)["clear_eligible"]
        assert protected_backup.read_bytes() == backup_bytes
        recovery = [candidate for candidate in path.parent.rglob("*.sqlite*")
                    if candidate.is_file() and "recovery" in candidate.name and not candidate.name.endswith(("-wal", "-shm"))]
        assert recovery and any(snapshot(candidate) == pre_restore for candidate in recovery)
        assert (path.stat().st_dev, path.stat().st_ino) == identity
        with reopened.app.state.db.read() as session:
            assert session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert session.connection().exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert list(session.connection().exec_driver_sql("PRAGMA foreign_key_check")) == []
    with client_for(path, full=True) as restarted:
        assert snapshot(path) == original
        assert not status(restarted)["clear_eligible"] and status(restarted)["restore_available"]


def test_mid_clear_and_restore_failure_roll_back_rows_ddl_and_fk_state(full_database, monkeypatch):
    path = full_database
    with client_for(path, full=True) as client:
        saved = backup(client)
        original = snapshot(path)
        original_delete = maintenance._delete_rows
        def fail_after_delete(*args, **kwargs):
            assert args[0].execute("PRAGMA foreign_keys").fetchone() == (1,)
            original_delete(*args, **kwargs)
            assert args[0].execute("SELECT count(*) FROM sqlite_schema WHERE type='trigger'").fetchone() == (0,)
            raise RuntimeError("injected failure after all transactional deletes")
        monkeypatch.setattr(maintenance, "_delete_rows", fail_after_delete)
        response = clear(client)
        assert response.status_code in {409, 500}, response.text
        assert "injected" in response.text
        assert snapshot(path) == original
        assert not maintenance.MaintenanceService(path).status()["clear_eligible"]
        monkeypatch.setattr(maintenance, "_delete_rows", original_delete)
        backup(client)
        assert clear(client).status_code == 200
        emptied = snapshot(path)
        original_restore = maintenance._restore_rows
        def fail_after_restore(*args, **kwargs):
            assert args[0].execute("PRAGMA foreign_keys").fetchone() == (1,)
            original_restore(*args, **kwargs)
            assert args[0].execute("SELECT count(*) FROM sqlite_schema WHERE type='trigger'").fetchone() == (0,)
            raise RuntimeError("injected failure after all transactional inserts")
        monkeypatch.setattr(maintenance, "_restore_rows", fail_after_restore)
        response = restore(client, saved)
        assert response.status_code in {409, 500}, response.text
        assert "injected" in response.text
        assert snapshot(path) == emptied
        assert snapshot(backup_file(path, saved)) == original
        assert not status(client)["busy"]
        assert not maintenance.MaintenanceService(path).status()["clear_eligible"]
        with client.app.state.db.read() as session:
            assert session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert session.connection().exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert list(session.connection().exec_driver_sql("PRAGMA foreign_key_check")) == []


def test_postcommit_registry_failure_reports_committed_clear_and_restore_with_warning(tmp_path, monkeypatch):
    path = seed_synthetic(tmp_path / "isolated.sqlite")
    original = snapshot(path)
    with client_for(path) as client:
        saved = backup(client)
        protected_backup = backup_file(path, saved)
        protected_bytes = protected_backup.read_bytes()
        manager = client.app.state.sqlite_maintenance
        save_registry = manager._save_registry

        def execute_with_postcommit_failure(operation):
            before = snapshot(path)
            token = review(client, operation, saved["id"] if operation == "restore" else None)
            calls, failed_states = [], []
            def fail_after_consumption(registry):
                calls.append(registry["last_result"].copy())
                if len(calls) == 1:
                    assert registry["eligible_backup_id"] is None
                    assert registry["last_result"]["success"] is False
                    save_registry(registry)
                    return
                # Read a separate real connection: the intended data change
                # must already be committed when bookkeeping starts failing.
                failed_states.append(snapshot(path))
                assert failed_states[-1] != before
                raise OSError("injected postcommit registry persistence failure")
            with monkeypatch.context() as phase:
                phase.setattr(manager, "_save_registry", fail_after_consumption)
                response = clear(client, token) if operation == "clear" else restore(client, saved, token)
                assert response.status_code == 200, response.text
                result = response.json()
                assert result["committed"] is True
                assert "injected postcommit registry persistence failure" in result["warning"]
                assert len(calls) >= 2 and failed_states
                for observed in (result["status"], status(client)):
                    last = observed["last_result"]
                    assert last["operation"] == operation and last["success"] is True
                    assert last["committed"] is True and last["warning"]
                    assert not observed["clear_eligible"]
                    assert observed["database"]["row_count"] == sum(len(rows) for rows in snapshot(path)["rows"].values())
                with closing(export._readonly(path)) as connection:
                    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
                    assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
                assert protected_backup.read_bytes() == protected_bytes
                return result

        execute_with_postcommit_failure("clear")
        emptied = snapshot(path)
        assert emptied["schema"] == original["schema"]
        assert all(not rows for rows in emptied["rows"].values())
        result = execute_with_postcommit_failure("restore")
        assert snapshot(path) == original
        recovery_file = backup_file(path, result["recovery_backup"])
        recovery_bytes = recovery_file.read_bytes()
        assert snapshot(recovery_file) == emptied
        assert status(client)["last_result"]["committed"] is True
        assert recovery_file.read_bytes() == recovery_bytes
        assert not maintenance.MaintenanceService(path).status()["clear_eligible"]
        # A genuine newer operation supersedes an older warning even if the
        # operating-system wall clock has stepped backwards.
        with monkeypatch.context() as clock:
            clock.setattr(maintenance, "_now", lambda: "2000-01-01T00:00:00+00:00")
            fresh = backup(client)
        assert fresh["created_at"] < result["status"]["last_result"]["completed_at"]
        ready = status(client)
        assert ready["clear_eligible"] and ready["last_result"]["operation"] == "backup"
        assert ready["last_result"]["success"] is True and not ready["last_result"].get("warning")
        registry = manager._registry()
        assert registry["eligible_backup_id"] == fresh["id"]
        assert manager._read_backup(fresh["id"], registry)["generation"] == registry["generation"]
        assert snapshot(path) == original and protected_backup.read_bytes() == protected_bytes
        assert recovery_file.read_bytes() == recovery_bytes


def test_confirmation_is_bound_to_operation_backup_state_and_single_use(tmp_path):
    path = seed_synthetic(tmp_path / "isolated.sqlite")
    with client_for(path) as client:
        saved = backup(client)
        before = snapshot(path)
        token = review(client, "clear")
        wrong = client.post(BASE + "/clear", json={"confirmation": "cancel", "confirmation_token": token},
                            headers=headers(client))
        assert wrong.status_code in {400, 422} and snapshot(path) == before
        assert restore(client, saved, token).status_code in {400, 409}
        expired = review(client, "clear")
        client.app.state.sqlite_maintenance.capabilities[expired]["expires"] = time.monotonic() - 1
        assert clear(client, expired).status_code == 409 and snapshot(path) == before
        token = review(client, "clear")
        with closing(connect(path)) as writer:
            writer.execute("INSERT INTO parent(id,amount) VALUES('after-review','1.125')")
        changed = snapshot(path)
        assert clear(client, token).status_code == 409
        assert snapshot(path) == changed and not status(client)["clear_eligible"]
        backup(client)
        token = review(client, "clear")
        assert clear(client, token).status_code == 200
        empty = snapshot(path)
        assert clear(client, token).status_code in {400, 409}
        assert snapshot(path) == empty and not status(client)["clear_eligible"]


def test_active_business_request_drains_and_maintenance_blocks_new_writes_and_duplicates(tmp_path, monkeypatch):
    path = seed_synthetic(tmp_path / "isolated.sqlite")
    entered, finish_write = threading.Event(), threading.Event()
    deleting, finish_clear = threading.Event(), threading.Event()
    client = client_for(path)
    @client.app.post("/api/qa-write")
    def business_write():
        with closing(connect(path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO parent(id,amount) VALUES('drained-write','3.125')")
            entered.set()
            assert finish_write.wait(10), "Test must release its isolated business request"
            writer.commit()
        return {"committed": True}
    original_delete = maintenance._delete_rows
    def blocking_delete(*args, **kwargs):
        original_delete(*args, **kwargs)
        deleting.set()
        assert finish_clear.wait(10), "Test must release its isolated clear transaction"
    with client, ThreadPoolExecutor(max_workers=3) as workers:
        valid = headers(client)
        writing = workers.submit(client.post, "/api/qa-write")
        assert entered.wait(2)
        saving = workers.submit(client.post, BASE + "/backups", json={}, headers=valid)
        deadline = time.monotonic() + 3
        while client.app.state.maintenance_coordinator.current_operation() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            assert client.app.state.maintenance_coordinator.current_operation() is not None
            assert not saving.done(), "Backup must wait for the active request to commit"
            assert client.post("/api/qa-write").status_code == 503
        finally:
            finish_write.set()
        assert writing.result(timeout=5).status_code == 200
        saved_response = saving.result(timeout=5)
        assert saved_response.status_code in {200, 201}, saved_response.text
        saved = saved_response.json()["backup"]
        assert any("drained-write" in row for row in snapshot(backup_file(path, saved))["rows"]["parent"])
        token = review(client, "clear")
        monkeypatch.setattr(maintenance, "_delete_rows", blocking_delete)
        clearing = workers.submit(clear, client, token)
        assert deleting.wait(3)
        try:
            assert status(client)["busy"]
            assert client.post("/api/qa-write").status_code == 503
            assert client.post(BASE + "/backups", json={}, headers=valid).status_code == 409
            assert clear(client, token).status_code == 409
            assert client.post(BASE + "/restore", json={"backup_id": saved["id"], "confirmed": True,
                               "confirmation_token": token}, headers=valid).status_code == 409
        finally:
            finish_clear.set()
        response = clearing.result(timeout=5)
        assert response.status_code == 200, response.text
        assert not status(client)["busy"] and not status(client)["clear_eligible"]


def test_export_background_package_keeps_shared_exclusion_until_completion(tmp_path, monkeypatch):
    from backend.tests.test_standalone_export import runtime, wait_job
    path = seed_synthetic(tmp_path / "isolated.sqlite")
    runtime_path = runtime(tmp_path / "synthetic-runtime")
    started, finish = threading.Event(), threading.Event()
    package = export.package_snapshot
    def blocking_package(*args):
        started.set()
        assert finish.wait(10), "Test must release its synthetic export worker"
        return package(*args)
    monkeypatch.setattr(export, "runtime_root", lambda: runtime_path)
    monkeypatch.setattr(export, "package_snapshot", blocking_package)
    client = client_for(path)
    export.install_standalone_export_routes(client.app)
    client.app.state.standalone_export_jobs.directory = tmp_path / "export-jobs"
    with client:
        backup(client)
        clear_token = review(client, "clear")
        export_headers = {export.TOKEN_HEADER: client.get("/api/standalone-export/config").json()["csrf_token"],
                          "Origin": ORIGIN}
        response = client.post("/api/standalone-export/jobs", json={"confirmed": True}, headers=export_headers)
        assert response.status_code == 202, response.text
        assert started.wait(3)
        before = snapshot(path)
        try:
            assert status(client)["busy"]
            assert clear(client, clear_token).status_code == 409
            assert client.post(BASE + "/backups", json={}, headers=headers(client)).status_code == 409
            assert client.post("/api/standalone-export/jobs", json={"confirmed": True},
                               headers=export_headers).status_code == 409
            assert snapshot(path) == before
        finally:
            finish.set()
        completed = wait_job(client, response.json()["id"], export_headers)
        assert completed["status"] == "completed", completed
        assert not status(client)["busy"] and status(client)["clear_eligible"]


