"""One genuine exported-EXE verification, orchestrated by the desktop workflow.

The verification Python belongs to the developer only. The extracted application
is launched directly with a Windows-only PATH and no developer runtime variables.
Only its independent database may receive the desktop test's work-center record.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts/standalone-export-2026-11-21'
STATE = ART / 'standalone-state.json'


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def database_view(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        quote = lambda n: '"' + n.replace('"', '""') + '"'
        return dict(
            rows={n:[list(r) for r in connection.execute('SELECT rowid,* FROM ' + quote(n) + ' ORDER BY rowid')] for n in names},
            schema=[list(r) for r in connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')],
            columns={n:[list(r) for r in connection.execute('PRAGMA table_xinfo(' + quote(n) + ')')] for n in names},
            integrity=connection.execute('PRAGMA integrity_check').fetchone()[0],
            foreign_key_violations=[list(r) for r in connection.execute('PRAGMA foreign_key_check')])


def expected_view():
    value = json.loads((ART/'owner-before.json').read_text(encoding='utf-8'))
    return {key:value[key] for key in ['rows','schema','columns','integrity','foreign_key_violations']}


def original_unchanged():
    assert database_view(ROOT/'data/erp.sqlite') == expected_view(), 'Original Owner database changed'


def request(url, method='GET', body=None, headers=None):
    data = None if body is None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method,
          headers={'Content-Type':'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def normalized(value):
    if isinstance(value, dict):
        return {k:normalized(v) for k,v in value.items() if k != 'as_of'}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def verify_business(state):
    url = state['url'].rstrip('/')
    originals = 'http://127.0.0.1:8000'
    result = {}
    # Six modules and exact reporting/stock values are checked from actual data.
    for route in ['/api/raw-materials','/api/purchase-orders','/api/work-orders',
                  '/api/sales-orders','/api/inventory-inquiry','/api/inventory-movements',
                  '/api/business-statistics?area=purchasing',
                  '/api/business-statistics?area=manufacturing',
                  '/api/business-statistics?area=sales','/api/lot-traceability?mode=options']:
        actual = request(url+route)
        assert normalized(actual) == normalized(request(originals+route)), route+' differs from live Owner sources'
        result[route] = actual
    options = result['/api/lot-traceability?mode=options']
    raw_lot = next(v for v in options['lots'] if v['item_kind']=='RAW')
    shipment = options['shipments'][0]
    for route in ['/api/lot-traceability?direction=forward&lot_id='+raw_lot['id'],
                  '/api/lot-traceability?direction=reverse&shipment_id='+shipment['id']]:
        actual=request(url+route)
        assert normalized(actual)==normalized(request(originals+route)), 'Directional genealogy differs'
        result[route]=actual
    # Startup must retain every registered posting capability, even though QA
    # never posts an Owner transaction or a copied historical business document.
    for resource in ['material-issues','material-returns','finished-goods-receipts',
                     'sales-shipments','customer-returns','stocktakes',
                     'inventory-adjustments','inventory-transfers']:
        policy=request(url+'/api/'+resource+'/policy')
        assert policy['posting_allowed'] is True, resource+' lost approved local-demo capability'
        result[resource+'-policy']=policy
    completion_policy=request(url+'/api/production-completions/policy')
    assert completion_policy['confirmation_allowed'] is True
    assert completion_policy['authorization_basis']=='LOCAL_DEMO_MANUAL'
    result['production-completions-policy']=completion_policy
    (ART/'standalone-business-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    state['business_API_and_bidirectional_traceability_verified']=True


def launch(state):
    installation=Path(state['installation'])
    startup=installation/'data/startup.json'
    if startup.exists():
        startup.unlink()  # This verifier owns only its generated startup marker.
    env={k:v for k,v in os.environ.items() if not k.upper().startswith(('PYTHON','NODE','ERP_'))}
    env['PATH']=str(Path(os.environ['SystemRoot'])/'System32')
    env['PYINSTALLER_RESET_ENVIRONMENT']='1'
    with (ART/'exe.stdout.log').open('ab') as stdout, (ART/'exe.stderr.log').open('ab') as stderr:
        process=subprocess.Popen([state['exe']],cwd=installation,env=env,stdout=stdout,stderr=stderr,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise RuntimeError('Extracted EXE exited '+str(process.returncode)+': '+(ART/'exe.stderr.log').read_text(encoding='utf-8',errors='replace'))
        if startup.exists():
            try:
                info=json.loads(startup.read_text(encoding='utf-8'))
                if info.get('browser_opened') is True and request(info['url'].rstrip('/')+'/api/health')['status']=='ok':
                    state.update(url=info['url'],pid=process.pid,browser_opened=True)
                    state['startup_information']=info
                    save(state)
                    return
            except (ValueError, OSError):
                pass
        time.sleep(.2)
    raise RuntimeError('EXE did not report ready/browser launch within 90 seconds')


def stop(state):
    url=state['url'].rstrip('/')
    info=request(url+'/api/standalone/runtime')
    # HTTP listeners close before Uvicorn completes database teardown. Wait for
    # the actual EXE exit so its installation lock is released before restart.
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.OpenProcess(0x00100000,False,info['pid'])
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        request(url+'/api/standalone/shutdown',method='POST',body={},
                headers={'Origin':url,'X-ERP-Shutdown-Token':info['shutdown_token']})
        if kernel.WaitForSingleObject(handle,20_000)!=0:
            raise RuntimeError('Standalone EXE did not exit after its graceful shutdown request')
    finally:
        kernel.CloseHandle(handle)


def start(zip_path):
    started=time.monotonic()
    original_unchanged()
    archive=Path(zip_path).resolve()
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None, 'ZIP CRC validation failed'
        for name in z.namelist():
            part=Path(name)
            assert not part.is_absolute() and '..' not in part.parts
            assert name.startswith('TIGER-ERP/')
        extraction=Path(tempfile.mkdtemp(prefix='TIGER-ERP-Standalone-'))
        z.extractall(extraction)
    installation=extraction/'TIGER-ERP'
    state=dict(zip=str(archive),zip_bytes=archive.stat().st_size,
               zip_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),zip_integrity=True,
               installation=str(installation),exe=str(installation/'TIGER-ERP.exe'),
               seed=str(installation/'seed/erp.sqlite3'),database=str(installation/'data/erp.sqlite3'))
    assert not Path(state['database']).exists()
    assert not installation.is_relative_to(ROOT)
    assert database_view(state['seed'])==expected_view(), 'ZIP seed is not the complete actual Owner snapshot'
    save(state)
    launch(state)
    assert not Path(state['database']).samefile(ROOT/'data/erp.sqlite')
    assert not Path(state['database']).samefile(state['seed'])
    assert database_view(state['database'])==expected_view(), 'First EXE startup changed the imported rows/schema'
    verify_business(state)
    original_unchanged()
    state.update(first_run_complete_equality=True,source_database_unchanged=True,start_wall_seconds=time.monotonic()-started)
    save(state)
    return state


def restart(record_id, expected_name):
    started=time.monotonic()
    state=json.loads(STATE.read_text(encoding='utf-8'))
    record=request(state['url'].rstrip('/')+'/api/work-centers/'+record_id)
    assert record['name']==expected_name and record['row_version']>=2
    before=database_view(state['database'])
    baseline=expected_view()
    for table,rows in baseline['rows'].items():
        current=before['rows'][table]
        if table=='work_center':
            assert len(current)==len(rows)+1
            assert all(row in current for row in rows)
        else:
            assert current==rows, table+' changed outside isolated new work-center record'
    assert before['schema']==baseline['schema'] and before['columns']==baseline['columns']
    assert before['integrity']=='ok' and before['foreign_key_violations']==[]
    stop(state)
    launch(state)
    persisted=request(state['url'].rstrip('/')+'/api/work-centers/'+record_id)
    assert persisted==record, 'Edited work-center was not preserved after EXE restart'
    assert database_view(state['database'])==before
    assert database_view(state['seed'])==baseline, 'Immutable seed was changed'
    original_unchanged()
    state.update(persistence_verified=True,mutation_record=record,active_database_preserved_on_restart=True,
                 source_database_unchanged=True,restart_wall_seconds=time.monotonic()-started)
    save(state)
    return state


def finish():
    state=json.loads(STATE.read_text(encoding='utf-8'))
    stop(state)
    original_unchanged()
    state['gracefully_stopped']=True
    state['source_database_unchanged']=True
    save(state)
    return state


if __name__=='__main__':
    ART.mkdir(parents=True,exist_ok=True)
    if sys.argv[1]=='start': result=start(sys.argv[2])
    elif sys.argv[1]=='restart': result=restart(sys.argv[2],sys.argv[3])
    elif sys.argv[1]=='finish': result=finish()
    else: raise SystemExit('Unknown verification stage')
    print(json.dumps(result,ensure_ascii=True))
