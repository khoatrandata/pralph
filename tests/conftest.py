from __future__ import annotations

import json

import pytest

from pralph.state import StateManager


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Keep ~/.pralph (central state) and ~/.claude out of the real home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def jsonl_state(tmp_path):
    """A JSONL-backed StateManager in a temp project directory."""
    project = tmp_path / "project"
    project.mkdir()
    return StateManager(str(project), project_name="test")


@pytest.fixture
def duckdb_state(tmp_path, monkeypatch):
    """A DuckDB-backed StateManager whose database lives in the temp directory."""
    import pralph.db as db

    db_dir = tmp_path / "db"
    monkeypatch.setattr(db, "_DB_DIR", db_dir)
    monkeypatch.setattr(db, "_DB_PATH", db_dir / "pralph.duckdb")
    monkeypatch.setattr(db, "_schema_initialized", False)

    project = tmp_path / "project"
    (project / ".pralph").mkdir(parents=True)
    (project / ".pralph" / "project.json").write_text(json.dumps({"storage": "duckdb"}))
    return StateManager(str(project), project_name="test")
