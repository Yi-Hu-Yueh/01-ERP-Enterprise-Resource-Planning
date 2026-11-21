"""Local, confirmed exports of a pinned live SQLite view and a verified runtime.

No browser input is used as a filesystem path or command. Application files come
only from the build's manifest; business data comes only from app.state.db.path.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path, PurePosixPath
import secrets
import sqlite3
import struct
import sys
import tempfile
import threading
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import zipfile

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from .db import ROOT
from .stock_ledger import register_exact_sql

ZIP_NAME = "TIGER-ERP-Standalone.zip"
TOKEN_HEADER = "X-ERP-Export-Token"
ACTIVE_STATUSES = {"queued", "snapshotting", "packaging"}


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _readonly(path):
    # mode=ro includes committed WAL pages, unlike immutable=1 or a file copy.
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                                 timeout=15, isolation_level=None)
    register_exact_sql(connection)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _encoded_row(row):
    """Compare SQLite storage types and exact bytes, including decimal TEXT."""
    result = bytearray()
    for value in row:
        if value is None:
            kind, data = b"n", b""
        elif isinstance(value, int):
            kind, data = b"i", str(value).encode("ascii")
        elif isinstance(value, float):
            kind, data = b"r", struct.pack("!d", value)
        elif isinstance(value, str):
            kind, data = b"t", value.encode("utf-8")
        else:
            kind, data = b"b", bytes(value)
        result.extend(kind + struct.pack("!Q", len(data)) + data)
    return bytes(result)


def _table_rows(connection, name, columns, without_rowid):
    names = {column[1].casefold() for column in columns}
    rowid = None if without_rowid else next(
        (alias for alias in ("_rowid_", "rowid", "oid") if alias not in names), None)
    selected = ([rowid] if rowid else []) + [_quote(column[1]) for column in columns]
    rows = sorted(_encoded_row(row) for row in connection.execute(
        "SELECT " + ",".join(selected) + " FROM " + _quote(name)))
    return rowid, rows


def compare_database_views(source, snapshot):
    """Compare every persisted row and schema object in two existing read views.

    The caller keeps the source transaction pinned until this has completed.
    Only counts, schema descriptions and digests leave this function, never rows.
    """
    schema_sql = "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
    source_schema = source.execute(schema_sql).fetchall()
    if source_schema != snapshot.execute(schema_sql).fetchall():
        raise RuntimeError("SQLite schema objects differ from the confirmed source view.")
    source_table_list = {row[1]: row for row in source.execute("PRAGMA table_list")}
    tables = {}
    for kind, name, _, _ in source_schema:
        if kind != "table":
            continue
        quoted = _quote(name)
        columns = source.execute("PRAGMA table_xinfo(" + quoted + ")").fetchall()
        if columns != snapshot.execute("PRAGMA table_xinfo(" + quoted + ")").fetchall():
            raise RuntimeError("SQLite columns or primary keys differ: " + name)
        foreign_keys = source.execute("PRAGMA foreign_key_list(" + quoted + ")").fetchall()
        if foreign_keys != snapshot.execute("PRAGMA foreign_key_list(" + quoted + ")").fetchall():
            raise RuntimeError("SQLite foreign-key schema differs: " + name)
        indexes = source.execute("PRAGMA index_list(" + quoted + ")").fetchall()
        if indexes != snapshot.execute("PRAGMA index_list(" + quoted + ")").fetchall():
            raise RuntimeError("SQLite index list differs: " + name)
        for index in indexes:
            statement = "PRAGMA index_xinfo(" + _quote(index[1]) + ")"
            if source.execute(statement).fetchall() != snapshot.execute(statement).fetchall():
                raise RuntimeError("SQLite index columns differ: " + index[1])
        without_rowid = bool(source_table_list[name][4])
        rowid, original = _table_rows(source, name, columns, without_rowid)
        other_rowid, copied = _table_rows(snapshot, name, columns, without_rowid)
        if rowid != other_rowid or original != copied:
            raise RuntimeError("SQLite persisted row content or rowids differ: " + name)
        digest = hashlib.sha256()
        for row in original:
            digest.update(struct.pack("!Q", len(row)))
            digest.update(row)
        tables[name] = dict(rows=len(original), columns=len(columns),
                            primary_key=[column[1] for column in sorted(columns, key=lambda c: c[5]) if column[5]],
                            rowid_alias=rowid, without_rowid=without_rowid,
                            row_content_sha256=digest.hexdigest())
    for pragma in ("user_version", "application_id", "encoding"):
        if source.execute("PRAGMA " + pragma).fetchall() != snapshot.execute("PRAGMA " + pragma).fetchall():
            raise RuntimeError("SQLite database setting differs: " + pragma)
    checks = {}
    for label, connection in (("source", source), ("snapshot", snapshot)):
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        checks[label] = dict(integrity_check=[row[0] for row in integrity],
                             foreign_key_violations=len(foreign_keys))
        if integrity != [("ok",)] or foreign_keys:
            raise RuntimeError("SQLite integrity or foreign-key verification failed: " + label)
    return dict(table_count=len(tables), column_count=sum(t["columns"] for t in tables.values()),
                row_count=sum(t["rows"] for t in tables.values()), tables=tables,
                schema_object_count=len(source_schema),
                schema_sha256=hashlib.sha256(json.dumps(source_schema, ensure_ascii=False,
                                                       separators=(",", ":")).encode("utf-8")).hexdigest(),
                schema_equal=True, all_rows_equal=True, checks=checks)


def snapshot_database(source_path, destination):
    """Online backup of the exact committed view pinned at confirmation time."""
    source_path, destination = Path(source_path).resolve(), Path(destination).resolve()
    if source_path == destination or destination.exists():
        raise RuntimeError("Snapshot destination must be a new independent database.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(_readonly(source_path)) as source:
        source.execute("BEGIN")
        # BEGIN alone is deferred. This SELECT establishes the WAL read mark
        # before backup, fingerprinting, or concurrent writes can advance it.
        source.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        observed = datetime.now(timezone.utc).isoformat()
        source_identity = Path(source.execute("PRAGMA database_list").fetchone()[2]).resolve()
        if source_identity != source_path:
            raise RuntimeError("SQLite source identity differs from the running backend database.")
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        with closing(_readonly(destination)) as target:
            target.execute("BEGIN")
            evidence = compare_database_views(source, target)
        source.rollback()
    evidence.update(source_path=str(source_path), snapshot_utc=observed,
                    snapshot_bytes=destination.stat().st_size,
                    snapshot_sha256=_sha256(destination))
    return evidence


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def runtime_root():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT / "standalone-runtime" / "TIGER-ERP"


def _verify_development_sources(manifest):
    """A cached runtime is reusable only for the application currently on disk."""
    paths = [ROOT / name for name in (
        "backend/alembic.ini", "backend/standalone.py", "scripts/build_standalone.py",
        "scripts/TIGER-ERP.spec", "scripts/standalone-README.txt", "scripts/packaging-requirements.txt",
        "frontend/index.html", "frontend/package.json", "frontend/package-lock.json")]
    for directory in (ROOT / "backend/app", ROOT / "backend/migrations"):
        paths.extend(directory.rglob("*.py"))
    for directory in (ROOT / "frontend/src", ROOT / "frontend/dist"):
        paths.extend(path for path in directory.rglob("*") if path.is_file())
    for directory, pattern in ((ROOT / "frontend", "tsconfig*.json"),
                               (ROOT / "frontend", "vite.config.*"),
                               (ROOT / "backend", "requirements*.txt")):
        paths.extend(directory.glob(pattern))
    expected = {path.relative_to(ROOT).as_posix(): path for path in paths}
    hashes = manifest.get("source_sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(expected):
        raise RuntimeError("The standalone runtime does not match current application sources; rebuild it with scripts/build_standalone.py.")
    for name, path in expected.items():
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve())
                or _sha256(path) != hashes[name]):
            raise RuntimeError("The standalone runtime is outdated; rebuild it with scripts/build_standalone.py. Changed source: " + name)


def _runtime_manifest(root):
    root = Path(root).resolve(strict=True)
    manifest_bytes = (root / "runtime-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise RuntimeError("Unsupported standalone runtime manifest; rebuild the trusted runtime.")
    if not getattr(sys, "frozen", False) and root == (ROOT / "standalone-runtime" / "TIGER-ERP").resolve():
        _verify_development_sources(manifest)
    files, names = [], set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RuntimeError("Invalid runtime manifest entry.")
        name = entry["path"]
        relative = PurePosixPath(name)
        if (not name or "\\" in name or ":" in name or relative.is_absolute()
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or not (name in {"TIGER-ERP.exe", "README.txt", "THIRD-PARTY-LICENSES.txt"} or name.startswith("_internal/"))
                or any(part.casefold() in {".env", ".git", "data", "seed", "backups", "conversation-assets"} for part in relative.parts)
                or relative.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}
                or name.casefold() in names):
            raise RuntimeError("Runtime manifest contains a forbidden or duplicate path.")
        path = root.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise RuntimeError("Runtime manifest file is missing or outside its trusted directory: " + name)
        if type(entry.get("size")) is not int or path.stat().st_size != entry["size"]:
            raise RuntimeError("Runtime file size verification failed: " + name)
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or _sha256(path) != digest:
            raise RuntimeError("Runtime file SHA-256 verification failed: " + name)
        names.add(name.casefold())
        files.append((name, path, entry["size"], digest))
    if not {"tiger-erp.exe", "readme.txt"}.issubset(names) or not any(name.startswith("_internal/") for name in names):
        raise RuntimeError("Standalone executable, bundled runtime or README is missing.")
    if ("_internal/frontend/dist/index.html" not in names
            or not any(name.startswith("_internal/frontend/dist/assets/") and name.endswith(".js") for name in names)):
        raise RuntimeError("Compiled standalone frontend is missing from the verified runtime.")
    with (root / "TIGER-ERP.exe").open("rb") as executable:
        header = executable.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise RuntimeError("Standalone executable is not a Windows PE image.")
        executable.seek(struct.unpack_from("<I", header, 60)[0])
        pe_header = executable.read(26)
        if (len(pe_header) != 26 or pe_header[:6] != b"PE\0\0\x64\x86"
                or pe_header[24:26] != b"\x0b\x02"):
            raise RuntimeError("Standalone executable is not a Windows x64 PE image.")
    return manifest_bytes, files


def package_snapshot(snapshot, destination, runtime_directory):
    """Allowlist-only package, then verify CRC, every runtime hash and seed rows."""
    snapshot, destination = Path(snapshot), Path(destination)
    manifest_bytes, files = _runtime_manifest(runtime_directory)
    partial = destination.with_suffix(".zip.part")
    seed_hash = _sha256(snapshot)
    expected_names = {"TIGER-ERP/" + name for name, _, _, _ in files}
    expected_names.update({"TIGER-ERP/runtime-manifest.json", "TIGER-ERP/seed/erp.sqlite3"})
    with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path, _, _ in files:
            archive.write(path, "TIGER-ERP/" + name)
        archive.writestr("TIGER-ERP/runtime-manifest.json", manifest_bytes)
        archive.write(snapshot, "TIGER-ERP/seed/erp.sqlite3")
    with zipfile.ZipFile(partial) as archive:
        if len(archive.namelist()) != len(expected_names) or set(archive.namelist()) != expected_names:
            raise RuntimeError("ZIP file inventory does not match the runtime manifest and current snapshot.")
        if archive.testzip() is not None:
            raise RuntimeError("ZIP CRC integrity verification failed.")
        for name, _, size, digest in files:
            info = archive.getinfo("TIGER-ERP/" + name)
            with archive.open(info) as handle:
                actual_hash = hashlib.file_digest(handle, "sha256").hexdigest()
            if info.file_size != size or actual_hash != digest:
                raise RuntimeError("Packaged runtime file verification failed: " + name)
        # Extract exactly this fixed member, never an archive-supplied path.
        with tempfile.TemporaryDirectory(prefix="verify-seed-", dir=destination.parent) as folder:
            extracted = Path(folder) / "erp.sqlite3"
            with archive.open("TIGER-ERP/seed/erp.sqlite3") as handle, extracted.open("xb") as output:
                while block := handle.read(1024 * 1024):
                    output.write(block)
            if _sha256(extracted) != seed_hash:
                raise RuntimeError("Packaged SQLite seed differs from the confirmed snapshot.")
            with closing(_readonly(snapshot)) as source, closing(_readonly(extracted)) as copied:
                source.execute("BEGIN")
                copied.execute("BEGIN")
                compare_database_views(source, copied)
    partial.replace(destination)
    return dict(filename=ZIP_NAME, bytes=destination.stat().st_size,
                sha256=_sha256(destination), crc_verified=True, seed_verified=True)


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _protect(request, token=None):
    if (request.client is None or not _loopback(request.client.host)
            or request.url.scheme != "http" or not _loopback(request.url.hostname or "")):
        raise HTTPException(403, "獨立系統匯出僅限本機迴路位址使用。")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不接受跨網站匯出要求。")
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            try:
                parsed = urlsplit(value)
                allowed = (parsed.scheme == "http" and _loopback(parsed.hostname or "")
                           and parsed.username is None and parsed.password is None
                           and parsed.port in {request.url.port or 80, 5173})
            except ValueError:
                allowed = False
            if not allowed:
                raise HTTPException(403, "匯出要求的來源不受信任，請從本機 ERP 操作。")
    if token is not None and not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
        raise HTTPException(403, "匯出確認憑證無效，請重新開啟確認視窗。")


class ConfirmExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: StrictBool


class ExportJobs:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs = {}
        self.active_job_id = None
        self.latest_job_id = None
        self.directory = Path(tempfile.gettempdir()) / "TIGER-ERP-exports" / str(uuid4())

    def public(self, job):
        return {key: value for key, value in job.copy().items() if not key.startswith("_")}

    def package(self, job):
        try:
            job["artifact"] = package_snapshot(job["_snapshot"], job["_zip"], runtime_root())
            job.update(status="completed", message="匯出完成；ZIP 已通過執行檔、資料快照及完整性驗證。")
        except Exception as exc:
            job.update(status="failed", message="獨立系統匯出失敗，未提供未驗證的 ZIP。", error=str(exc))
        finally:
            self.active_job_id = None
            self.lock.release()
            lease = job.pop("_maintenance_lease", None)
            if lease is not None:
                lease.release()


def install_standalone_export_routes(app):
    from .maintenance_coordination import install_maintenance_coordination
    coordinator = install_maintenance_coordination(app)
    jobs = ExportJobs()
    app.state.standalone_export_jobs = jobs

    def reply(data, status_code=200):
        return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})

    def find(job_id):
        job = jobs.jobs.get(str(job_id))
        if job is None:
            raise HTTPException(404, "找不到本次匯出工作；服務重新啟動後請重新確認匯出。")
        return job

    @app.get("/api/standalone-export/config")
    def config(request: Request):
        _protect(request)
        return reply(dict(csrf_token=jobs.token, active_job_id=jobs.active_job_id,
                          latest_job_id=jobs.latest_job_id))

    @app.post("/api/standalone-export/jobs", status_code=202)
    def create(data: ConfirmExport, request: Request):
        _protect(request, jobs.token)
        if data.confirmed is not True:
            raise HTTPException(400, "請先確認匯出完整現有資料庫。")
        if not jobs.lock.acquire(blocking=False):
            return reply(dict(message="已有匯出工作進行中，請等待完成。", active_job_id=jobs.active_job_id), 409)
        try:
            lease = coordinator.acquire("export", request.app.state.db.path)
        except Exception:
            jobs.lock.release()
            raise
        job_id = str(uuid4())
        job_folder = jobs.directory / job_id
        job = dict(id=job_id, status="snapshotting", message="正在擷取確認當下的完整 SQLite 資料快照。",
                   _snapshot=job_folder / "snapshot.sqlite3", _zip=job_folder / ZIP_NAME,
                   _maintenance_lease=lease)
        jobs.jobs[job_id] = job
        jobs.active_job_id = job_id
        jobs.latest_job_id = job_id
        try:
            # Do this in the POST, before scheduling slow packaging. A later
            # background snapshot could accidentally export post-confirmation data.
            job["snapshot"] = snapshot_database(request.app.state.db.path, job["_snapshot"])
            job.update(status="packaging", message="SQLite 快照已驗證，正在封裝獨立執行環境。")
            thread = threading.Thread(target=jobs.package, args=(job,), name="erp-export-" + job_id, daemon=True)
            thread.start()
        except Exception as exc:
            job.update(status="failed", message="SQLite 快照或匯出啟動失敗，未提供 ZIP。", error=str(exc))
            jobs.active_job_id = None
            jobs.lock.release()
            job.pop("_maintenance_lease").release()
        return reply(jobs.public(job), 202)

    @app.get("/api/standalone-export/jobs/{job_id}")
    def status(job_id: UUID, request: Request):
        _protect(request, jobs.token)
        return reply(jobs.public(find(job_id)))

    @app.get("/api/standalone-export/jobs/{job_id}/download")
    def download(job_id: UUID, request: Request):
        _protect(request, jobs.token)
        job = find(job_id)
        if job["status"] != "completed" or not job["_zip"].is_file():
            raise HTTPException(409, "匯出尚未完成或未通過驗證，無法下載。")
        if _sha256(job["_zip"]) != job["artifact"]["sha256"]:
            job.update(status="failed", message="ZIP 驗證失敗，請重新匯出。", error="Completed ZIP changed before download.")
            raise HTTPException(409, job["message"])
        return FileResponse(job["_zip"], media_type="application/zip", filename=ZIP_NAME,
                            headers={"Cache-Control": "no-store"})
