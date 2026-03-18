from __future__ import annotations

import json
import threading
from pathlib import Path

from pralph.file_state import FileStateMixin


class ProjectNotInitializedError(Exception):
    """Raised when a command is run in a directory without a project.json."""
    pass


class _BaseStateManager(FileStateMixin):
    """Base class with shared init logic — no storage mixin yet."""

    def __init__(self, project_dir: str, *, project_name: str | None = None, readonly: bool = False) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.state_dir = self.project_dir / ".pralph"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._readonly = readonly

        # Resolve project_id from project.json or create it
        self.project_id = self._resolve_project_id(project_name)

        # Central data directory: ~/.pralph/<project-id>/
        self.data_dir = Path.home() / ".pralph" / self.project_id
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _project_config_path(self) -> Path:
        return self.state_dir / "project.json"

    @property
    def storage_backend(self) -> str:
        """Return the storage backend for this project."""
        if self._project_config_path.exists():
            try:
                data = json.loads(self._project_config_path.read_text())
                if "storage" in data:
                    return data["storage"]
                # Existing project without storage key — was using DuckDB
                return "duckdb"
            except (json.JSONDecodeError, OSError):
                pass
        return "jsonl"

    def _resolve_project_id(self, project_name: str | None) -> str:
        """Resolve project_id: read from project.json, or create from project_name."""
        if self._project_config_path.exists():
            try:
                data = json.loads(self._project_config_path.read_text())
                stored_id = data.get("project_id", "")
                if stored_id:
                    return stored_id
            except (json.JSONDecodeError, OSError):
                pass

        if project_name:
            self._save_project_config(project_name)
            return project_name

        # Legacy project: has JSONL files but no project.json — auto-assign basename
        if self._has_legacy_data():
            legacy_name = self.project_dir.name
            self._save_project_config(legacy_name)
            return legacy_name

        # No project.json and no name provided — not initialized yet
        raise ProjectNotInitializedError(
            f"Project not initialized. Run 'pralph plan --name <project-name>' first.\n"
            f"  directory: {self.project_dir}"
        )

    def _migrate_data_dir(self) -> None:
        """Move data files from .pralph/ to ~/.pralph/<project-id>/ if needed."""
        import shutil

        moves = [
            ("stories.jsonl", "stories.jsonl"),
            ("status.jsonl", "status.jsonl"),
            ("run-log.jsonl", "run-log.jsonl"),
            ("phase1-analysis.json", "phase1-analysis.json"),
        ]
        for src_name, dst_name in moves:
            src = self.state_dir / src_name
            dst = self.data_dir / dst_name
            if src.exists() and not dst.exists():
                shutil.move(str(src), str(dst))

        # Migrate phases/ directory
        src_phases = self.state_dir / "phases"
        dst_phases = self.data_dir / "phases"
        if src_phases.is_dir() and not dst_phases.exists():
            shutil.move(str(src_phases), str(dst_phases))

        # Migrate solutions/ directory
        src_solutions = self.state_dir / "solutions"
        dst_solutions = self.data_dir / "solutions"
        if src_solutions.is_dir() and not dst_solutions.exists():
            shutil.move(str(src_solutions), str(dst_solutions))

    def _has_legacy_data(self) -> bool:
        """Check if this project has old-style JSONL files (pre-DuckDB)."""
        return (
            (self.state_dir / "stories.jsonl").exists()
            or (self.state_dir / "phase-state.json").exists()
            or self.design_doc_path.exists()
        )

    def _save_project_config(self, project_id: str, storage: str | None = None) -> None:
        data: dict = {"project_id": project_id}
        # Preserve existing config fields
        if self._project_config_path.exists():
            try:
                existing = json.loads(self._project_config_path.read_text())
                if isinstance(existing, dict):
                    data = existing
                    data["project_id"] = project_id
            except (json.JSONDecodeError, OSError):
                pass
        if storage is not None:
            data["storage"] = storage
        elif "storage" not in data:
            data["storage"] = "jsonl"
        self._project_config_path.write_text(
            json.dumps(data, indent=2) + "\n"
        )

    def refresh_readonly(self) -> None:
        """Override in subclasses if needed."""
        pass


# Lazy imports to avoid circular dependencies and allow duckdb to be optional.

class _DuckDbStateManager(_BaseStateManager):
    """StateManager backed by DuckDB. Mixin applied at class definition time below."""
    pass


class _JsonlStateManager(_BaseStateManager):
    """StateManager backed by JSONL files. Mixin applied at class definition time below."""
    pass


def _build_duckdb_class():
    """Build the DuckDB StateManager class with mixin and init logic."""
    import duckdb
    from pralph import db
    from pralph.db_state import DbStateMixin
    from pralph.migrate import migrate_project, needs_migration

    class DuckDbStateManager(_BaseStateManager, DbStateMixin):

        def __init__(self, project_dir: str, *, project_name: str | None = None, readonly: bool = False) -> None:
            super().__init__(project_dir, project_name=project_name, readonly=readonly)
            self.__conn: duckdb.DuckDBPyConnection | None = None

            if readonly:
                pass
            else:
                with db.connection() as conn:
                    db.register_project(conn, self.project_id, self.project_dir.name)
                    if needs_migration(self.state_dir, self.project_id, conn):
                        migrate_project(self.state_dir, self.project_id, conn)
                # Move solutions/ to data_dir after DuckDB migration
                self._migrate_data_dir()

        @property
        def _conn(self):
            if self.__conn is not None:
                return self.__conn
            if self._readonly:
                self.__conn = db.get_readonly_connection()
                return self.__conn
            raise RuntimeError("No held connection — wrap operation in _hold_conn()")

        def _hold_conn(self):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                if self.__conn is not None:
                    yield
                    return
                self.__conn = db.get_connection()
                try:
                    yield
                finally:
                    self.__conn.close()
                    self.__conn = None

            return _cm()

        def refresh_readonly(self) -> None:
            if not self._readonly:
                return
            if self.__conn is not None:
                self.__conn.close()
            self.__conn = db.get_readonly_connection()

        def _transient_write(self, sql: str, params: list) -> None:
            import time

            last_err: Exception | None = None
            for attempt in range(5):
                try:
                    with db.connection() as conn:
                        conn.execute(sql, params)
                    return
                except duckdb.IOException as e:
                    last_err = e
                    time.sleep(0.5)
            raise last_err  # type: ignore[misc]

    return DuckDbStateManager


def _build_jsonl_class():
    """Build the JSONL StateManager class with mixin."""
    from pralph.jsonl_state import JsonlStateMixin

    class JsonlStateManager(_BaseStateManager, JsonlStateMixin):
        def __init__(self, project_dir: str, *, project_name: str | None = None, readonly: bool = False) -> None:
            super().__init__(project_dir, project_name=project_name, readonly=readonly)
            if not readonly:
                self._migrate_data_dir()

    return JsonlStateManager


# Cache built classes
_duckdb_cls = None
_jsonl_cls = None


def StateManager(project_dir: str, *, project_name: str | None = None, readonly: bool = False) -> _BaseStateManager:
    """Factory that returns the appropriate StateManager based on project.json storage setting."""
    global _duckdb_cls, _jsonl_cls

    config_path = Path(project_dir).resolve() / ".pralph" / "project.json"
    storage = "jsonl"  # default for new projects
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
            if "storage" in data:
                storage = data["storage"]
            else:
                # Existing project without storage key — was using DuckDB before this change
                storage = "duckdb"
        except (json.JSONDecodeError, OSError):
            pass

    if storage == "duckdb":
        if _duckdb_cls is None:
            _duckdb_cls = _build_duckdb_class()
        return _duckdb_cls(project_dir, project_name=project_name, readonly=readonly)

    if _jsonl_cls is None:
        _jsonl_cls = _build_jsonl_class()
    return _jsonl_cls(project_dir, project_name=project_name, readonly=readonly)
