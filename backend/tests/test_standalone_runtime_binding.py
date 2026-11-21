"""Portable path/port adaptation must not widen development posting authority."""
import sys

import pytest

from backend.app.local_demo import LocalDemoRuntime, create_local_demo_app


def test_source_runtime_keeps_existing_path_and_distinct_port_restrictions(tmp_path):
    with pytest.raises(ValueError):
        LocalDemoRuntime(tmp_path / 'erp.sqlite3', 43521, 43521, '127.0.0.1')
    with pytest.raises(ValueError):
        create_local_demo_app(tmp_path / 'data/erp.sqlite3', backend_port=43521,
                              frontend_port=43521, standalone_directory=tmp_path)


def test_frozen_runtime_binds_every_existing_action_to_only_its_own_database(tmp_path, monkeypatch):
    executable = tmp_path / 'TIGER-ERP.exe'
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(executable))
    active = tmp_path / 'data/erp.sqlite3'
    app = create_local_demo_app(active, backend_port=43521, frontend_port=43521,
                                standalone_directory=tmp_path)
    runtimes = [value for value in app.state._state.values() if isinstance(value, LocalDemoRuntime)]
    # Issue plus independently attached return/completion/FG/shipment/stock guards.
    assert len(runtimes) == 9
    assert all(value.path == active.resolve() for value in runtimes)
    assert all(value.backend_port == value.frontend_port == 43521 for value in runtimes)
    with pytest.raises(ValueError):
        LocalDemoRuntime(tmp_path / 'elsewhere/erp.sqlite3', 43521, 43521, '127.0.0.1')
    with pytest.raises(ValueError):
        LocalDemoRuntime(active, 43521, 43521, '0.0.0.0')
