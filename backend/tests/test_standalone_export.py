"""Export verification uses synthetic databases and synthetic runtime files only."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import threading
import time
from types import SimpleNamespace
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.app import standalone_export as export


def database(path):
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE parent(id TEXT PRIMARY KEY, amount TEXT NOT NULL, payload BLOB);
        CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id TEXT REFERENCES parent(id));
        CREATE TABLE audit(message TEXT);
        CREATE TABLE compact(a TEXT, b TEXT, PRIMARY KEY(a,b)) WITHOUT ROWID;
        CREATE TABLE shadowed(rowid TEXT, value TEXT);
        CREATE INDEX parent_amount ON parent(amount);
        CREATE VIEW parent_view AS SELECT id,amount FROM parent;
        CREATE TRIGGER parent_audit AFTER INSERT ON parent BEGIN
          INSERT INTO audit(message) VALUES (new.id);
        END;
        PRAGMA user_version=20;
        PRAGMA application_id=12345;
        INSERT INTO parent(rowid,id,amount,payload) VALUES(23,'original','000123.4500',x'00FF');
        INSERT INTO child(id,parent_id) VALUES(81,'original');
        INSERT INTO compact VALUES('b','c');
        INSERT INTO shadowed(_rowid_,rowid,value) VALUES(77,'named column','exact');
    """)
    connection.commit()
    return connection


def runtime(folder):
    folder.mkdir()
    (folder / "_internal").mkdir()
    # Deliberately minimal PE headers for unit tests, never a deliverable runtime.
    pe = bytearray(154)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 60, 128)
    pe[128:134] = b"PE\0\0\x64\x86"
    pe[152:154] = b"\x0b\x02"
    (folder / "TIGER-ERP.exe").write_bytes(pe)
    (folder / "README.txt").write_text("測試用執行環境，不是發布檔案。", encoding="utf-8")
    (folder / "_internal" / "python.dll").write_bytes(b"synthetic-runtime")
    frontend = folder / "_internal" / "frontend" / "dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<html>synthetic frontend</html>", encoding="utf-8")
    (frontend / "assets" / "index.js").write_text("/* synthetic frontend */", encoding="utf-8")
    entries = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            content = path.read_bytes()
            entries.append(dict(path=path.relative_to(folder).as_posix(), size=len(content),
                                sha256=hashlib.sha256(content).hexdigest()))
    (folder / "runtime-manifest.json").write_text(json.dumps(dict(version=1, files=entries)), encoding="utf-8")
    # These unlisted private files must never enter an export.
    (folder / ".env").write_text("PRIVATE=excluded", encoding="utf-8")
    (folder / "unlisted.txt").write_text("excluded", encoding="utf-8")
    return folder


def api_client(path):
    app = FastAPI()
    app.state.db = SimpleNamespace(path=path)
    export.install_standalone_export_routes(app)
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 51000))


def wait_job(client, job_id, headers):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get("/api/standalone-export/jobs/" + job_id, headers=headers).json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail("Synthetic export job did not finish within 10 seconds")


def test_online_snapshot_preserves_wal_all_rows_rowids_schema_and_source(tmp_path):
    path, snapshot = tmp_path / "live.sqlite3", tmp_path / "snapshot.sqlite3"
    with closing(database(path)) as writer:
        before = {str(p): export._sha256(p) for p in (path, Path(str(path) + "-wal"))}
        evidence = export.snapshot_database(path, snapshot)
        assert {name: export._sha256(name) for name in before} == before
        assert evidence["all_rows_equal"] and evidence["schema_equal"]
        assert evidence["table_count"] == 5 and evidence["row_count"] == 5
        assert evidence["schema_object_count"] == writer.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0]
        assert evidence["tables"]["parent"]["primary_key"] == ["id"]
        assert evidence["tables"]["compact"]["without_rowid"]
        assert evidence["tables"]["shadowed"]["rowid_alias"] == "_rowid_"
        assert evidence["checks"]["snapshot"] == dict(integrity_check=["ok"], foreign_key_violations=0)
        with closing(export._readonly(snapshot)) as copied:
            assert copied.execute("SELECT rowid,id,amount,hex(payload) FROM parent").fetchone() == (
                23, "original", "000123.4500", "00FF")
            assert copied.execute("SELECT _rowid_,rowid FROM shadowed").fetchone() == (77, "named column")
        with closing(export._readonly(path)) as readonly:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                readonly.execute("DELETE FROM parent")
        assert writer.execute("SELECT count(*) FROM parent").fetchone() == (1,)


def test_snapshot_comparison_keeps_confirmation_read_view_during_new_wal_commit(tmp_path, monkeypatch):
    path, snapshot = tmp_path / "live.sqlite3", tmp_path / "snapshot.sqlite3"
    compare = export.compare_database_views
    with closing(database(path)) as writer:
        def later_commit(source, copied):
            writer.execute("INSERT INTO parent(id,amount) VALUES('later','1.000')")
            writer.commit()
            assert source.execute("SELECT count(*) FROM parent").fetchone() == (1,)
            return compare(source, copied)
        monkeypatch.setattr(export, "compare_database_views", later_commit)
        evidence = export.snapshot_database(path, snapshot)
        assert evidence["tables"]["parent"]["rows"] == 1
        assert writer.execute("SELECT count(*) FROM parent").fetchone() == (2,)
        with closing(sqlite3.connect(snapshot)) as copied:
            assert copied.execute("SELECT id FROM parent").fetchall() == [("original",)]


def test_package_allowlist_crc_seed_equality_and_runtime_tamper_rejection(tmp_path, monkeypatch):
    path, snapshot = tmp_path / "live.sqlite3", tmp_path / "snapshot.sqlite3"
    with closing(database(path)):
        export.snapshot_database(path, snapshot)
    root = runtime(tmp_path / "runtime")
    destination = tmp_path / export.ZIP_NAME
    artifact = export.package_snapshot(snapshot, destination, root)
    assert artifact["crc_verified"] and artifact["seed_verified"]
    assert artifact["sha256"] == export._sha256(destination)
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert "TIGER-ERP/seed/erp.sqlite3" in archive.namelist()
        assert all(".env" not in name and "unlisted" not in name for name in archive.namelist())
        assert hashlib.sha256(archive.read("TIGER-ERP/seed/erp.sqlite3")).hexdigest() == export._sha256(snapshot)
    (root / "_internal" / "python.dll").write_bytes(b"tampered-runtime!")
    with pytest.raises(RuntimeError, match="verification failed"):
        export.package_snapshot(snapshot, tmp_path / "tampered.zip", root)
    assert not (tmp_path / "tampered.zip").exists()
    manifest_path = root / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../private.txt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="forbidden"):
        export.package_snapshot(snapshot, tmp_path / "traversal.zip", root)
    private_backup = root / "backups" / "private.sqlite"
    private_backup.parent.mkdir()
    private_backup.write_bytes(b"local backup must never enter a standalone ZIP")
    manifest["files"][0] = dict(path="backups/private.sqlite", size=private_backup.stat().st_size,
                               sha256=export._sha256(private_backup))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="forbidden"):
        export.package_snapshot(snapshot, tmp_path / "private-backup.zip", root)
    source_root = tmp_path / "synthetic-sources"
    relative_sources = ["backend/alembic.ini", "backend/standalone.py", "scripts/build_standalone.py",
                        "scripts/TIGER-ERP.spec", "scripts/standalone-README.txt", "scripts/packaging-requirements.txt", "backend/app/main.py",
                        "backend/migrations/versions/first.py", "backend/requirements.txt", "frontend/dist/index.html",
                        "frontend/src/main.tsx", "frontend/index.html", "frontend/package.json",
                        "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.ts"]
    hashes = {}
    for name in relative_sources:
        file = source_root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic source", encoding="utf-8")
        hashes[name] = export._sha256(file)
    monkeypatch.setattr(export, "ROOT", source_root)
    export._verify_development_sources(dict(source_sha256=hashes))
    with pytest.raises(RuntimeError, match="does not match"):
        export._verify_development_sources(dict(source_sha256={}))
    (source_root / "backend/app/main.py").write_text("changed since build", encoding="utf-8")
    with pytest.raises(RuntimeError, match="outdated"):
        export._verify_development_sources(dict(source_sha256=hashes))


def test_api_local_csrf_confirmation_duplicate_current_snapshot_and_download(tmp_path, monkeypatch):
    path = tmp_path / "live.sqlite3"
    root = runtime(tmp_path / "runtime")
    started, finish = threading.Event(), threading.Event()
    original_package = export.package_snapshot
    def blocking_package(*args):
        started.set()
        assert finish.wait(10)
        return original_package(*args)
    monkeypatch.setattr(export, "runtime_root", lambda: root)
    monkeypatch.setattr(export, "package_snapshot", blocking_package)
    with closing(database(path)) as writer, api_client(path) as client:
        config = client.get("/api/standalone-export/config")
        assert config.status_code == 200 and config.headers["cache-control"] == "no-store"
        headers = {export.TOKEN_HEADER: config.json()["csrf_token"], "Origin": "http://127.0.0.1:5173"}
        assert client.get("/api/standalone-export/config", headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/api/standalone-export/config", headers={"Host": "evil.invalid"}).status_code == 403
        assert client.get("/api/standalone-export/config", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        with TestClient(client.app, base_url="http://127.0.0.1:8000", client=("192.0.2.2", 1)) as remote:
            assert remote.get("/api/standalone-export/config").status_code == 403
        assert client.post("/api/standalone-export/jobs", json={"confirmed": True}).status_code == 403
        assert client.post("/api/standalone-export/jobs", json={"confirmed": False}, headers=headers).status_code == 400
        assert client.post("/api/standalone-export/jobs", json={"confirmed": True, "path": "arbitrary"}, headers=headers).status_code == 422
        first = client.post("/api/standalone-export/jobs", json={"confirmed": True}, headers=headers)
        assert first.status_code == 202
        job_id = first.json()["id"]
        assert started.wait(2)
        try:
            assert first.json()["snapshot"]["tables"]["parent"]["rows"] == 1
            assert client.get("/api/standalone-export/config").json()["active_job_id"] == job_id
            duplicate = client.post("/api/standalone-export/jobs", json={"confirmed": True}, headers=headers)
            assert duplicate.status_code == 409 and duplicate.json()["active_job_id"] == job_id
            url = "/api/standalone-export/jobs/" + job_id
            assert client.get(url).status_code == 403
            assert client.get(url + "/download", headers=headers).status_code == 409
            writer.execute("INSERT INTO parent(id,amount) VALUES('after-confirmation','2.000')")
            writer.commit()
        finally:
            finish.set()
        final = wait_job(client, job_id, headers)
        assert final["status"] == "completed", final
        download = client.get(url + "/download", headers=headers)
        assert download.status_code == 200 and export.ZIP_NAME in download.headers["content-disposition"]
        assert hashlib.sha256(download.content).hexdigest() == final["artifact"]["sha256"]
        internal = client.app.state.standalone_export_jobs.jobs[job_id]
        with closing(sqlite3.connect(internal["_snapshot"])) as copied:
            assert copied.execute("SELECT id FROM parent").fetchall() == [("original",)]
        assert writer.execute("SELECT count(*) FROM parent").fetchone() == (2,)
        internal["_zip"].write_bytes(b"changed after validation")
        assert client.get(url + "/download", headers=headers).status_code == 409
        assert client.get(url, headers=headers).json()["status"] == "failed"


def test_api_snapshot_and_packaging_failures_never_offer_success_or_download(tmp_path, monkeypatch):
    missing = tmp_path / "not-created.sqlite3"
    with api_client(missing) as client:
        headers = {export.TOKEN_HEADER: client.get("/api/standalone-export/config").json()["csrf_token"]}
        failed = client.post("/api/standalone-export/jobs", json={"confirmed": True}, headers=headers).json()
        assert failed["status"] == "failed" and "unable to open" in failed["error"]
        assert not missing.exists()
        assert client.get("/api/standalone-export/jobs/" + failed["id"] + "/download", headers=headers).status_code == 409
        assert client.app.state.standalone_export_jobs.active_job_id is None
    path = tmp_path / "live.sqlite3"
    monkeypatch.setattr(export, "runtime_root", lambda: tmp_path / "runtime-missing")
    with closing(database(path)), api_client(path) as client:
        headers = {export.TOKEN_HEADER: client.get("/api/standalone-export/config").json()["csrf_token"]}
        response = client.post("/api/standalone-export/jobs", json={"confirmed": True}, headers=headers).json()
        failed = wait_job(client, response["id"], headers)
        assert failed["status"] == "failed" and "artifact" not in failed
        assert client.get("/api/standalone-export/jobs/" + failed["id"] + "/download", headers=headers).status_code == 409
        assert not client.app.state.standalone_export_jobs.lock.locked()
    with closing(sqlite3.connect(path)) as invalid:
        invalid.execute("INSERT INTO child(id,parent_id) VALUES(99,'missing-parent')")
        invalid.commit()
    with pytest.raises(RuntimeError, match="foreign-key verification failed"):
        export.snapshot_database(path, tmp_path / "invalid-snapshot.sqlite3")
