"""Verified local SQLite backups and atomic data-only maintenance.

Immutable business triggers necessarily reject bulk maintenance. Their exact SQL
is suspended/recreated *inside the same IMMEDIATE transaction*, with foreign keys
always enabled and all requests drained. Any error rolls back both DDL and DML.
No live database file is replaced and no normal business authorization changes.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from .db import DEMO_ACTOR_ID
from .maintenance_coordination import install_maintenance_coordination
from .standalone_export import (_quote, _readonly, _sha256, compare_database_views,
                                snapshot_database)
from .stock_ledger import register_exact_sql

TOKEN_HEADER = "X-ERP-Maintenance-Token"
CAPABILITY_SECONDS = 120


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _schema(connection):
    return connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name").fetchall()


def _validate_connection(connection):
    if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("外鍵保護未啟用。")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("SQLite 完整性檢查失敗。")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("SQLite 外鍵檢查失敗。")


def _view_state(connection):
    evidence = compare_database_views(connection, connection)
    evidence["settings"] = {name: connection.execute("PRAGMA " + name).fetchone()[0]
                            for name in ("user_version", "application_id", "encoding")}
    versions = []
    if "alembic_version" in evidence["tables"]:
        versions = sorted(row[0] for row in connection.execute("SELECT version_num FROM alembic_version"))
    evidence["schema_version"] = versions
    evidence["state_sha256"] = hashlib.sha256(_canonical(dict(
        tables=evidence["tables"], schema_sha256=evidence["schema_sha256"],
        settings=evidence["settings"]))).hexdigest()
    return evidence


def _business_count(connection, evidence):
    count = sum(value["rows"] for name, value in evidence["tables"].items()
                if name != "alembic_version" and not name.startswith("sqlite_"))
    if "actor" in evidence["tables"]:
        count -= connection.execute("SELECT count(*) FROM actor WHERE id=?", (DEMO_ACTOR_ID,)).fetchone()[0]
    return count


def _tables(connection):
    tables = list(connection.execute("PRAGMA table_list"))
    if any(row[2] in {"virtual", "shadow"} for row in tables):
        raise RuntimeError("此資料庫包含尚未支援的虛擬資料表，維護已停止。")
    return sorted(row[1] for row in tables if row[0] == "main" and row[2] == "table"
                  and row[1] != "sqlite_schema")


def _delete_rows(connection, preserve_technical=True):
    # defer_foreign_keys postpones existing RESTRICT relationships until the
    # complete operation is validated. It never disables foreign_keys.
    for name in _tables(connection):
        if preserve_technical and name == "alembic_version":
            continue
        if preserve_technical and name == "actor":
            connection.execute("DELETE FROM actor WHERE id<>?", (DEMO_ACTOR_ID,))
        else:
            connection.execute("DELETE FROM " + _quote(name))


def _restore_rows(connection, backup):
    for name in sorted(_tables(backup), key=lambda value: (value == "sqlite_sequence", value)):
        if name == "sqlite_sequence":
            # AUTOINCREMENT INSERTs may have populated this internal table.
            connection.execute("DELETE FROM sqlite_sequence")
        columns = backup.execute("PRAGMA table_xinfo(" + _quote(name) + ")").fetchall()
        # Generated columns are recomputed; final comparison includes them.
        writable = [column[1] for column in columns if column[6] == 0]
        without_rowid = next(row[4] for row in backup.execute("PRAGMA table_list") if row[1] == name)
        names = {column[1].casefold() for column in columns}
        rowid = None if without_rowid else next((alias for alias in ("_rowid_", "rowid", "oid")
                                                  if alias not in names), None)
        selected = ([rowid] if rowid else []) + [_quote(name) for name in writable]
        projection = ",".join(selected)
        rows = backup.execute("SELECT " + projection + " FROM " + _quote(name))
        statement = "INSERT INTO " + _quote(name) + "(" + projection + ") VALUES(" + ",".join("?" for _ in selected) + ")"
        connection.executemany(statement, rows)


class MaintenanceService:
    def __init__(self, database_path):
        self.path = Path(database_path).absolute()
        self.directory = self.path.parent / "backups" / "sqlite"
        self.token = secrets.token_urlsafe(32)
        self.mutex = threading.Lock()
        self.capabilities = {}
        self.cached_status = None
        self.pending_committed_result = None
        self.pending_committed_context = None

    def _physical_identity(self):
        stat = self.path.stat()
        if self.path.resolve() != self.path or self.path.is_symlink() or stat.st_nlink != 1:
            raise RuntimeError("目前資料庫不可使用符號連結或共用硬連結。")
        return dict(path=str(self.path), device=stat.st_dev, inode=stat.st_ino)

    def _safe_file(self, path):
        if (not path.is_file() or path.is_symlink() or path.resolve() != path.absolute()
                or path.stat().st_nlink != 1):
            raise RuntimeError("備份或登錄檔不存在，或不是獨立實體檔案。")

    def _key(self, create=False):
        path = self.directory / "registry.key"
        if create and not path.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
        self._safe_file(path)
        value = path.read_bytes()
        if len(value) != 32:
            raise RuntimeError("備份登錄金鑰無效，必須重新建立合格備份。")
        return value

    def _write_signed(self, path, payload, replace=False):
        signature = hmac.new(self._key(), _canonical(payload), hashlib.sha256).hexdigest()
        content = _canonical(dict(payload=payload, signature=signature))
        temporary = path.with_name("." + path.name + "." + str(uuid4()) + ".tmp")
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if replace:
                os.replace(temporary, path)
            else:
                temporary.rename(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_signed(self, path):
        self._safe_file(path)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        payload, signature = envelope["payload"], envelope["signature"]
        expected = hmac.new(self._key(), _canonical(payload), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise RuntimeError("備份登錄簽章不符；不允許清空或恢復。")
        return payload

    def _registry(self, create=False):
        identity = self._physical_identity()
        path = self.directory / "registry.json"
        if not path.exists() and create:
            if any(self.directory.glob("*.json")):
                raise RuntimeError("既有備份缺少登錄狀態；不會自行重設備份授權。")
            self._key(create=True)
            state = dict(version=1, identity=identity, database_id=str(uuid4()),
                         generation=str(uuid4()), eligible_backup_id=None, last_result=None)
            self._write_signed(path, state)
        state = self._read_signed(path)
        if (state.get("version") != 1 or state.get("identity") != identity
                or not all(isinstance(state.get(key), str) for key in ("database_id", "generation"))):
            raise RuntimeError("備份不屬於目前的實體資料庫，請建立此資料庫的新備份。")
        UUID(state["database_id"])
        UUID(state["generation"])
        return state

    def _save_registry(self, state):
        self._write_signed(self.directory / "registry.json", state, replace=True)

    def _record_path(self, backup_id):
        return self.directory / (str(UUID(str(backup_id))) + ".json")

    def _read_backup(self, backup_id, registry, current=None):
        metadata = self._read_signed(self._record_path(backup_id))
        expected_id = str(UUID(str(backup_id)))
        if (metadata.get("version") != 1 or metadata.get("id") != expected_id
                or metadata.get("database_id") != registry["database_id"]
                or metadata.get("identity") != registry["identity"]
                or metadata.get("kind") not in {"backup", "recovery"}
                or metadata.get("filename") != "erp-" + metadata["kind"] + "-" + expected_id + ".sqlite"):
            raise RuntimeError("備份身分或檔案名稱不符。")
        path = self.directory / metadata["filename"]
        self._safe_file(path)
        if (path.stat().st_size != metadata["bytes"] or _sha256(path) != metadata["sha256"]
                or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))):
            raise RuntimeError("備份檔案已變更、損壞或不完整。")
        with closing(_readonly(path)) as backup:
            backup.execute("BEGIN")
            evidence = _view_state(backup)
            if evidence["state_sha256"] != metadata["evidence"]["state_sha256"]:
                raise RuntimeError("備份完整資料與已驗證登錄不符。")
            if current is not None and _schema(current) != _schema(backup):
                raise RuntimeError("備份結構與目前 ERP 結構不同，不能恢復。")
            if current is not None:
                settings = {name: current.execute("PRAGMA " + name).fetchone()[0]
                            for name in ("user_version", "application_id", "encoding")}
                versions = sorted(row[0] for row in current.execute("SELECT version_num FROM alembic_version")) if "alembic_version" in evidence["tables"] else []
                if settings != evidence["settings"] or versions != evidence["schema_version"]:
                    raise RuntimeError("備份的資料庫設定或遷移版本與目前 ERP 不同，不能恢復。")
        if _sha256(path) != metadata["sha256"]:
            raise RuntimeError("驗證期間備份檔案發生變更。")
        return metadata

    @staticmethod
    def _public_backup(metadata):
        evidence = metadata["evidence"]
        return dict(id=metadata["id"], created_at=metadata["created_at"], bytes=metadata["bytes"],
                    sha256=metadata["sha256"], row_count=evidence["row_count"],
                    business_row_count=metadata["business_row_count"], table_count=evidence["table_count"],
                    schema_version=", ".join(evidence["schema_version"]), database_id=metadata["database_id"],
                    integrity_status="VERIFIED", kind=metadata["kind"])

    def _database_summary(self, connection, registry, evidence):
        technical = []
        if "actor" in evidence["tables"] and connection.execute("SELECT 1 FROM actor WHERE id=?", (DEMO_ACTOR_ID,)).fetchone():
            technical.append("actor.id=" + DEMO_ACTOR_ID)
        if "alembic_version" in evidence["tables"]:
            technical.append("alembic_version（全部遷移版本列）")
        return dict(id=registry["database_id"] if registry else None, filename=self.path.name,
                    path=str(self.path), schema_version=", ".join(evidence["schema_version"]), row_count=evidence["row_count"],
                    business_row_count=_business_count(connection, evidence), technical_preserved=technical)

    def _status(self):
        error, registry = "", None
        try:
            registry = self._registry()
        except Exception as exc:
            error = "尚無可用的已驗證備份，請先備份。" if not (self.directory / "registry.json").exists() else str(exc)
        backups, eligible = [], False
        with closing(_readonly(self.path)) as current:
            current.execute("BEGIN")
            evidence = _view_state(current)
            if registry:
                for path in self.directory.glob("*.json"):
                    if path.name == "registry.json":
                        continue
                    try:
                        metadata = self._read_backup(path.stem, registry, current)
                        backups.append(self._public_backup(metadata))
                        if metadata["id"] == registry["eligible_backup_id"]:
                            eligible = (metadata["kind"] == "backup" and metadata["generation"] == registry["generation"]
                                        and metadata["evidence"]["state_sha256"] == evidence["state_sha256"])
                    except Exception:
                        continue
            reason = "" if eligible else (error or "備份已使用、遺失、無效或資料已變更；請重新備份。")
            status = dict(csrf_token=self.token, backups=sorted(backups, key=lambda row: row["created_at"], reverse=True),
                          clear_eligible=eligible, clear_disabled_reason=reason, restore_available=bool(backups),
                          busy=False, operation=None, last_result=registry["last_result"] if registry else None,
                          database=self._database_summary(current, registry, evidence))
        pending = self.pending_committed_result
        if pending:
            recorded = status.get("last_result") or {}
            context = self.pending_committed_context
            same_event = ({key: value for key, value in recorded.items() if key != "warning"}
                          == {key: value for key, value in pending.items() if key != "warning"})
            # Signed state is ordered causally, not by wall-clock timestamps:
            # only this operation's exact consumed marker or its own completed
            # event may be older than the in-memory delivery warning.
            superseded = (registry is not None and
                (registry["generation"] != context["generation"] or
                 (recorded != context["started_result"] and not same_event)))
            if superseded:
                self.pending_committed_result = None
                self.pending_committed_context = None
            else:
                # The consumed-generation registry may still contain the
                # pre-DML 'started' result if completion persistence failed.
                # Never let that older disk entry erase a verified COMMIT.
                status["last_result"] = pending
                status["clear_eligible"] = False
                status["clear_disabled_reason"] = "資料維護已完成；請先建立新的已驗證備份。"
        self.cached_status = status
        return status

    def status(self, operation=None):
        if operation or not self.mutex.acquire(blocking=False):
            # A first page load may occur while an export already owns the
            # lease. A separate pinned RO view remains safe in WAL mode and
            # supplies the complete database summary without waiting on a writer.
            if self.cached_status is None:
                self._status()
            value = dict(self.cached_status)
            value.update(busy=True, operation=operation or "maintenance", clear_eligible=False,
                         clear_disabled_reason="SQLite 維護或獨立系統匯出進行中，請稍候。")
            return value
        try:
            try:
                return self._status()
            except Exception:
                if self.pending_committed_result and self.cached_status:
                    # Counts here are the validated post-DML view captured
                    # before COMMIT, never the pre-maintenance cached counts.
                    return dict(self.cached_status)
                raise
        finally:
            self.mutex.release()

    def _snapshot(self, registry, kind="backup"):
        backup_id = str(uuid4())
        destination = self.directory / ("erp-" + kind + "-" + backup_id + ".sqlite")
        evidence = snapshot_database(self.path, destination)
        # Online backup preserves the source's WAL journal-mode header. Finalize
        # this independent, closed snapshot as one durable DELETE-mode file so
        # later read-only validation cannot create WAL/SHM companions. The live
        # source stays in WAL mode and is never checkpointed or rewritten here.
        with closing(sqlite3.connect(destination, isolation_level=None)) as finalized:
            if finalized.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise RuntimeError("備份無法完成獨立單檔封存。")
        with closing(_readonly(destination)) as saved:
            saved.execute("BEGIN")
            state = _view_state(saved)
            count = _business_count(saved, state)
        if evidence["schema_sha256"] != state["schema_sha256"] or evidence["tables"] != state["tables"]:
            raise RuntimeError("備份在完成驗證前已發生變更。")
        metadata = dict(version=1, id=backup_id, kind=kind, filename=destination.name,
                        database_id=registry["database_id"], identity=registry["identity"],
                        generation=registry["generation"], created_at=_now(), bytes=destination.stat().st_size,
                        sha256=_sha256(destination), business_row_count=count, evidence=state)
        # Windows _commit/fsync requires a writable descriptor. No bytes are
        # changed here; this flushes the newly created standalone backup file.
        with destination.open("r+b") as handle:
            os.fsync(handle.fileno())
        self._write_signed(self._record_path(backup_id), metadata)
        return metadata

    def backup(self):
        with self.mutex:
            registry = self._registry(create=True)
            previous_eligible = registry["eligible_backup_id"]
            try:
                metadata = self._snapshot(registry)
                registry["eligible_backup_id"] = metadata["id"]
                registry["last_result"] = dict(operation="backup", success=True, message="SQLite 備份完成並已完整驗證。", completed_at=_now())
                self._save_registry(registry)
                return dict(message=registry["last_result"]["message"], backup=self._public_backup(metadata), status=self._status())
            except Exception as exc:
                registry["eligible_backup_id"] = previous_eligible
                registry["last_result"] = dict(operation="backup", success=False,
                    message="SQLite 備份失敗；沒有授予新的清空權限：" + str(exc), completed_at=_now())
                try:
                    self._save_registry(registry)
                except Exception:
                    pass
                raise

    def review(self, operation, backup_id=None):
        with self.mutex:
            registry = self._registry()
            with closing(_readonly(self.path)) as current:
                current.execute("BEGIN")
                state = _view_state(current)
                selected = registry["eligible_backup_id"] if operation == "clear" else backup_id
                metadata = self._read_backup(selected, registry, current)
                if operation == "clear":
                    self._require_fresh(registry, metadata, state)
                token = secrets.token_urlsafe(32)
                self.capabilities = {key: value for key, value in self.capabilities.items() if value["expires"] > time.monotonic()}
                self.capabilities[token] = dict(operation=operation, backup_id=metadata["id"],
                    generation=registry["generation"], state_sha256=state["state_sha256"], expires=time.monotonic() + CAPABILITY_SECONDS)
                return dict(confirmation_token=token, expires_in=CAPABILITY_SECONDS, summary=dict(
                    database=self._database_summary(current, registry, state), backup=self._public_backup(metadata)))

    def _require_fresh(self, registry, metadata, state):
        if (metadata["kind"] != "backup" or registry["eligible_backup_id"] != metadata["id"]
                or metadata["generation"] != registry["generation"]
                or metadata["evidence"]["state_sha256"] != state["state_sha256"]):
            raise RuntimeError("備份已使用或目前資料已變更；請重新備份後再清空。")

    def _consume(self, registry, operation):
        # Persist BEFORE database writes. A crash or later rollback consumes the
        # authorization safely; a new backup is then required, even for empty DBs.
        registry["generation"] = str(uuid4())
        registry["eligible_backup_id"] = None
        registry["last_result"] = dict(operation=operation, success=False,
            message="維護已開始；若未顯示完成，請檢查資料並重新備份。", completed_at=_now())
        self._save_registry(registry)

    def _mutate(self, operation, token, backup_id=None):
        with self.mutex:
            capability = self.capabilities.pop(token, None)
            if (not capability or capability["operation"] != operation or capability["expires"] <= time.monotonic()
                    or (operation == "restore" and capability["backup_id"] != backup_id)):
                raise RuntimeError("確認內容已過期、已使用或不屬於本次操作，請重新確認。")
            registry = self._registry()
            if capability["generation"] != registry["generation"]:
                raise RuntimeError("資料維護狀態已變更，請重新確認。")
            recovery, connection, committed = None, None, False
            try:
                # mode=rw can never silently create an empty fallback database.
                connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=15, isolation_level=None)
                register_exact_sql(connection)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                state = _view_state(connection)
                if state["state_sha256"] != capability["state_sha256"]:
                    raise RuntimeError("確認後資料已變更；沒有執行維護，請重新確認。")
                metadata = self._read_backup(capability["backup_id"], registry, connection)
                if operation == "clear":
                    self._require_fresh(registry, metadata, state)
                else:
                    # Independent RO connection sees the same committed state:
                    # IMMEDIATE already excludes other writers; no own writes yet.
                    recovery = self._snapshot(registry, "recovery")
                    if recovery["evidence"]["state_sha256"] != state["state_sha256"]:
                        raise RuntimeError("恢復前保護快照與目前資料不符。")
                original_schema = _schema(connection)
                triggers = [(name, sql) for kind, name, _, sql in original_schema if kind == "trigger"]
                preserved = {name: connection.execute("SELECT * FROM " + _quote(name)).fetchall()
                             for name in ("alembic_version",) if name in state["tables"]}
                actor = connection.execute("SELECT * FROM actor WHERE id=?", (DEMO_ACTOR_ID,)).fetchall() if "actor" in state["tables"] else []
                self._consume(registry, operation)
                consumed_result = dict(registry["last_result"])
                connection.execute("PRAGMA defer_foreign_keys=ON")
                for name, _ in triggers:
                    connection.execute("DROP TRIGGER " + _quote(name))
                _delete_rows(connection, preserve_technical=operation == "clear")
                if operation == "restore":
                    with closing(_readonly(self.directory / metadata["filename"])) as backup:
                        backup.execute("BEGIN")
                        if _view_state(backup)["state_sha256"] != metadata["evidence"]["state_sha256"]:
                            raise RuntimeError("恢復來源在驗證後發生變更。")
                        _restore_rows(connection, backup)
                        for _, sql in triggers:
                            connection.execute(sql)
                        compare_database_views(backup, connection)
                    if _sha256(self.directory / metadata["filename"]) != metadata["sha256"]:
                        raise RuntimeError("恢復期間備份檔案發生變更。")
                else:
                    for _, sql in triggers:
                        connection.execute(sql)
                    for name in _tables(connection):
                        if name == "alembic_version":
                            if connection.execute("SELECT * FROM " + _quote(name)).fetchall() != preserved[name]:
                                raise RuntimeError("遷移版本列意外變更。")
                        elif name == "actor":
                            if connection.execute("SELECT * FROM actor").fetchall() != actor:
                                raise RuntimeError("本機操作身分保留結果不符。")
                        elif connection.execute("SELECT count(*) FROM " + _quote(name)).fetchone()[0]:
                            raise RuntimeError("業務資料未完全清空：" + name)
                if _schema(connection) != original_schema:
                    raise RuntimeError("維護後資料庫結構不符，已取消操作。")
                _validate_connection(connection)
                if self._physical_identity() != registry["identity"]:
                    raise RuntimeError("目前資料庫實體身分發生變更。")
                post_state = _view_state(connection)
                verified_backups = [self._public_backup(metadata)]
                if recovery:
                    verified_backups.append(self._public_backup(recovery))
                # Build a truthful fallback while the verified new data is
                # still visible in this transaction. No post-COMMIT read or
                # result-file write is necessary to establish these counts.
                committed_status = dict(csrf_token=self.token,
                    backups=sorted(verified_backups, key=lambda row: row["created_at"], reverse=True),
                    clear_eligible=False, clear_disabled_reason="資料維護已完成；請先建立新的已驗證備份。",
                    restore_available=True, busy=False, operation=None, last_result=None,
                    database=self._database_summary(connection, registry, post_state))
                connection.commit()
                committed = True
                message = "SQLite 業務資料已清空；結構與必要技術資料保留。" if operation == "clear" else "SQLite 已恢復，原始資料、識別碼、數值及結構均已驗證。"
                registry["last_result"] = dict(operation=operation, success=True, committed=True,
                                               message=message, completed_at=_now())
                self._save_registry(registry)
                result = dict(committed=True, message=message, status=self._status())
                if recovery:
                    result["recovery_backup"] = self._public_backup(recovery)
                return result
            except Exception as exc:
                if committed:
                    # The data COMMIT already succeeded after full validation.
                    # Do not compensate with another destructive transaction or
                    # report a rollback. Return HTTP 200 with an explicit warning
                    # so clients invalidate their now-obsolete business caches.
                    warning = "資料交易已完成且已驗證；完成結果登錄或狀態讀取失敗：" + str(exc) + "。請檢查資料並建立新備份。"
                    event = dict(registry["last_result"], committed=True, success=True, warning=warning)
                    registry["last_result"] = event
                    self.pending_committed_result = event
                    self.pending_committed_context = dict(generation=registry["generation"],
                                                          started_result=consumed_result)
                    try:
                        self._save_registry(registry)
                    except Exception:
                        pass
                    committed_status["last_result"] = event
                    self.cached_status = committed_status
                    result = dict(committed=True, message=event["message"], warning=warning, status=committed_status)
                    if recovery:
                        result["recovery_backup"] = self._public_backup(recovery)
                    return result
                if connection is not None:
                    connection.rollback()
                # A failed operation never restores its consumed authorization.
                registry["last_result"] = dict(operation=operation, success=False,
                    message="維護失敗；未完成的資料交易已回復：" + str(exc), completed_at=_now())
                try:
                    self._save_registry(registry)
                except Exception:
                    pass
                raise
            finally:
                if connection is not None:
                    connection.close()

    def clear(self, confirmation_token):
        return self._mutate("clear", confirmation_token)

    def restore(self, backup_id, confirmation_token):
        return self._mutate("restore", confirmation_token, str(backup_id))


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewRequest(EmptyRequest):
    operation: Literal["clear", "restore"]
    backup_id: UUID | None = None


class ClearRequest(EmptyRequest):
    confirmation: str
    confirmation_token: str


class RestoreRequest(EmptyRequest):
    backup_id: UUID
    confirmed: StrictBool
    confirmation_token: str


def _protect(request, token=None):
    runtime = getattr(request.app.state, "local_demo_runtime", None)
    binding = getattr(request.app.state, "sqlite_maintenance_binding", None)
    # Only the trusted explicit local factory (or isolated test factory) binds
    # authority. Host headers and loopback requests alone grant no permission.
    if runtime is not None:
        allowed = runtime.accepts(request)
        backend_port, frontend_port = runtime.backend_port, runtime.frontend_port
    elif binding:
        server, peer = request.scope.get("server"), request.scope.get("client")
        backend_port, frontend_port = binding["backend_port"], binding["frontend_port"]
        allowed = (binding.get("host") == "127.0.0.1" and server is not None and tuple(server) == ("127.0.0.1", backend_port)
                   and peer is not None and peer[0] == "127.0.0.1")
    else:
        allowed, backend_port, frontend_port = False, None, None
    if not allowed or request.url.scheme != "http":
        raise HTTPException(403, "SQLite 維護僅限明確啟用的 127.0.0.1 本機 ERP；公開部署不可使用。")
    def local_url(value):
        try:
            parsed = urlsplit(value)
            return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    and parsed.username is None and parsed.password is None
                    and parsed.port in {backend_port, frontend_port})
        except ValueError:
            return False
    if not local_url("http://" + request.headers.get("host", "")):
        raise HTTPException(403, "SQLite 維護 Host 不受信任。")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不接受跨網站 SQLite 維護要求。")
    for header in ("origin", "referer"):
        if request.headers.get(header) and not local_url(request.headers[header]):
            raise HTTPException(403, "SQLite 維護來源不受信任。")
    if token is not None:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "SQLite 維護要求必須使用 JSON。")
        if not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            raise HTTPException(403, "維護憑證無效，請重新整理後操作。")


def install_sqlite_maintenance_routes(app):
    coordinator = install_maintenance_coordination(app)
    app.state.sqlite_maintenance = None
    service_lock = threading.Lock()

    def service(request):
        with service_lock:
            if app.state.sqlite_maintenance is None:
                app.state.sqlite_maintenance = MaintenanceService(request.app.state.db.path)
            return app.state.sqlite_maintenance

    def reply(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    def invoke(request, operation, function):
        manager = service(request)
        _protect(request, manager.token)
        with coordinator.acquire(operation, request.app.state.db.path):
            try:
                return reply(function(manager))
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.get("/api/sqlite-maintenance/status")
    def status(request: Request):
        _protect(request)
        try:
            return reply(service(request).status(coordinator.current_operation()))
        except Exception as exc:
            raise HTTPException(409, "無法驗證目前 SQLite 狀態：" + str(exc)) from exc

    @app.get("/api/sqlite-maintenance/backups")
    def backups(request: Request):
        _protect(request)
        return reply(dict(backups=service(request).status(coordinator.current_operation())["backups"]))

    @app.post("/api/sqlite-maintenance/backups")
    def backup(data: EmptyRequest, request: Request):
        return invoke(request, "backup", lambda manager: manager.backup())

    @app.post("/api/sqlite-maintenance/review")
    def review(data: ReviewRequest, request: Request):
        if (data.operation == "restore") != (data.backup_id is not None):
            raise HTTPException(422, "恢復必須選擇備份；清空使用目前合格備份。")
        return invoke(request, "review", lambda manager: manager.review(data.operation, data.backup_id))

    @app.post("/api/sqlite-maintenance/clear")
    def clear(data: ClearRequest, request: Request):
        if data.confirmation != "清空":
            raise HTTPException(400, "請完整輸入「清空」確認本次操作。")
        return invoke(request, "clear", lambda manager: manager.clear(data.confirmation_token))

    @app.post("/api/sqlite-maintenance/restore")
    def restore(data: RestoreRequest, request: Request):
        if data.confirmed is not True:
            raise HTTPException(400, "請明確確認以所選備份恢復目前資料。")
        return invoke(request, "restore", lambda manager: manager.restore(data.backup_id, data.confirmation_token))
