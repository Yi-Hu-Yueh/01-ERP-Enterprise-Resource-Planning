"""Operation-specific customer-return grants using the established local-demo mechanism."""
from base64 import urlsafe_b64decode,urlsafe_b64encode
from contextlib import asynccontextmanager,contextmanager
from contextvars import ContextVar
import hashlib,hmac,json
from pathlib import Path
from uuid import uuid4
from datetime import datetime,timezone
from sqlalchemy import event
from .db import ROOT
from .local_demo import LocalDemoRuntime,canonical,observed_time,DISCLOSURE

_request=ContextVar('customer_return_request',default=None)
_grant=ContextVar('customer_return_transaction_grant',default=None)
REVIEW_TTL_SECONDS=600
CLOSED_MESSAGE=DISCLOSURE+'。本服務未啟用本機手動客戶退貨過帳授權；可保存草稿與唯讀檢視，請使用專用本機展示服務。'
LOCAL_DEMO_MESSAGE=DISCLOSURE+'。Owner 已核准本機手動庫存過帳常設授權；每張客戶退貨單仍須檢視真實出貨來源、退貨量及是否可回庫後明確確認；不回庫登記不增加庫存，不代表正式身分驗證或電子簽核。'


def runtime_for(session):
    runtime=_request.get()
    bind=session.get_bind();engine=getattr(bind,'engine',bind)
    if not runtime or not runtime.intact() or runtime.database.engine is not engine:return None
    if runtime.customer_return_basis=='LOCAL_DEMO_MANUAL':
        from .local_demo import REGISTERED_LOCAL_DEMO_POSTINGS
        if 'customer_return' not in REGISTERED_LOCAL_DEMO_POSTINGS:return None
    return runtime


def policy(session):
    runtime=runtime_for(session)
    if runtime:
        basis=runtime.customer_return_basis
        return dict(posting_allowed=True,authorization_basis=basis,
            message=LOCAL_DEMO_MESSAGE)
    return dict(posting_allowed=False,authorization_basis=None,message=CLOSED_MESSAGE)


def attach_customer_return_local_demo_runtime(app,database_path,*,backend_port=8024,frontend_port=5187,bind_host='127.0.0.1'):
    """Only the explicit demo factory registers this implemented customer-return action."""
    from .local_demo import REGISTERED_LOCAL_DEMO_POSTINGS
    if 'customer_return' not in REGISTERED_LOCAL_DEMO_POSTINGS:
        raise ValueError('Customer return is not a registered local-demo posting operation')
    runtime=LocalDemoRuntime(database_path,backend_port,frontend_port,bind_host)
    return _attach_customer_return_runtime(app,runtime,'LOCAL_DEMO_MANUAL')


def _attach_customer_return_runtime(app,runtime,basis):
    if basis!='LOCAL_DEMO_MANUAL':
        raise ValueError('Unsupported customer return authorization basis')
    runtime.customer_return_basis=basis
    path=runtime.path
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            runtime.database=application.state.db
            info=path.stat();runtime.identity=(info.st_dev,info.st_ino);runtime.live=True
            if not runtime.intact():raise RuntimeError('Customer-return runtime database identity mismatch')
            engine=runtime.database.engine
            def configure(driver,_):
                actual=Path(next(row[2] for row in driver.execute('PRAGMA database_list') if row[1]=='main')).resolve()
                def value(key,default=None):
                    grant=_grant.get()
                    if (not grant or grant['runtime'] is not runtime or _request.get() is not runtime
                        or not runtime.intact() or actual!=path or application.state.db is not runtime.database
                        or runtime.database.engine is not engine or grant['connection'] is not driver
                        or grant['basis']!=basis or runtime.customer_return_basis!=basis):
                        return default
                    return grant.get(key,default)
                driver.create_function('erp_customer_return_authorized',0,lambda:int(value('document') is not None))
                for name,key in [('document','document'),('digest','digest'),('snapshot','snapshot'),('time','time_text')]:
                    driver.create_function('erp_customer_return_'+name,0,lambda key=key:value(key))
            event.listen(engine,'connect',configure);engine.dispose()
            try:yield
            finally:
                runtime.live=False;event.remove(engine,'connect',configure);engine.dispose()
    app.router.lifespan_context=lifespan
    app.state.customer_return_local_demo_runtime=runtime
    @app.middleware('http')
    async def scope(request,call_next):
        token=_request.set(runtime if runtime.accepts(request) else None)
        try:return await call_next(request)
        finally:_request.reset(token)
    return app


def make_token(session,snapshot):
    runtime=runtime_for(session)
    if runtime is None:return None
    payload=dict(operation='customer-return',authorization_basis=runtime.customer_return_basis,
        runtime=runtime.id,review_id=str(uuid4()),
        reviewed_at=observed_time(),snapshot=snapshot)
    encoded=urlsafe_b64encode(canonical(payload).encode()).decode().rstrip('=')
    return encoded+'.'+hmac.new(runtime.secret,encoded.encode(),hashlib.sha256).hexdigest()


@contextmanager
def verified_confirmation(session,parent,token,current,fail):
    runtime=runtime_for(session)
    if runtime is None:fail(403,CLOSED_MESSAGE)
    if not token:fail(403,'請先檢視並明確確認本次客戶退貨內容。')
    try:
        encoded,signature=token.split('.',1)
        expected=hmac.new(runtime.secret,encoded.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):raise ValueError('Invalid signature')
        payload=json.loads(urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        if not isinstance(payload,dict):raise ValueError('Invalid payload')
    except (ValueError,TypeError,UnicodeDecodeError):
        fail(403,'客戶退貨確認內容無效或服務已重新啟動，請重新檢視。')
    if (payload.get('operation')!='customer-return' or payload.get('runtime')!=runtime.id
        or payload.get('authorization_basis')!=runtime.customer_return_basis):
        fail(403,'客戶退貨確認不屬於目前作業或服務，請重新檢視。')
    try:
        reviewed=datetime.fromisoformat(payload['reviewed_at'].replace('Z','+00:00'))
        if reviewed.tzinfo is None:raise ValueError('Missing timezone')
        age=(observed_time()-reviewed.astimezone(timezone.utc)).total_seconds()
        if age<0 or age>REVIEW_TTL_SECONDS:raise ValueError('Expired review')
        if not isinstance(payload.get('review_id'),str) or not payload['review_id']:raise ValueError('Missing review identity')
    except (KeyError,TypeError,ValueError,OverflowError):
        fail(409,'客戶退貨確認已逾期或時間無效，請重新檢視後確認。')
    if payload.get('snapshot')!=current:
        fail(409,'客戶退貨內容、來源、累計已退貨量、回庫選擇或庫存已變更，請重新檢視後確認。')
    snapshot=canonical(payload);received=observed_time()
    grant=dict(runtime=runtime,connection=session.connection().connection.driver_connection,
        document=parent.id,basis=runtime.customer_return_basis,digest=hashlib.sha256(snapshot.encode()).hexdigest(),
        snapshot=snapshot,received_at=received,time_text=received.isoformat(timespec='microseconds').replace('+00:00','Z'))
    handle=_grant.set(grant)
    try:yield grant
    finally:_grant.reset(handle)


def register_denied_sql(driver):
    driver.create_function('erp_customer_return_authorized',0,lambda:0)
    for name in ['document','digest','snapshot','time']:
        driver.create_function('erp_customer_return_'+name,0,lambda:None)
