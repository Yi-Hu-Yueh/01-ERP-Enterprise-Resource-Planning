"""Explicit test-only authorization. Never imported by production application code."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
import tempfile
from sqlalchemy import event
from backend.app.db import ROOT
from backend.app.main import create_app


def new_qa_application(parent):
    parent=Path(parent).resolve()
    allowed=[(ROOT/"artifacts").resolve(),Path(tempfile.gettempdir()).resolve()]
    if not any(parent.is_relative_to(base) for base in allowed):
        raise ValueError("QA runtime requires artifacts or an OS temporary directory")
    parent.mkdir(parents=True,exist_ok=True)
    directory=Path(tempfile.mkdtemp(prefix="issue-qa-",dir=parent)).resolve()
    path=directory/"isolated.sqlite"
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.close(fd)
    original=path.stat()
    identity=(original.st_dev,original.st_ino)
    owner=(ROOT/"data/erp.sqlite").resolve()
    app=create_app(path)
    original_lifespan=app.router.lifespan_context
    consumed=False
    live=False

    def intact():
        try:
            info=path.stat()
            return (path.resolve()==path and (info.st_dev,info.st_ino)==identity
                    and info.st_nlink==1 and (not owner.exists() or not path.samefile(owner)))
        except OSError:
            return False

    @asynccontextmanager
    async def lifespan(application):
        nonlocal consumed,live
        if consumed or not intact():
            raise RuntimeError("QA capability is single-runtime and bound to its newly created physical database")
        consumed=True
        async with original_lifespan(application):
            database=application.state.db
            if database.path!=path or not intact():
                raise RuntimeError("QA database identity mismatch; authorization denied")
            engine=database.engine
            def configure(driver,_):
                actual=Path(next(row[2] for row in driver.execute("PRAGMA database_list") if row[1]=="main")).resolve()
                def authorized():
                    return int(live and actual==path and intact() and application.state.db is database
                               and database.path==path and database.engine is engine)
                driver.create_function("erp_issue_test_authorized",0,authorized)
            event.listen(engine,"connect",configure)
            engine.dispose()
            live=True
            try:
                yield
            finally:
                live=False
                event.remove(engine,"connect",configure)
                engine.dispose()
    app.router.lifespan_context=lifespan
    return app,path
