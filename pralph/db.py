"""DuckDB connection management and schema for pralph.

All structured data (stories, status log, run log, phase state, solutions index)
lives in a single DuckDB file at ~/.pralph/pralph.duckdb, keyed by project_id
(the absolute path of each project directory).

Markdown files (design docs, guardrails, review feedback, prompt overrides)
remain on disk under each project's .pralph/ directory.

Connection strategy: short-lived connections opened per operation and closed
immediately after. This avoids holding the DuckDB write lock while Claude
runs (which can take minutes), allowing concurrent read access from other
processes (e.g. `pralph query --report`).
"""
from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

_DB_DIR = Path.home() / ".pralph"
_DB_PATH = _DB_DIR / "pralph.duckdb"

_schema_initialized = False
_schema_lock = threading.Lock()


def _ensure_db_dir() -> None:
    _DB_DIR.mkdir(parents=True, exist_ok=True)


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
            session_id TEXT DEFAULT '',
            logged_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # Migration: add session_id column if missing (existing DBs)
    try:
        conn.execute("SELECT session_id FROM run_log LIMIT 0")
    except Exception:
        conn.execute("ALTER TABLE run_log ADD COLUMN session_id TEXT DEFAULT ''")


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


@retry(
    retry=retry_if_exception_type(duckdb.IOException),
    stop=stop_after_attempt(10),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    before_sleep=lambda retry_state: print(
        f"  [db] lock contention, retrying in {retry_state.next_action.sleep:.1f}s "
        f"(attempt {retry_state.attempt_number}/10)",
        file=sys.stderr,
    ),
    reraise=True,
)
def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a new short-lived DuckDB connection.

    Callers should close the connection when done, ideally via the
    `connection()` context manager. Schema is ensured on first call.
    Retries on IOException (lock contention) with exponential backoff.
    """
    global _schema_initialized
    _ensure_db_dir()
    if not _schema_initialized:
        with _schema_lock:
            if not _schema_initialized:
                conn = duckdb.connect(str(_DB_PATH))
                _ensure_schema(conn)
                _schema_initialized = True
                return conn
    return duckdb.connect(str(_DB_PATH))


@contextmanager
def connection():
    """Context manager that yields a DuckDB connection and closes it on exit.

    Usage:
        with db.connection() as conn:
            conn.execute("INSERT ...")
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def get_readonly_connection() -> duckdb.DuckDBPyConnection:
    """Return a read-only DuckDB connection that works even while another process writes.

    DuckDB doesn't allow concurrent connections (even read-only) when a write lock
    is held, so we snapshot the file to a temp copy and open that instead.
    The caller should close the connection when done — the temp file is cleaned up
    automatically when the connection is closed.
    """
    import shutil
    import tempfile

    if not _DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {_DB_PATH}")

    # Copy DB + WAL with consistency check: retry if DB changed during copy
    tmp = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    tmp.close()
    tmp_path = tmp.name
    tmp_wal = tmp_path + ".wal"

    wal_path = Path(str(_DB_PATH) + ".wal")
    for _attempt in range(3):
        mtime_before = _DB_PATH.stat().st_mtime_ns
        shutil.copy2(str(_DB_PATH), tmp_path)
        if wal_path.exists():
            shutil.copy2(str(wal_path), tmp_wal)
        mtime_after = _DB_PATH.stat().st_mtime_ns
        if mtime_before == mtime_after:
            break  # consistent snapshot

    conn = duckdb.connect(tmp_path, read_only=True)

    # Clean up temp files when connection is closed
    _original_close = conn.close

    def _cleanup_close():
        _original_close()
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            Path(tmp_wal).unlink(missing_ok=True)
        except OSError:
            pass

    conn.close = _cleanup_close  # type: ignore[assignment]
    return conn


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
