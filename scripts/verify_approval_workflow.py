"""Lifecycle checks for the one synthetic desktop approval workflow.

Only the generated native QA installation receives approval mutations. Reuse the
export verifier's Windows EXE lifecycle, whose physical-file and shutdown
assumptions also apply here; none of its Owner-copy business fixtures are used.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts/electronic-approval-2026-11-21'
STATE = ART / 'native-state.json'
spec = importlib.util.spec_from_file_location('approval_native_lifecycle', ROOT / 'scripts/verify_standalone_export.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
native.ART, native.STATE = ART, STATE


def save(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def owner_preserved():
    baseline = json.loads((ART / 'owner-before.json').read_text(encoding='utf-8'))
    current = native.database_view(baseline['source'])
    for table, rows in baseline['rows'].items():
        if table != 'alembic_version':
            assert current['rows'][table] == rows, 'Owner rows changed: ' + table
        assert current['columns'][table] == baseline['columns'][table], 'Owner columns changed: ' + table
    existing = {tuple(row) for row in baseline['schema']}
    actual = {tuple(row) for row in current['schema']}
    assert existing.issubset(actual), 'Existing Owner schema changed'
    assert current['integrity'] == 'ok' and current['foreign_key_violations'] == []
    return dict(preexisting_tables=len(baseline['rows']),
                preexisting_rows=sum(len(rows) for name, rows in baseline['rows'].items() if name != 'alembic_version'),
                existing_rows_schema_preserved=True, integrity='ok', foreign_key_violations=0)


def isolated_business_preserved(state):
    metadata = json.loads((ART / 'native-ui-final.json').read_text(encoding='utf-8'))
    assert metadata['synthetic_only'] is True
    seed = native.database_view(state['seed'])
    active = native.database_view(state['database'])
    for table in metadata['business_tables']:
        assert active['rows'][table] == seed['rows'][table], 'Synthetic source/ledger changed: ' + table
        assert active['columns'][table] == seed['columns'][table]
    assert active['integrity'] == 'ok' and active['foreign_key_violations'] == []
    assert hashlib.sha256(Path(state['seed']).read_bytes()).hexdigest() == state['seed_sha256']
    state['synthetic_source_and_ledger_preserved'] = True
    state['owner_preservation'] = owner_preserved()


def load():
    state = json.loads(STATE.read_text(encoding='utf-8'))
    installation = Path(state['installation']).resolve()
    assert installation.name == 'TIGER-ERP' and installation.parent.name.startswith('TIGER-ERP-Approval-QA-')
    assert not installation.is_relative_to(ROOT)
    assert Path(state['database']).resolve() == installation / 'data/erp.sqlite3'
    assert not Path(state['database']).samefile(ROOT / 'data/erp.sqlite')
    return state


def start(archive_path):
    started = time.monotonic()
    owner_preserved()
    archive = Path(archive_path).resolve(strict=True)
    assert archive.is_relative_to(ART), 'Only the task-specific synthetic QA ZIP is allowed'
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        for name in zipped.namelist():
            parts = Path(name)
            assert name.startswith('TIGER-ERP/') and not parts.is_absolute() and '..' not in parts.parts
        extraction = Path(tempfile.mkdtemp(prefix='TIGER-ERP-Approval-QA-'))
        zipped.extractall(extraction)
    installation = extraction / 'TIGER-ERP'
    state = dict(synthetic_only=True, zip=str(archive), installation=str(installation),
                 exe=str(installation / 'TIGER-ERP.exe'), seed=str(installation / 'seed/erp.sqlite3'),
                 database=str(installation / 'data/erp.sqlite3'), zip_integrity=True)
    baseline = native.database_view(ART / 'native-ui-final.sqlite')
    assert native.database_view(state['seed']) == baseline, 'QA ZIP must contain the complete synthetic seed'
    state['seed_sha256'] = hashlib.sha256(Path(state['seed']).read_bytes()).hexdigest()
    assert not Path(state['database']).exists()
    save(state)
    native.launch(state)
    assert not Path(state['database']).samefile(state['seed'])
    assert native.database_view(state['database']) == baseline, 'Native startup changed the migrated synthetic seed'
    isolated_business_preserved(state)
    state.update(first_run_complete_equality=True, start_wall_seconds=time.monotonic() - started)
    save(state)
    return state


def restart():
    started = time.monotonic()
    state = load()
    before = native.database_view(state['database'])
    isolated_business_preserved(state)
    native.stop(state)
    native.launch(state)
    assert native.database_view(state['database']) == before, 'Restart changed approval rows, events or source data'
    isolated_business_preserved(state)
    state.update(restart_complete_database_equality=True, restart_wall_seconds=time.monotonic() - started)
    save(state)
    return state


def finish():
    state = load()
    isolated_business_preserved(state)
    native.stop(state)
    state['gracefully_stopped'] = True
    save(state)
    return state


if __name__ == '__main__':
    ART.mkdir(parents=True, exist_ok=True)
    if sys.argv[1] == 'start':
        result = start(sys.argv[2])
    elif sys.argv[1] == 'restart':
        result = restart()
    elif sys.argv[1] == 'finish':
        result = finish()
    else:
        raise SystemExit('Unknown verification stage')
    print(json.dumps(result, ensure_ascii=True))
