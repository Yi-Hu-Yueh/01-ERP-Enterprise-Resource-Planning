# Fixed, repository-owned Windows onedir build. No browser-supplied operations.
from pathlib import Path

project = Path(SPECPATH).resolve().parent
resources = [(str(project / 'frontend' / 'dist'), 'frontend/dist'),
             (str(project / 'backend' / 'alembic.ini'), 'backend')]
for source in sorted((project / 'backend' / 'migrations').rglob('*.py')):
    if '__pycache__' not in source.parts:
        resources.append((str(source), str(source.parent.relative_to(project))))
for source in sorted((project / 'backend').rglob('*.sql')):
    if 'tests' not in source.parts:
        resources.append((str(source), str(source.parent.relative_to(project))))

# Alembic migration files import earlier numbered revisions dynamically.
hidden = [str(source.relative_to(project).with_suffix('')).replace('\\', '.').replace('/', '.')
          for source in (project / 'backend' / 'migrations' / 'versions').glob('*.py')]
hidden += ['uvicorn.logging', 'uvicorn.loops.asyncio', 'uvicorn.lifespan.on',
           'uvicorn.protocols.http.h11_impl', 'sqlalchemy.dialects.sqlite.pysqlite']

a = Analysis([str(project / 'backend' / 'standalone.py')], pathex=[str(project)],
             binaries=[], datas=resources, hiddenimports=hidden, hookspath=[],
             hooksconfig={}, runtime_hooks=[],
             excludes=['pytest', '_pytest', 'IPython', 'tkinter', 'unittest', 'httpx'],
             noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='TIGER-ERP',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False,
          contents_directory='_internal')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='TIGER-ERP')
