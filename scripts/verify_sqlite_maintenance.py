"""Native maintenance QA on a newly extracted database COPY, never the Owner DB.

The desktop workflow calls these phases. The distribution itself needs neither
this verifier nor an installed Python runtime.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts/sqlite-maintenance-2026-11-21'
STATE = ART / 'native-state.json'
spec = importlib.util.spec_from_file_location('native_export_verification', ROOT / 'scripts/verify_standalone_export.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
# Reuse only the verified portable EXE lifecycle and raw SQLite reader. Evidence
# is explicitly redirected so no previous task's records are rewritten.
native.ART, native.STATE = ART, STATE
view, request = native.database_view, native.request


def baseline():
    document = json.loads((ART/'owner-before.json').read_text(encoding='utf-8'))
    return {key:document[key] for key in ['rows','schema','columns','integrity','foreign_key_violations']}


def owner_unchanged():
    source = Path(json.loads((ART/'owner-before.json').read_text(encoding='utf-8'))['source'])
    assert view(source) == baseline(), 'Original Owner database changed'


def state():
    result=json.loads(STATE.read_text(encoding='utf-8'))
    database=Path(result['database']).resolve()
    owner=Path(json.loads((ART/'owner-before.json').read_text(encoding='utf-8'))['source']).resolve()
    assert database == Path(result['installation']).resolve()/'data/erp.sqlite3'
    assert database != owner and not database.samefile(owner)
    assert str(database).startswith(str(Path(tempfile.gettempdir()).resolve()))
    return result


def save(result):
    STATE.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


def backup_hashes(result):
    directory=Path(result['database']).parent/'backups/sqlite'
    return {p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file() and p.suffix in {'.sqlite','.sqlite3'}} if directory.exists() else {}


def check_policies(result):
    for name in ['material-issues','material-returns','finished-goods-receipts','sales-shipments',
                 'customer-returns','stocktakes','inventory-adjustments','inventory-transfers']:
        policy=request(result['url']+'/api/'+name+'/policy')
        assert policy['posting_allowed'], name+' lost its database-bound local capability'
    assert request(result['url']+'/api/production-completions/policy')['confirmation_allowed']


def start(zip_path):
    started=time.monotonic();owner_unchanged()
    archive=Path(zip_path).resolve()
    directory=Path(tempfile.mkdtemp(prefix='TIGER-ERP-SQLiteMaintenance-')).resolve()
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for name in z.namelist():
            path=Path(name)
            assert name.startswith('TIGER-ERP/') and not path.is_absolute() and '..' not in path.parts
            assert (directory/path).resolve().is_relative_to(directory)
        z.extractall(directory)
    installation=directory/'TIGER-ERP'
    result={'installation':str(installation),'exe':str(installation/'TIGER-ERP.exe'),
            'seed':str(installation/'seed/erp.sqlite3'),'database':str(installation/'data/erp.sqlite3'),
            'zip':str(archive),'zip_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    assert view(result['seed']) == baseline()
    assert not Path(result['database']).exists()
    save(result);native.launch(result)
    result['url']=result['url'].rstrip('/')
    assert view(result['database']) == baseline()
    assert not backup_hashes(result)
    check_policies(result)
    result.update(first_run_all_rows_equal=True,start_seconds=time.monotonic()-started)
    save(result);owner_unchanged()
    return result


def check_phase(phase):
    result=state();current=view(result['database']);expected=baseline()
    assert current['schema']==expected['schema'] and current['columns']==expected['columns']
    assert current['integrity']=='ok' and current['foreign_key_violations']==[]
    if phase in {'unchanged','restored'}:
        assert current==expected, 'All original row content/rowids must match'
    elif phase=='empty':
        actor_columns=[v[1] for v in expected['columns']['actor']]
        position=1+actor_columns.index('id')
        actor=[row for row in expected['rows']['actor'] if row[position]=='00000000-0000-4000-8000-000000000001']
        assert len(actor)==1
        for table,rows in current['rows'].items():
            preserved=actor if table=='actor' else expected['rows'][table] if table=='alembic_version' else []
            assert rows==preserved, 'Clear left business rows or lost necessary technical rows: '+table
    else:
        raise ValueError('Unknown verification phase')
    if phase=='restored':
        check_policies(result)
        reports={area:request(result['url']+'/api/business-statistics?area='+area)
                 for area in ['purchasing','manufacturing','sales']}
        reports['inventory']=request(result['url']+'/api/inventory-inquiry')
        options=request(result['url']+'/api/lot-traceability?mode=options')
        raw=next(row for row in options['lots'] if row['item_kind']=='RAW')
        reports['forward']=request(result['url']+'/api/lot-traceability?direction=forward&lot_id='+raw['id'])
        reports['reverse']=request(result['url']+'/api/lot-traceability?direction=reverse&shipment_id='+options['shipments'][0]['id'])
        (ART/'restored-business-api-evidence.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    result[phase+'_verified']=True;save(result);owner_unchanged()
    return {'phase':phase,'verified':True,'tables':len(current['rows']),
            'rows':sum(map(len,current['rows'].values())),'schema_unchanged':True,
            'backup_files':len(backup_hashes(result))}


def checkpoint(name, compare=False):
    assert re.fullmatch(r'[a-z0-9-]+',name)
    result=state();current=view(result['database'])
    digest=hashlib.sha256(json.dumps(current,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    value={'database_sha256':digest,'backups':backup_hashes(result)}
    location=ART/('checkpoint-'+name+'.json')
    if compare:
        assert json.loads(location.read_text(encoding='utf-8'))==value,'Canceled dialog mutated data or backup files'
    else:
        location.write_text(json.dumps(value,indent=2),encoding='utf-8')
    owner_unchanged()
    return {'checkpoint':name,'unchanged':compare,'captured':not compare}


def restart():
    started=time.monotonic();result=state();before=view(result['database']);files=backup_hashes(result)
    assert files,'Restart must exercise existing durable SQLite backups'
    native.stop(result);native.launch(result);result['url']=result['url'].rstrip('/')
    assert view(result['database'])==before
    assert backup_hashes(result)==files
    check_policies(result);owner_unchanged()
    result.update(restart_verified=True,backups_persisted=True,restart_seconds=time.monotonic()-started)
    save(result);return result


def finish():
    result=state();native.stop(result);owner_unchanged()
    assert view(result['seed'])==baseline()
    result.update(gracefully_stopped=True,Owner_database_unchanged=True,
                  retained_backup_files=backup_hashes(result))
    save(result);return result


if __name__=='__main__':
    ART.mkdir(parents=True,exist_ok=True)
    operation=sys.argv[1]
    if operation=='start': answer=start(sys.argv[2])
    elif operation.startswith('check-') and operation not in {'check-checkpoint'}: answer=check_phase(operation[6:])
    elif operation=='checkpoint': answer=checkpoint(sys.argv[2])
    elif operation=='check-checkpoint': answer=checkpoint(sys.argv[2],True)
    elif operation=='restart': answer=restart()
    elif operation=='finish': answer=finish()
    else: raise SystemExit('Unknown isolated verification operation')
    print(json.dumps(answer,ensure_ascii=True))
