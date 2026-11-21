"""Reuse immutable synthetic seed data, never an application or posting grant."""
from contextlib import closing
from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import tempfile

import pytest

from backend.app.db import ROOT


@pytest.fixture(scope="session")
def material_return_sources():
    # Only this fixture is registered by conftest. Direct seeder and callback
    # tests still execute the original fresh-data path without this template.
    from backend.tests.material_return_qa_runtime import seed_material_return_qa_database

    parent = ROOT / "artifacts" / "material-return-fixture-templates"
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="session-", dir=parent)) / "baseline.sqlite"
    return path, seed_material_return_qa_database(path)


def clone_material_return_seed(sources, destination):
    """Give each case independent bytes, nested metadata and future runtime."""
    from backend.tests.material_return_qa_runtime import artifact_path

    source, metadata = sources
    source = artifact_path(source)
    destination = artifact_path(destination)
    if not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError("Fixture template must be an existing single-link synthetic database")
    if destination.exists():
        raise ValueError("Fixture cloning refuses an existing destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("Fixture cloning refuses an existing destination") from exc
    os.close(descriptor)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as target:
            original.backup(target)
    if destination.stat().st_nlink != 1 or destination.samefile(source):
        raise ValueError("Fixture clone must have an independent physical file")
    copied = deepcopy(metadata)
    copied["database"] = str(destination)
    return copied
