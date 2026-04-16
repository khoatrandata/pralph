"""One-time migration of .pralph/ JSONL files into DuckDB.

Called automatically by StateManager on init when JSONL files exist
but no rows for the project are in DuckDB yet.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb


def needs_migration(state_dir: Path, project_id: str, conn: duckdb.DuckDBPyConnection) -> bool:
    """Check if this project has JSONL files but no DuckDB data yet."""
    stories_jsonl = state_dir / "stories.jsonl"
    if not stories_jsonl.exists():
        return False
    row = conn.execute(
        "SELECT COUNT(*) FROM stories WHERE project_id = ?", [project_id]
    ).fetchone()
    return row is not None and row[0] == 0


def migrate_project(state_dir: Path, project_id: str, conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate all JSONL data for a project into DuckDB."""
    _migrate_stories(state_dir, project_id, conn)
    _migrate_status_log(state_dir, project_id, conn)
    _migrate_run_log(state_dir, project_id, conn)
    _migrate_phase_state(state_dir, project_id, conn)
    _migrate_phase1_analysis(state_dir, project_id, conn)
    _migrate_solutions_index(state_dir, project_id, conn)


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, skipping blank/invalid lines."""
    entries: list[dict] = []
    if not path.exists():
        return entries
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                entries.append(obj)
        except json.JSONDecodeError:
            continue
    return entries


def _migrate_stories(state_dir: Path, project_id: str, conn: duckdb.DuckDBPyConnection) -> None:
    path = state_dir / "stories.jsonl"
    for d in _read_jsonl(path):
        if not d.get("id"):
            continue
        conn.execute(
            """INSERT OR IGNORE INTO stories
               (project_id, id, title, content, acceptance_criteria, priority,
                category, complexity, dependencies, source, status, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                project_id,
                d["id"],
                d.get("title", ""),
                d.get("content", ""),
                json.dumps(d.get("acceptance_criteria", [])),
                d.get("priority", 3),
                d.get("category", ""),
                d.get("complexity", "medium"),
                json.dumps(d.get("dependencies", [])),
                d.get("source", "extract"),
                d.get("status", "pending"),
                json.dumps(d.get("metadata", {})),
            ],
        )
    _backup(path)


def _migrate_status_log(state_dir: Path, project_id: str, conn: duckdb.DuckDBPyConnection) -> None:
    path = state_dir / "status.jsonl"
    for d in _read_jsonl(path):
        story_id = d.get("story_id", "")
        if not story_id:
            continue
        extra = {k: v for k, v in d.items() if k not in ("story_id", "status", "summary")}
        conn.execute(
            """INSERT INTO status_log (project_id, story_id, status, summary, extra)
               VALUES (?, ?, ?, ?, ?)""",
            [
                project_id,
                story_id,
                d.get("status", ""),
                d.get("summary", ""),
                json.dumps(extra),
            ],
        )
    _backup(path)


def _migrate_run_log(state_dir: Path, project_id: str, conn: duckdb.DuckDBPyConnection) -> None:
    path = state_dir / "run-log.jsonl"
    for d in _read_jsonl(path):
        conn.execute(
            """INSERT INTO run_log
               (project_id, iteration, phase, mode, success, stories_generated,
                impl_status, error, duration, cost_usd, story_id,
                input_tokens, output_tokens, cache_read_input_tokens,
                cache_creation_input_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                project_id,
                d.get("iteration", 0),
                d.get("phase", ""),
                d.get("mode", ""),
                d.get("success", False),
                d.get("stories_generated", 0),
                d.get("impl_status", ""),
                d.get("error", ""),
                d.get("duration", 0.0),
                d.get("cost_usd", 0.0),
                d.get("story_id", ""),
                d.get("input_tokens", 0),
                d.get("output_tokens", 0),
                d.get("cache_read_input_tokens", 0),
                d.get("cache_creation_input_tokens", 0),
            ],
        )
    _backup(path)


def _migrate_phase_state(state_dir: Path, project_id: str, conn: duckdb.DuckDBPyConnection) -> None:
    def _insert_phase(d: dict, path: Path | None = None) -> None:
        if not isinstance(d, dict) or "phase" not in d:
            return
        conn.execute(
            """INSERT OR REPLACE INTO phase_state
               (project_id, phase, current_iteration, consecutive_empty,
                consecutive_errors, completed, completion_reason, total_cost_usd,
                last_error, last_summary, active_session_id, active_story_id,
                active_session_started)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                project_id,
                d["phase"],
                d.get("current_iteration", 0),
                d.get("consecutive_empty", 0),
                d.get("consecutive_errors", 0),
                d.get("completed", False),
                d.get("completion_reason", ""),
                d.get("total_cost_usd", 0.0),
                d.get("last_error", ""),
                d.get("last_summary", ""),
                d.get("active_session_id", ""),
                d.get("active_story_id", ""),
                d.get("active_session_started", ""),
            ],
        )
        if path is not None:
            _backup(path)

    # Migrate top-level phase-state.json
    path = state_dir / "phase-state.json"
    if path.exists():
        try:
            d = json.loads(path.read_text())
            _insert_phase(d, path)
        except (json.JSONDecodeError, OSError):
            pass

    # Migrate per-phase files under phases/ (e.g. phases/implement.json)
    phases_dir = state_dir / "phases"
    if phases_dir.is_dir():
        for phase_file in phases_dir.glob("*.json"):
            try:
                d = json.loads(phase_file.read_text())
                if isinstance(d, dict) and "phase" not in d:
                    # Infer phase name from filename
                    d["phase"] = phase_file.stem
                _insert_phase(d, phase_file)
            except (json.JSONDecodeError, OSError):
                continue


def _migrate_phase1_analysis(state_dir: Path, project_id: str, conn: duckdb.DuckDBPyConnection) -> None:
    path = state_dir / "phase1-analysis.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    conn.execute(
        "INSERT OR REPLACE INTO phase1_analysis (project_id, data) VALUES (?, ?)",
        [project_id, json.dumps(data)],
    )
    _backup(path)


def _migrate_solutions_index(state_dir: Path, project_id: str, conn: duckdb.DuckDBPyConnection) -> None:
    path = state_dir / "solutions" / "index.jsonl"
    for d in _read_jsonl(path):
        conn.execute(
            """INSERT INTO solutions_index
               (project_id, title, category, filename, tags, error_signature, story_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                project_id,
                d.get("title", ""),
                d.get("category", ""),
                d.get("filename", ""),
                json.dumps(d.get("tags", [])),
                d.get("error_signature", ""),
                d.get("story_id", ""),
            ],
        )
    _backup(path)


def _backup(path: Path) -> None:
    """Rename a migrated file to .bak."""
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        path.rename(backup)
