"""Explicit noninventory local-demo confirmation, independent of stock grants."""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import json
from uuid import uuid4
from sqlalchemy import event
from .local_demo import LocalDemoRuntime, canonical, DISCLOSURE
from .models import now_utc

_request = ContextVar("completion_request", default=None)
_grant = ContextVar("completion_confirmation", default=None)
MESSAGE = DISCLOSURE + "。完工回報只記錄生產結果，不是正式電子簽核、工單發放或成品入庫。"


def register_denied_sql(driver):
    for key in ["document", "fingerprint", "snapshot", "time"]:
        driver.create_function("erp_completion_" + key, 0, lambda: None)


def attach_completion_runtime(app, path, *, backend_port, frontend_port, bind_host):
    runtime = LocalDemoRuntime(path, backend_port, frontend_port, bind_host)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            runtime.database = application.state.db
            info = runtime.path.stat()
            runtime.identity = (info.st_dev, info.st_ino)
            runtime.live = True
            if not runtime.intact():
                raise RuntimeError("Completion runtime database identity unavailable")
            engine = runtime.database.engine

            def configure(driver, _):
                def value(key):
                    grant = _grant.get()
                    if (runtime_for_engine(engine) is not runtime or not grant
                        or grant["runtime"] is not runtime or grant["connection"] is not driver):
                        return None
                    return grant[key]
                for key in ["document", "fingerprint", "snapshot", "time"]:
                    driver.create_function("erp_completion_" + key, 0, lambda key=key: value(key))
            event.listen(engine, "connect", configure)
            engine.dispose()
            try:
                yield
            finally:
                runtime.live = False
                event.remove(engine, "connect", configure)
                engine.dispose()
    app.router.lifespan_context = lifespan
    app.state.completion_runtime = runtime

    @app.middleware("http")
    async def scope(request, call_next):
        marker = _request.set(runtime if runtime.accepts(request) else None)
        try:
            return await call_next(request)
        finally:
            _request.reset(marker)
    return app


def runtime_for_engine(engine):
    runtime = _request.get()
    return runtime if runtime and runtime.intact() and runtime.database.engine is engine else None


def runtime_for(session):
    binding = session.get_bind()
    return runtime_for_engine(getattr(binding, "engine", binding))


def policy(session):
    return dict(confirmation_allowed=runtime_for(session) is not None,
                authorization_basis="LOCAL_DEMO_MANUAL" if runtime_for(session) else None,
                message=MESSAGE if runtime_for(session) else "目前執行環境未啟用本機手動完工確認；請使用專用 localhost 展示啟動器。")


def review_token(session, snapshot):
    runtime = runtime_for(session)
    if not runtime:
        return None
    payload = canonical(dict(operation="production-completion", runtime=runtime.id,
                             review_id=str(uuid4()), snapshot=snapshot)).encode()
    return urlsafe_b64encode(payload).decode() + "." + hmac.new(runtime.secret, payload, hashlib.sha256).hexdigest()


@contextmanager
def confirmation_grant(session, row, data, snapshot, fingerprint, fail):
    runtime = runtime_for(session)
    if not runtime:
        fail(403, "本機完工確認未啟用；草稿與庫存均未變更。")
    try:
        encoded, signature = data.confirmation_token.split(".")
        payload = urlsafe_b64decode(encoded.encode())
        expected = hmac.new(runtime.secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        content = json.loads(payload)
        if (content.get("operation") != "production-completion" or content.get("runtime") != runtime.id
            or content.get("snapshot") != snapshot or not content.get("review_id")):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        fail(409, "確認內容、來源或累計進度已變更，或服務已重新啟動；請重新檢視完工回報。")
    time = now_utc()
    grant = dict(runtime=runtime, connection=session.connection().connection.driver_connection,
                 document=row.id, fingerprint=fingerprint, snapshot=canonical(snapshot),
                 time=time.isoformat(timespec="microseconds").replace("+00:00", "Z"))
    marker = _grant.set(grant)
    try:
        yield time, grant["snapshot"]
    finally:
        _grant.reset(marker)
