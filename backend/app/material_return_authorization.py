"""Independent signed return grants for registered local-demo and isolated QA."""
from base64 import urlsafe_b64decode,urlsafe_b64encode
from contextlib import asynccontextmanager,contextmanager
from contextvars import ContextVar
import hashlib,hmac,json
from pathlib import Path
from uuid import uuid4
from sqlalchemy import event
from .db import ROOT
from .local_demo import LocalDemoRuntime,canonical,observed_time,DISCLOSURE

_request=ContextVar('material_return_request',default=None)
_grant=ContextVar('material_return_transaction_grant',default=None)
CLOSED_MESSAGE=DISCLOSURE+'。本服務未啟用本機手動退料過帳授權；可保存草稿與唯讀檢視，請使用專用本機展示服務。'
LOCAL_DEMO_MESSAGE=DISCLOSURE+'。Owner 已核准本機手動庫存過帳常設授權；每張退料單仍須檢視實際來源與數量後明確確認，不代表正式身分驗證或電子簽核。'
QA_MESSAGE=DISCLOSURE+'。目前僅為隔離 QA 生產退料測試授權；測試交易不代表 Owner 已完成人工驗收。'


def runtime_for(session):
    runtime=_request.get()
    bind=session.get_bind();engine=getattr(bind,'engine',bind)
    if not runtime or not runtime.intact() or runtime.database.engine is not engine:return None
    if runtime.material_return_basis=='LOCAL_DEMO_MANUAL':
        from .local_demo import REGISTERED_LOCAL_DEMO_POSTINGS
        if 'material_return' not in REGISTERED_LOCAL_DEMO_POSTINGS:return None
    return runtime


def policy(session):
    runtime=runtime_for(session)
    if runtime:
        basis=runtime.material_return_basis
        return dict(posting_allowed=True,authorization_basis=basis,
            message=LOCAL_DEMO_MESSAGE if basis=='LOCAL_DEMO_MANUAL' else QA_MESSAGE)
    return dict(posting_allowed=False,authorization_basis=None,message=CLOSED_MESSAGE)


def attach_material_return_qa_runtime(app,database_path,*,backend_port=8026,frontend_port=5189,bind_host='127.0.0.1'):
    """Explicit test-only artifact capability; never accepts the Owner database."""
    supplied=Path(database_path).absolute();path=supplied.resolve()
    if supplied!=path or not path.is_relative_to((ROOT/'artifacts').resolve()):
        raise ValueError('Manufacturing return QA capability requires an isolated project artifact database')
    owner=(ROOT/'data/erp.sqlite').resolve()
    if path==owner or (path.exists() and owner.exists() and path.samefile(owner)):
        raise ValueError('Owner database cannot receive return QA authorization')
    runtime=LocalDemoRuntime(path,backend_port,frontend_port,bind_host)
    return _attach_return_runtime(app,runtime,'CONTROLLED_QA_ONLY')


def attach_material_return_local_demo_runtime(app,database_path,*,backend_port=8024,frontend_port=5187,bind_host='127.0.0.1'):
    """Only the explicit demo factory registers this implemented return action."""
    from .local_demo import REGISTERED_LOCAL_DEMO_POSTINGS
    if 'material_return' not in REGISTERED_LOCAL_DEMO_POSTINGS:
        raise ValueError('Manufacturing return is not a registered local-demo posting operation')
    runtime=LocalDemoRuntime(database_path,backend_port,frontend_port,bind_host)
    return _attach_return_runtime(app,runtime,'LOCAL_DEMO_MANUAL')


def _attach_return_runtime(app,runtime,basis):
    if basis not in {'CONTROLLED_QA_ONLY','LOCAL_DEMO_MANUAL'}:
        raise ValueError('Unsupported manufacturing return authorization basis')
    runtime.material_return_basis=basis
    path=runtime.path
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            runtime.database=application.state.db
            info=path.stat();runtime.identity=(info.st_dev,info.st_ino);runtime.live=True
            if not runtime.intact():raise RuntimeError('Return runtime database identity mismatch')
            engine=runtime.database.engine
            def configure(driver,_):
                actual=Path(next(row[2] for row in driver.execute('PRAGMA database_list') if row[1]=='main')).resolve()
                def value(key,default=None):
                    grant=_grant.get()
                    if (not grant or grant['runtime'] is not runtime or _request.get() is not runtime
                        or not runtime.intact() or actual!=path or application.state.db is not runtime.database
                        or runtime.database.engine is not engine or grant['connection'] is not driver
                        or grant['basis']!=basis or runtime.material_return_basis!=basis):
                        return default
                    return grant.get(key,default)
                driver.create_function('erp_material_return_authorized',0,lambda:int(value('document') is not None))
                for name,key in [('basis','basis'),('document','document'),('digest','digest'),('snapshot','snapshot'),('time','time_text')]:
                    driver.create_function('erp_material_return_'+name,0,lambda key=key:value(key))
            event.listen(engine,'connect',configure);engine.dispose()
            try:yield
            finally:
                runtime.live=False;event.remove(engine,'connect',configure);engine.dispose()
    app.router.lifespan_context=lifespan
    if basis=='CONTROLLED_QA_ONLY':app.state.material_return_qa_runtime=runtime
    else:app.state.material_return_local_demo_runtime=runtime
    @app.middleware('http')
    async def scope(request,call_next):
        token=_request.set(runtime if runtime.accepts(request) else None)
        try:return await call_next(request)
        finally:_request.reset(token)
    return app


def make_token(session,snapshot):
    runtime=runtime_for(session)
    if runtime is None:return None
    payload=dict(operation='manufacturing-material-return',authorization_basis=runtime.material_return_basis,
        runtime=runtime.id,review_id=str(uuid4()),
        reviewed_at=observed_time(),snapshot=snapshot)
    encoded=urlsafe_b64encode(canonical(payload).encode()).decode().rstrip('=')
    return encoded+'.'+hmac.new(runtime.secret,encoded.encode(),hashlib.sha256).hexdigest()


@contextmanager
def verified_confirmation(session,parent,token,current,fail):
    runtime=runtime_for(session)
    if runtime is None:fail(403,CLOSED_MESSAGE)
    if not token:fail(403,'請先檢視並明確確認本次生產退料入庫內容。')
    try:
        encoded,signature=token.split('.',1)
        expected=hmac.new(runtime.secret,encoded.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):raise ValueError('Invalid signature')
        payload=json.loads(urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        if not isinstance(payload,dict):raise ValueError('Invalid payload')
    except (ValueError,TypeError,UnicodeDecodeError):
        fail(403,'退料確認內容無效或服務已重新啟動，請重新檢視。')
    if (payload.get('operation')!='manufacturing-material-return' or payload.get('runtime')!=runtime.id
        or payload.get('authorization_basis')!=runtime.material_return_basis):
        fail(403,'退料確認不屬於目前作業或服務，請重新檢視。')
    if payload.get('snapshot')!=current:
        fail(409,'退料內容、來源、已退量或庫存已變更，請重新檢視後確認。')
    snapshot=canonical(payload);received=observed_time()
    grant=dict(runtime=runtime,connection=session.connection().connection.driver_connection,
        document=parent.id,basis=runtime.material_return_basis,digest=hashlib.sha256(snapshot.encode()).hexdigest(),
        snapshot=snapshot,received_at=received,time_text=received.isoformat(timespec='microseconds').replace('+00:00','Z'))
    handle=_grant.set(grant)
    try:yield grant
    finally:_grant.reset(handle)
