"""DuckDB connection management and schema for pralph.

All structured data (stories, status log, run log, phase state, solutions index)
lives in a single DuckDB file at ~/.pralph/pralph.duckdb, keyed by project_id
(the absolute path of each project directory).

Markdown files (design docs, guardrails, review feedback, prompt overrides)
remain on disk under each project's .pralph/ directory.
"""
from __future__ import annotations

import threading
from pathlib import Path

import duckdb

_DB_DIR = Path.home() / ".pralph"
_DB_PATH = _DB_DIR / "pralph.duckdb"

_lock = threading.Lock()
_conn: duckdb.DuckDBPyConnection | None = None


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a module-level DuckDB connection (created on first call)."""
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        _conn = duckdb.connect(str(_DB_PATH))
        _ensure_schema(_conn)
        return _conn


def get_readonly_connection() -> duckdb.DuckDBPyConnection:
    """Return a read-only DuckDB connection that works even while another process writes.

    DuckDB doesn't allow concurrent connections (even read-only) when a write lock
    is held, so we snapshot the file to a temp copy and open that instead.
    The caller should close the connection when done.
    """
    import shutil
    import tempfile

    if not _DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {_DB_PATH}")
    # Copy to a temp file so we don't conflict with the writer
    tmp = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    tmp.close()
    shutil.copy2(str(_DB_PATH), tmp.name)
    # Also copy WAL file if it exists
    wal_path = Path(str(_DB_PATH) + ".wal")
    if wal_path.exists():
        shutil.copy2(str(wal_path), tmp.name + ".wal")
    return duckdb.connect(tmp.name, read_only=True)


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS stories (
            project_id TEXT NOT NULL,
            id TEXT NOT NULL,
            title TEXT DEFAULT '',
            content TEXT DEFAULT '',
            acceptance_criteria TEXT DEFAULT '[]',
            priority INTEGER DEFAULT 3,
            category TEXT DEFAULT '',
            complexity TEXT DEFAULT 'medium',
            dependencies TEXT DEFAULT '[]',
            source TEXT DEFAULT 'extract',
            status TEXT DEFAULT 'pending',
            metadata TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (project_id, id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS status_log (
            project_id TEXT NOT NULL,
            story_id TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT DEFAULT '',
            extra TEXT DEFAULT '{}',
            logged_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            project_id TEXT NOT NULL,
            iteration INTEGER,
            phase TEXT,
            mode TEXT,
            success BOOLEAN,
            stories_generated INTEGER DEFAULT 0,
            impl_status TEXT DEFAULT '',
            error TEXT DEFAULT '',
            duration DOUBLE DEFAULT 0.0,
            cost_usd DOUBLE DEFAULT 0.0,
            story_id TEXT DEFAULT '',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_input_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            logged_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS phase_state (
            project_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            current_iteration INTEGER DEFAULT 0,
            consecutive_empty INTEGER DEFAULT 0,
            consecutive_errors INTEGER DEFAULT 0,
            completed BOOLEAN DEFAULT FALSE,
            completion_reason TEXT DEFAULT '',
            total_cost_usd DOUBLE DEFAULT 0.0,
            last_error TEXT DEFAULT '',
            last_summary TEXT DEFAULT '',
            active_session_id TEXT DEFAULT '',
            active_story_id TEXT DEFAULT '',
            active_session_started TEXT DEFAULT '',
            PRIMARY KEY (project_id, phase)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS phase1_analysis (
            project_id TEXT PRIMARY KEY,
            data TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS solutions_index (
            project_id TEXT NOT NULL,
            title TEXT DEFAULT '',
            category TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            error_signature TEXT DEFAULT '',
            story_id TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT current_timestamp
        )
    """)


def register_project(conn: duckdb.DuckDBPyConnection, project_id: str, name: str) -> None:
    """Register a project if it doesn't already exist."""
    conn.execute(
        "INSERT OR IGNORE INTO projects (project_id, name) VALUES (?, ?)",
        [project_id, name],
    )


def execute_query(sql: str, params: list | None = None) -> tuple[list[str], list[tuple]]:
    """Execute arbitrary SQL and return (column_names, rows).

    Uses a read-only snapshot so queries work while another pralph process
    holds the write lock.
    """
    conn = get_readonly_connection()
    try:
        if params:
            result = conn.execute(sql, params)
        else:
            result = conn.execute(sql)
        columns = [desc[0] for desc in result.description] if result.description else []
        rows = result.fetchall()
        return columns, rows
    finally:
        conn.close()
