"""Standing Owner-approved local-demo manual posting, separate from enterprise authority."""
from base64 import urlsafe_b64decode,urlsafe_b64encode
from contextlib import asynccontextmanager,contextmanager
from contextvars import ContextVar
from datetime import datetime,timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit
from uuid import uuid4
from sqlalchemy import event,select
from .db import ROOT
from .decimal_text import exact_sum
from .stock_ledger import compare,negate,on_hand

DISCLOSURE="本機展示模式／尚未啟用正式電子簽核與身分權限驗證"
DEMO_MESSAGE=DISCLOSURE+"。Owner 已核准本機手動庫存過帳常設授權；本作業仍須逐單確認，不代表完整 D06 生產核准。"
# Implemented action registrations only. Future actions require their own validators/tests.
REGISTERED_LOCAL_DEMO_POSTINGS=frozenset({"material_issue","material_return","finished_goods_receipt","sales_shipment","customer_return","stocktake","inventory_adjustment","inventory_transfer"})
_request_runtime=ContextVar("issue_local_demo_request",default=None)
_posting_grant=ContextVar("issue_local_demo_grant",default=None)


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"),
        default=lambda x:x.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z"))


def observed_time():
    return datetime.now(timezone.utc)


class LocalDemoRuntime:
    def __init__(self,path,backend_port,frontend_port,bind_host):
        from .db import ROOT
        supplied=Path(path).absolute()
        path=supplied.resolve()
        if bind_host!="127.0.0.1" or supplied!=path:
            raise ValueError("Local demo requires explicit127.0.0.1 binding and a non-aliased project database")
        # A frozen portable installation owns exactly one adjacent writable DB.
        # This applies to every registered action runtime, without widening the
        # source/development factory's existing path or distinct-port rules.
        standalone = (getattr(sys,"frozen",False)
            and path == Path(sys.executable).resolve().parent / "data" / "erp.sqlite3")
        if not standalone and path!=(ROOT/"data/erp.sqlite").resolve() and not path.is_relative_to((ROOT/"artifacts").resolve()):
            raise ValueError("Local-demo database must be the project Owner database or an isolated project artifact")
        if (backend_port==frontend_port and not standalone) or not all(isinstance(p,int) and 1<=p<=65535 for p in [backend_port,frontend_port]):
            raise ValueError("Distinct valid loopback ports are required")
        self.path=path;self.backend_port=backend_port;self.frontend_port=frontend_port
        self.secret=secrets.token_bytes(32);self.id=str(uuid4())
        self.database=None;self.identity=None;self.live=False

    def intact(self):
        try:
            info=self.path.stat()
            return (self.live and self.path.resolve()==self.path and info.st_nlink==1
                and (info.st_dev,info.st_ino)==self.identity and self.database.path==self.path)
        except (OSError,AttributeError):
            return False

    def accepts(self,request):
        server=request.scope.get("server")
        peer=request.scope.get("client")
        if not self.intact() or request.app.state.db is not self.database:
            return False
        if not server or server[0]!="127.0.0.1" or server[1]!=self.backend_port or not peer or peer[0]!="127.0.0.1":
            return False
        # Host/Origin are additional restrictions, never a source of authority.
        def local_url(value):
            try:
                parsed=urlsplit(value)
                return parsed.scheme=="http" and parsed.hostname=="127.0.0.1" and parsed.port in {self.backend_port,self.frontend_port}
            except ValueError:
                return False
        if not local_url("http://"+request.headers.get("host","")):
            return False
        origin=request.headers.get("origin")
        return not origin or local_url(origin)


def runtime_for(session):
    runtime=_request_runtime.get()
    binding=session.get_bind()
    engine=getattr(binding,"engine",binding)
    if ("material_issue" in REGISTERED_LOCAL_DEMO_POSTINGS and runtime
        and runtime.intact() and runtime.database.engine is engine):
        return runtime
    return None


def current_grant(runtime):
    grant=_posting_grant.get()
    if (grant and grant["runtime"] is runtime and _request_runtime.get() is runtime
        and runtime.intact()):
        return grant
    return None


def create_local_demo_app(database_path,*,backend_port=8024,frontend_port=5187,bind_host="127.0.0.1",standalone_directory=None):
    """Dedicated loopback opt-in for explicitly registered manual inventory actions."""
    from .main import create_app
    if standalone_directory is not None:
        directory=Path(standalone_directory).absolute()
        if (not getattr(sys,"frozen",False) or directory!=directory.resolve()
            or directory!=Path(sys.executable).resolve().parent
            or Path(database_path).absolute()!=directory/"data"/"erp.sqlite3"):
            raise ValueError("Standalone demo requires the frozen executable's own data/erp.sqlite3")
    runtime=LocalDemoRuntime(database_path,backend_port,frontend_port,bind_host)
    app=create_app(runtime.path)
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            runtime.database=application.state.db
            stat=runtime.path.stat();runtime.identity=(stat.st_dev,stat.st_ino)
            runtime.live=True
            if not runtime.intact():
                raise RuntimeError("Local-demo database identity could not be established")
            engine=runtime.database.engine
            def configure(driver,_):
                actual=Path(next(r[2] for r in driver.execute("PRAGMA database_list") if r[1]=="main")).resolve()
                def grant_value(key,default=None):
                    grant=current_grant(runtime)
                    if (actual!=runtime.path or application.state.db is not runtime.database or runtime.database.engine is not engine
                        or (grant is not None and grant["connection"] is not driver)):
                        return default
                    return grant.get(key,default) if grant else default
                driver.create_function("erp_issue_manual_authorized",0,lambda:int(grant_value("document") is not None))
                driver.create_function("erp_issue_manual_document",0,lambda:grant_value("document"))
                driver.create_function("erp_issue_manual_digest",0,lambda:grant_value("digest"))
                driver.create_function("erp_issue_manual_snapshot",0,lambda:grant_value("snapshot"))
                driver.create_function("erp_issue_manual_time",0,lambda:grant_value("time_text"))
            event.listen(engine,"connect",configure);engine.dispose()
            try:
                yield
            finally:
                runtime.live=False
                event.remove(engine,"connect",configure);engine.dispose()
    app.router.lifespan_context=lifespan
    app.state.local_demo_runtime=runtime

    @app.middleware("http")
    async def scope_demo(request,call_next):
        token=_request_runtime.set(runtime if runtime.accepts(request) else None)
        try:
            return await call_next(request)
        finally:
            _request_runtime.reset(token)
    if "material_return" in REGISTERED_LOCAL_DEMO_POSTINGS:
        from .material_return_authorization import attach_material_return_local_demo_runtime
        app=attach_material_return_local_demo_runtime(app,runtime.path,backend_port=backend_port,
            frontend_port=frontend_port,bind_host=bind_host)
    # Production reporting has its own noninventory confirmation capability.
    from .completion_authorization import attach_completion_runtime
    app=attach_completion_runtime(app,runtime.path,backend_port=backend_port,
        frontend_port=frontend_port,bind_host=bind_host)
    if "finished_goods_receipt" in REGISTERED_LOCAL_DEMO_POSTINGS:
        from .finished_goods_authorization import attach_finished_goods_local_demo_runtime
        app=attach_finished_goods_local_demo_runtime(app,runtime.path,backend_port=backend_port,
            frontend_port=frontend_port,bind_host=bind_host)
    if "sales_shipment" in REGISTERED_LOCAL_DEMO_POSTINGS:
        from .sales_shipment_authorization import attach_sales_shipment_local_demo_runtime
        app=attach_sales_shipment_local_demo_runtime(app,runtime.path,backend_port=backend_port,
            frontend_port=frontend_port,bind_host=bind_host)
    if "customer_return" in REGISTERED_LOCAL_DEMO_POSTINGS:
        from .customer_return_authorization import attach_customer_return_local_demo_runtime
        app=attach_customer_return_local_demo_runtime(app,runtime.path,backend_port=backend_port,
            frontend_port=frontend_port,bind_host=bind_host)
    if "stocktake" in REGISTERED_LOCAL_DEMO_POSTINGS:
        from .stocktake_authorization import attach_stocktake_local_demo_runtime
        app=attach_stocktake_local_demo_runtime(app,runtime.path,backend_port=backend_port,
            frontend_port=frontend_port,bind_host=bind_host)
    if "inventory_adjustment" in REGISTERED_LOCAL_DEMO_POSTINGS:
        from .adjustment_authorization import attach_adjustment_local_demo_runtime
        app=attach_adjustment_local_demo_runtime(app,runtime.path,backend_port=backend_port,
            frontend_port=frontend_port,bind_host=bind_host)
    if "inventory_transfer" in REGISTERED_LOCAL_DEMO_POSTINGS:
        from .transfer_authorization import attach_transfer_local_demo_runtime
        app=attach_transfer_local_demo_runtime(app,runtime.path,backend_port=backend_port,
            frontend_port=frontend_port,bind_host=bind_host)
    return app


def source_snapshot(session,parent,lines):
    """All sources and unique stock positions from the caller's single snapshot."""
    from .models import Item,Unit,Warehouse
    from .bom_models import Bom,BomComponent
    from .routing_models import Routing
    from .work_order_models import WorkOrder
    from .receiving_models import StockPosition,InventoryLot
    def fields(row):
        return {column.name:getattr(row,column.name) for column in row.__table__.columns} if row else None
    wo=session.get(WorkOrder,parent.work_order_id)
    positions={}
    details=[]
    for line in sorted(lines,key=lambda x:(x.line_no,x.id)):
        item=session.get(Item,line.material_id);unit=session.get(Unit,line.unit_id)
        position=session.get(StockPosition,line.position_id)
        if position.id not in positions:
            positions[position.id]=dict(position=fields(position),warehouse=fields(session.get(Warehouse,position.warehouse_id)),
                lot=fields(session.get(InventoryLot,position.lot_id)) if position.lot_id else None,
                physical_qty_base=on_hand(session,position.id))
        details.append(dict(line=fields(line),material=fields(item),unit=fields(unit),
            component=fields(session.get(BomComponent,line.bom_component_id)) if line.bom_component_id else None))
    # Round-trip datetime values to canonical text so HMAC payload and current
    # values compare structurally without changing business representations.
    return json.loads(canonical(dict(issue=fields(parent),work_order=fields(wo),
        bom=fields(session.get(Bom,parent.bom_id)) if parent.bom_id else None,
        routing=fields(session.get(Routing,parent.routing_id)) if parent.routing_id else None,
        lines=details,positions=positions)))


def validate_outbound(session,parent,lines,fail,validate_header,validate_line):
    validate_header(session,parent.work_order_id,parent.work_order_version,parent)
    if not lines:
        fail(422,"領料單沒有明細，不能確認過帳。")
    demands={}
    for line in lines:
        validate_line(session,parent,line)
        if compare(line.quantity,"0")<=0:
            fail(422,"本機展示實際領料須為正向扣庫量；有號或零量仍可保存草稿。")
        demands.setdefault(line.position_id,[]).append(line.quantity)
    for position,quantities in demands.items():
        if compare(exact_sum(quantities),on_hand(session,position))>0:
            fail(422,"同倉同批的合計領料量超過實際庫存；沒有執行任何部分扣庫。")


def make_review(session,parent,fail,validate_header,validate_line):
    from .issue_models import MaterialIssueLine,MaterialIssuePosting
    runtime=runtime_for(session)
    if runtime is None:
        fail(403,"目前不是明確授權的本機展示環境，不能確認領料過帳。")
    if session.scalar(select(MaterialIssuePosting.id).where(MaterialIssuePosting.material_issue_id==parent.id)):
        fail(409,"領料單已有過帳紀錄，只能檢視。")
    lines=list(session.scalars(select(MaterialIssueLine).where(MaterialIssueLine.material_issue_id==parent.id)))
    validate_outbound(session,parent,lines,fail,validate_header,validate_line)
    payload=dict(runtime=runtime.id,review_id=str(uuid4()),reviewed_at=observed_time(),
        snapshot=source_snapshot(session,parent,lines))
    raw=canonical(payload).encode()
    encoded=urlsafe_b64encode(raw).decode().rstrip("=")
    signature=hmac.new(runtime.secret,encoded.encode(),hashlib.sha256).hexdigest()
    movements=[dict(issue_line_id=line.id,position_id=line.position_id,qty_base_delta=negate(line.quantity),
        unit_code=next(d["unit"]["code"] for d in payload["snapshot"]["lines"] if d["line"]["id"]==line.id))
        for line in sorted(lines,key=lambda x:(x.line_no,x.id))]
    return encoded+"."+signature,movements


@contextmanager
def verified_confirmation(session,parent,lines,token,fail):
    runtime=runtime_for(session)
    if runtime is None:
        fail(403,"目前不是明確授權的本機展示環境，不能正式扣庫存。")
    if not token:
        fail(403,"請先檢視並明確確認本次領料扣庫內容。")
    try:
        encoded,signature=token.split(".",1)
        expected=hmac.new(runtime.secret,encoded.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):
            raise ValueError("Invalid signature")
        raw=urlsafe_b64decode(encoded+"="*(-len(encoded)%4))
        payload=json.loads(raw)
    except (ValueError,TypeError,UnicodeDecodeError):
        fail(403,"領料確認內容無效或服務已重新啟動，請重新檢視後確認。")
    if payload.get("runtime")!=runtime.id:
        fail(403,"領料確認不屬於目前服務，請重新檢視後確認。")
    current=source_snapshot(session,parent,lines)
    if payload.get("snapshot")!=current:
        fail(409,"領料內容、來源資料或實體庫存已變更，請重新檢視後確認。")
    snapshot=canonical(payload)
    digest=hashlib.sha256(snapshot.encode()).hexdigest()
    received=observed_time()
    grant=dict(runtime=runtime,connection=session.connection().connection.driver_connection,
        document=parent.id,digest=digest,snapshot=snapshot,received_at=received,
        time_text=received.isoformat(timespec="microseconds").replace("+00:00","Z"))
    handle=_posting_grant.set(grant)
    try:
        yield grant
    finally:
        _posting_grant.reset(handle)
