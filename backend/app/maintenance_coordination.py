"""Drain HTTP database users and serialize maintenance, including export workers.

No thread-owned/reentrant mutex is held across an ASGI await. The short condition
protects counters only; a transferable lease owns the maintenance interval.
"""
import os
from pathlib import Path
import threading
import time

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class MaintenanceLease:
    def __init__(self, coordinator, handle):
        self.coordinator, self.handle = coordinator, handle

    def release(self):
        with self.coordinator.condition:
            if self.handle is None:
                return
            handle, self.handle = self.handle, None
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                self.coordinator.operation = None
                self.coordinator.condition.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class MaintenanceCoordinator:
    def __init__(self):
        self.condition = threading.Condition(threading.Lock())
        self.operation = None
        self.active = 0

    def current_operation(self):
        with self.condition:
            return self.operation

    def acquire(self, operation, database_path, timeout=15):
        with self.condition:
            if self.operation is not None:
                raise HTTPException(409, "已有 SQLite 維護或獨立系統匯出進行中，請稍候。")
            self.operation = operation
            deadline = time.monotonic() + timeout
            while self.active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.operation = None
                    self.condition.notify_all()
                    raise HTTPException(409, "既有資料要求尚未完成，請稍後重新操作。")
                self.condition.wait(remaining)
        handle = None
        try:
            root = Path(database_path).resolve().parent / "backups" / "sqlite"
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink() or root.resolve() != root.absolute():
                raise RuntimeError("備份目錄不可是連結。")
            path = root / ".operation.lock"
            if path.is_symlink():
                raise RuntimeError("維護鎖定檔不可是連結。")
            handle = path.open("a+b")
            if path.stat().st_nlink != 1:
                raise RuntimeError("維護鎖定檔不可共用硬連結。")
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return MaintenanceLease(self, handle)
        except Exception as exc:
            if handle is not None:
                handle.close()
            with self.condition:
                self.operation = None
                self.condition.notify_all()
            raise HTTPException(409, "另一個程序正在維護此資料庫，或維護鎖無法使用：" + str(exc)) from exc


class MaintenanceRequestGate:
    def __init__(self, app, coordinator):
        self.app, self.coordinator = app, coordinator

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        business = (scope["type"] == "http" and path.startswith("/api/")
                    and not path.startswith(("/api/sqlite-maintenance/", "/api/standalone-export/")))
        if not business:
            return await self.app(scope, receive, send)
        with self.coordinator.condition:
            busy = self.coordinator.operation is not None
            if not busy:
                self.coordinator.active += 1
        if busy:
            response = JSONResponse({"message": "SQLite 維護中，資料要求已暫停；請稍後重新整理。"},
                                    status_code=503, headers={"Retry-After": "2", "Cache-Control": "no-store"})
            return await response(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            with self.coordinator.condition:
                self.coordinator.active -= 1
                self.coordinator.condition.notify_all()


def install_maintenance_coordination(app):
    if not hasattr(app.state, "maintenance_coordinator"):
        app.state.maintenance_coordinator = MaintenanceCoordinator()
        app.add_middleware(MaintenanceRequestGate, coordinator=app.state.maintenance_coordinator)
    return app.state.maintenance_coordinator
