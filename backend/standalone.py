"""Portable, loopback-only entry point for the existing ERP application.

The executable contains no Owner database. Every export supplies its verified
snapshot in seed/; only this installation's data/erp.sqlite3 is ever writable.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
from urllib.request import urlopen
import webbrowser

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import BackgroundTasks, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
import uvicorn

from backend.app.db import DEMO_ACTOR_ID, DEMO_LABEL, ROOT
from backend.app.local_demo import create_local_demo_app
from backend.app.stock_ledger import register_exact_sql


class StartupError(RuntimeError):
    """An actionable startup failure; never authorizes an empty fallback."""


def _safe_regular_file(path: Path):
    if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
        raise StartupError(f"資料庫必須是此安裝目錄內的實體檔案：{path}")
    if path.stat().st_nlink != 1:
        raise StartupError(f"資料庫不可與其他安裝共用硬連結：{path}")


@contextmanager
def _read_connection(path: Path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        register_exact_sql(connection)
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _validate_database(connection):
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise StartupError(f"資料庫完整性檢查失敗：{integrity!r}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise StartupError(f"資料庫關聯檢查失敗：{foreign_keys!r}")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    if not {"actor", "alembic_version", "item", "unit"}.issubset(tables):
        raise StartupError("資料庫不是完整的 ERP 快照；不會建立空白資料庫。")
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    actual = {row[0] for row in connection.execute("SELECT version_num FROM alembic_version")}
    if actual != expected:
        raise StartupError("資料庫與此執行檔的結構版本不符；請保留資料並使用相符的完整匯出。")
    operator = connection.execute("SELECT login_key, display_name FROM actor WHERE id=?", (DEMO_ACTOR_ID,)).fetchone()
    if operator != ("LOCAL_DEMO", DEMO_LABEL):
        raise StartupError("資料庫缺少既有穩定本機操作身分，或其內容衝突；不會重新植入資料。")


def prepare_database(installation: Path) -> Path:
    """Open existing active data, or atomically initialize from the real seed."""
    installation = installation.resolve()
    data = installation / "data"
    if data.is_symlink() or (data.exists() and data.resolve() != data.absolute()):
        raise StartupError("data 目錄不可是指向其他位置的連結。")
    try:
        data.mkdir(exist_ok=True)
        # Probe the actual destination rather than relying on ACL heuristics.
        with tempfile.NamedTemporaryFile(prefix=".writable-", dir=data):
            pass
    except OSError as error:
        raise StartupError("安裝目錄無法寫入。請將整個 TIGER-ERP 資料夾解壓縮到可寫入的位置後重試；既有資料不會重設。") from error
    active = data / "erp.sqlite3"
    if active.exists():
        _safe_regular_file(active)
        attributes = getattr(active.stat(), "st_file_attributes", 0)
        if attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 1):
            raise StartupError("data/erp.sqlite3 是唯讀檔案；請修正此安裝的寫入權限，既有資料不會被 seed 覆蓋。")
        with _read_connection(active) as connection:
            _validate_database(connection)
        return active

    seed = installation / "seed" / "erp.sqlite3"
    if not seed.exists():
        raise StartupError("缺少 seed/erp.sqlite3 原始資料快照。請完整解壓縮匯出 ZIP；不會建立空白資料庫。")
    _safe_regular_file(seed)
    descriptor, name = tempfile.mkstemp(prefix=".erp-initialize-", suffix=".sqlite3", dir=data)
    os.close(descriptor)
    staged = Path(name)
    try:
        with _read_connection(seed) as source, closing(sqlite3.connect(staged)) as target:
            # A real source read transaction includes all committed WAL content.
            source.execute("BEGIN")
            _validate_database(source)
            source.backup(target)
            register_exact_sql(target)
            _validate_database(target)
            source.rollback()
        # os.rename never replaces an existing destination on Windows. The
        # installation lock also prevents two first launches from racing.
        if active.exists():
            raise StartupError("另一個程序已建立資料庫；未覆寫任何既有資料，請重新啟動。")
        staged.rename(active)
    except StartupError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise StartupError(f"無法從完整快照初始化此安裝的資料庫：{error}。不會改用空白資料庫。") from error
    finally:
        staged.unlink(missing_ok=True)
    return active


@contextmanager
def installation_lock(installation: Path):
    """One process owns a portable installation; another folder is independent."""
    data = installation / "data"
    try:
        data.mkdir(exist_ok=True)
        if data.resolve() != data.absolute() or data.is_symlink():
            raise StartupError("data 目錄不可是指向其他位置的連結。")
        handle = (data / ".instance.lock").open("a+b")
    except OSError as error:
        raise StartupError("安裝目錄無法寫入；請解壓縮到可寫入的位置。") from error
    locked = False
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise StartupError("此 TIGER-ERP 安裝已在執行中。請使用已開啟的視窗，或先在其主控台按 Ctrl+C。") from error
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise StartupError("此 TIGER-ERP 安裝已在執行中。") from error
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class FrontendFiles(StaticFiles):
    """Production Vite assets, with client routes behind all existing APIs."""

    async def get_response(self, path, scope):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code != 404 or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


def create_standalone_app(database: Path, port: int, installation: Path):
    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise StartupError("缺少完整前端資產，請重新完整解壓縮 ZIP。")
    app = create_local_demo_app(database, backend_port=port, frontend_port=port,
                                standalone_directory=installation)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
    shutdown_token = secrets.token_urlsafe(32)
    origin = f"http://127.0.0.1:{port}"

    def local_request(request, *, mutation=False):
        peer, server = request.scope.get("client"), request.scope.get("server")
        if (not peer or peer[0] != "127.0.0.1" or server != ("127.0.0.1", port)
                or request.headers.get("host") != f"127.0.0.1:{port}"):
            raise HTTPException(status_code=403, detail="Only this local installation may control its runtime")
        supplied = request.headers.get("origin")
        if (mutation and supplied != origin) or (supplied and supplied != origin):
            raise HTTPException(status_code=403, detail="Same-origin runtime control is required")
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            raise HTTPException(status_code=403, detail="Cross-site runtime control is denied")

    @app.get("/api/standalone/runtime")
    def runtime_status(request: Request):
        local_request(request)
        return {"url": origin + "/", "pid": os.getpid(), "shutdown_token": shutdown_token}

    @app.post("/api/standalone/shutdown")
    def shutdown(request: Request, background: BackgroundTasks):
        local_request(request, mutation=True)
        supplied = request.headers.get("x-erp-shutdown-token", "")
        if not secrets.compare_digest(supplied, shutdown_token):
            raise HTTPException(status_code=403, detail="Runtime shutdown token is required")
        callback = getattr(app.state, "standalone_shutdown", None)
        if callback is None:
            raise HTTPException(status_code=503, detail="Runtime is still starting")
        background.add_task(callback)
        return {"stopping": True}

    app.mount("/", FrontendFiles(directory=frontend, html=True), name="standalone-frontend")
    return app


def _announce_when_ready(server, url: str, installation: Path, database: Path, finished):
    """Open the browser exactly once, after an actual successful HTTP request."""
    while not finished.wait(0.1):
        if not server.started:
            continue
        try:
            with urlopen(url + "api/health", timeout=1) as response:
                if response.status != 200:
                    continue
        except OSError:
            continue
        print(f"ERP 已啟動：{url}", flush=True)
        print("請保留此主控台；按 Ctrl+C 可安全結束 ERP。關閉瀏覽器不會結束服務。", flush=True)
        browser_error = None
        try:
            browser_opened = bool(webbrowser.open(url, new=2))
        except Exception as error:
            browser_opened, browser_error = False, str(error)
        if not browser_opened:
            print(f"預設瀏覽器未能自動開啟。請在本機瀏覽器開啟：{url}", flush=True)
        info = {"pid": os.getpid(), "port": int(url.rsplit(":", 1)[1].rstrip("/")),
                "url": url, "database": str(database), "browser_opened": browser_opened}
        if browser_error:
            info["browser_error"] = browser_error
        temporary = installation / "data" / ".startup.json.tmp"
        try:
            temporary.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(installation / "data" / "startup.json")
        except OSError as error:
            print(f"無法寫入啟動資訊：{error}", flush=True)
            server.should_exit = True
            return
        return


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not getattr(sys, "frozen", False):
        raise SystemExit("This entry point runs from the built TIGER-ERP.exe only.")
    installation = Path(sys.executable).resolve().parent
    startup = installation / "data" / "startup.json"
    try:
        with installation_lock(installation):
            startup.unlink(missing_ok=True)
            database = prepare_database(installation)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                # Binding port 0 reserves the selected port until Uvicorn owns it;
                # there is no close/rebind race and no development-port conflict.
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                if port in {5173, 8000}:
                    raise StartupError("系統選到開發服務保留連接埠；請重新啟動。")
                app = create_standalone_app(database, port, installation)
                server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                    proxy_headers=False, server_header=False, access_log=False,
                    loop="asyncio", http="h11", ws="none"))
                app.state.standalone_shutdown = lambda: setattr(server, "should_exit", True)
                finished = threading.Event()
                notifier = threading.Thread(target=_announce_when_ready,
                    args=(server, f"http://127.0.0.1:{port}/", installation, database, finished), daemon=True)
                notifier.start()
                try:
                    server.run(sockets=[listener])
                finally:
                    finished.set()
                    notifier.join(timeout=2)
                    startup.unlink(missing_ok=True)
                if not server.started:
                    raise StartupError("ERP 資料庫或服務啟動失敗。請保留 data 與 seed，檢查上方錯誤；未建立空白替代資料庫。")
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"TIGER-ERP 啟動失敗：{error}", file=sys.stderr, flush=True)
        if sys.stdin is not None and sys.stdin.isatty():
            input("按 Enter 關閉此視窗。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
