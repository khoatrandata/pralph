"""DB state — DuckDB CRUD operations for stories, phase state, run log, solutions."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pralph.models import IterationResult, PhaseState, Story, StoryStatus


def _row_to_story(row: tuple, columns: list[str]) -> Story:
    """Convert a DuckDB row + column names into a Story object."""
    d = dict(zip(columns, row))
    return Story(
        id=d["id"],
        title=d.get("title", ""),
        content=d.get("content", ""),
        acceptance_criteria=json.loads(d.get("acceptance_criteria", "[]")),
        priority=d.get("priority", 3),
        category=d.get("category", ""),
        complexity=d.get("complexity", "medium"),
        dependencies=json.loads(d.get("dependencies", "[]")),
        source=d.get("source", "extract"),
        status=StoryStatus(d.get("status", "pending")),
        metadata=json.loads(d.get("metadata", "{}")),
    )


class DbStateMixin:
    """Mixin providing DuckDB CRUD operations.

    Expects the host class to provide:
    - self.project_id: str
    - self._lock: threading.RLock
    - self._hold_conn(): context manager
    - self._conn: duckdb.DuckDBPyConnection (inside _hold_conn)
    - self._readonly: bool
    - self._transient_write(sql, params): for readonly writes
    - self.solutions_dir: Path (from FileStateMixin)
    """

    # -- phase1 analysis (DuckDB) --

    def has_phase1_analysis(self) -> bool:
        with self._hold_conn():  # type: ignore[attr-defined]
            row = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM phase1_analysis WHERE project_id = ?",
                [self.project_id],  # type: ignore[attr-defined]
            ).fetchone()
            return row is not None and row[0] > 0

    def load_phase1_analysis(self) -> dict | None:
        with self._hold_conn():  # type: ignore[attr-defined]
            row = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT data FROM phase1_analysis WHERE project_id = ?",
                [self.project_id],  # type: ignore[attr-defined]
            ).fetchone()
            if row is None:
                return None
            return json.loads(row[0])

    def save_phase1_analysis(self, data: dict) -> None:
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            self._conn.execute(  # type: ignore[attr-defined]
                "INSERT OR REPLACE INTO phase1_analysis (project_id, data) VALUES (?, ?)",
                [self.project_id, json.dumps(data)],  # type: ignore[attr-defined]
            )

    def delete_phase1_analysis(self) -> None:
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            self._conn.execute(  # type: ignore[attr-defined]
                "DELETE FROM phase1_analysis WHERE project_id = ?",
                [self.project_id],  # type: ignore[attr-defined]
            )

    # -- stories (DuckDB) --

    def _story_columns(self) -> list[str]:
        """Column names for the stories table, in SELECT * order."""
        return [
            "project_id", "id", "title", "content", "acceptance_criteria",
            "priority", "category", "complexity", "dependencies", "source",
            "status", "metadata", "created_at", "updated_at",
        ]

    def _query_stories(self, where: str = "", params: list | None = None) -> list[Story]:
        """Run a SELECT on stories and return Story objects."""
        sql = f"SELECT * FROM stories WHERE project_id = ?"
        p: list = [self.project_id]  # type: ignore[attr-defined]
        if where:
            sql += f" AND ({where})"
            if params:
                p.extend(params)
        with self._hold_conn():  # type: ignore[attr-defined]
            result = self._conn.execute(sql, p)  # type: ignore[attr-defined]
            cols = [desc[0] for desc in result.description]
            return [_row_to_story(row, cols) for row in result.fetchall()]

    def load_stories(self) -> list[Story]:
        return self._query_stories()

    def has_stories(self) -> bool:
        with self._hold_conn():  # type: ignore[attr-defined]
            row = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM stories WHERE project_id = ?",
                [self.project_id],  # type: ignore[attr-defined]
            ).fetchone()
            return row is not None and row[0] > 0

    def append_stories(self, stories: list[Story]) -> None:
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            for s in stories:
                self._conn.execute(  # type: ignore[attr-defined]
                    """INSERT OR IGNORE INTO stories
                       (project_id, id, title, content, acceptance_criteria, priority,
                        category, complexity, dependencies, source, status, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        self.project_id,  # type: ignore[attr-defined]
                        s.id,
                        s.title,
                        s.content,
                        json.dumps(s.acceptance_criteria),
                        s.priority,
                        s.category,
                        s.complexity,
                        json.dumps(s.dependencies),
                        s.source,
                        s.status.value,
                        json.dumps(s.metadata),
                    ],
                )

    def get_pending_stories(self) -> list[Story]:
        return self._query_stories("status = ?", ["pending"])

    def get_actionable_stories(self) -> list[Story]:
        """Return stories that are pending or need rework (rework first)."""
        stories = self._query_stories("status IN (?, ?)", ["rework", "pending"])
        rework = [s for s in stories if s.status == StoryStatus.rework]
        pending = [s for s in stories if s.status == StoryStatus.pending]
        return rework + pending

    def reset_error_stories(self) -> list[Story]:
        """Find stories with error status and reset them to pending."""
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            error_stories = self._query_stories("status = ?", ["error"])
            if not error_stories:
                return []

            for s in error_stories:
                s.status = StoryStatus.pending
                s.metadata.pop("error_reason", None)
                s.metadata.pop("error_output", None)
                s.metadata.pop("error_at", None)
                self._conn.execute(  # type: ignore[attr-defined]
                    """UPDATE stories SET status = ?, metadata = ?, updated_at = current_timestamp
                       WHERE project_id = ? AND id = ?""",
                    ["pending", json.dumps(s.metadata), self.project_id, s.id],  # type: ignore[attr-defined]
                )
                self._conn.execute(  # type: ignore[attr-defined]
                    """INSERT INTO status_log (project_id, story_id, status, summary, extra)
                       VALUES (?, ?, ?, ?, ?)""",
                    [self.project_id, s.id, "pending", "Reset from error status", "{}"],  # type: ignore[attr-defined]
                )

            return error_stories

    def recover_orphaned_stories(self) -> list[Story]:
        """Find in_progress stories (orphans from crashes) and reset to pending."""
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            in_progress = self._query_stories("status = ?", ["in_progress"])
            if not in_progress:
                return []

            now = datetime.now().isoformat()
            for s in in_progress:
                s.status = StoryStatus.pending
                s.metadata["previous_attempt"] = {
                    "was_in_progress": True,
                    "recovered_at": now,
                }
                self._conn.execute(  # type: ignore[attr-defined]
                    """UPDATE stories SET status = ?, metadata = ?, updated_at = current_timestamp
                       WHERE project_id = ? AND id = ?""",
                    ["pending", json.dumps(s.metadata), self.project_id, s.id],  # type: ignore[attr-defined]
                )
                self._conn.execute(  # type: ignore[attr-defined]
                    """INSERT INTO status_log (project_id, story_id, status, summary, extra)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        self.project_id, s.id, "pending",  # type: ignore[attr-defined]
                        "Recovered from crash (was in_progress)",
                        json.dumps({"recovery": True}),
                    ],
                )
            return in_progress

    def get_story_ids(self) -> set[str]:
        with self._hold_conn():  # type: ignore[attr-defined]
            rows = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT id FROM stories WHERE project_id = ?",
                [self.project_id],  # type: ignore[attr-defined]
            ).fetchall()
            return {row[0] for row in rows}

    def get_category_stats(self) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "next_id": 1})
        for story in self.load_stories():
            cat = story.category.upper()
            if not cat:
                continue
            stats[cat]["count"] += 1
            parts = story.id.rsplit("-", 1)
            if len(parts) == 2:
                try:
                    num = int(parts[1])
                    stats[cat]["next_id"] = max(stats[cat]["next_id"], num + 1)
                except ValueError:
                    pass
        return dict(stats)

    def format_existing_stories_context(self) -> str:
        stories = self.load_stories()
        if not stories:
            return "(none yet)"
        lines: list[str] = []
        for s in stories:
            deps = ", ".join(s.dependencies) if s.dependencies else "none"
            lines.append(f"- {s.id}: {s.title} [priority={s.priority}, status={s.status.value}, deps={deps}]")
        return "\n".join(lines)

    def format_category_stats(self) -> str:
        stats = self.get_category_stats()
        if not stats:
            return "(no categories yet — start IDs at CATEGORY-001)"
        lines: list[str] = []
        for cat, info in sorted(stats.items()):
            lines.append(f"- {cat}: count={info['count']}, next_id={info['next_id']:03d}")
        return "\n".join(lines)

    # -- story status (DuckDB) --

    def mark_story_status(
        self,
        story_id: str,
        status: StoryStatus,
        summary: str = "",
        extra: dict | None = None,
        error_reason: str = "",
        error_output: str = "",
    ) -> None:
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            log_extra = dict(extra or {})
            if error_reason:
                log_extra["error_reason"] = error_reason
            self._conn.execute(  # type: ignore[attr-defined]
                """INSERT INTO status_log (project_id, story_id, status, summary, extra)
                   VALUES (?, ?, ?, ?, ?)""",
                [self.project_id, story_id, status.value, summary, json.dumps(log_extra)],  # type: ignore[attr-defined]
            )

            # Build metadata update for error context
            metadata_update = None
            if status == StoryStatus.error:
                # Read current metadata, merge error fields
                row = self._conn.execute(  # type: ignore[attr-defined]
                    "SELECT metadata FROM stories WHERE project_id = ? AND id = ?",
                    [self.project_id, story_id],  # type: ignore[attr-defined]
                ).fetchone()
                meta = json.loads(row[0]) if row and row[0] else {}
                meta["error_reason"] = error_reason or summary
                if error_output:
                    meta["error_output"] = error_output[-2000:]
                meta["error_at"] = datetime.now().isoformat()
                metadata_update = json.dumps(meta)

            if metadata_update is not None:
                self._conn.execute(  # type: ignore[attr-defined]
                    """UPDATE stories SET status = ?, metadata = ?, updated_at = current_timestamp
                       WHERE project_id = ? AND id = ?""",
                    [status.value, metadata_update, self.project_id, story_id],  # type: ignore[attr-defined]
                )
            else:
                self._conn.execute(  # type: ignore[attr-defined]
                    """UPDATE stories SET status = ?, updated_at = current_timestamp
                       WHERE project_id = ? AND id = ?""",
                    [status.value, self.project_id, story_id],  # type: ignore[attr-defined]
                )

    def _rewrite_stories(self, stories: list[Story]) -> None:
        """Update all stories in bulk (used by viewer edit, etc.)."""
        if self._readonly:  # type: ignore[attr-defined]
            for s in stories:
                self._transient_write(  # type: ignore[attr-defined]
                    """UPDATE stories SET
                         title = ?, content = ?, acceptance_criteria = ?,
                         priority = ?, category = ?, complexity = ?,
                         dependencies = ?, source = ?, status = ?,
                         metadata = ?, updated_at = current_timestamp
                       WHERE project_id = ? AND id = ?""",
                    [
                        s.title, s.content, json.dumps(s.acceptance_criteria),
                        s.priority, s.category, s.complexity,
                        json.dumps(s.dependencies), s.source, s.status.value,
                        json.dumps(s.metadata), self.project_id, s.id,  # type: ignore[attr-defined]
                    ],
                )
            return
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            for s in stories:
                self._conn.execute(  # type: ignore[attr-defined]
                    """UPDATE stories SET
                         title = ?, content = ?, acceptance_criteria = ?,
                         priority = ?, category = ?, complexity = ?,
                         dependencies = ?, source = ?, status = ?,
                         metadata = ?, updated_at = current_timestamp
                       WHERE project_id = ? AND id = ?""",
                    [
                        s.title, s.content, json.dumps(s.acceptance_criteria),
                        s.priority, s.category, s.complexity,
                        json.dumps(s.dependencies), s.source, s.status.value,
                        json.dumps(s.metadata), self.project_id, s.id,  # type: ignore[attr-defined]
                    ],
                )

    def update_story(self, story: Story) -> None:
        """Update a single story's fields in DuckDB."""
        self._rewrite_stories([story])

    def delete_story(self, story_id: str) -> bool:
        """Delete a story by ID. Returns True if a row was deleted."""
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            row = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM stories WHERE project_id = ? AND id = ?",
                [self.project_id, story_id],  # type: ignore[attr-defined]
            ).fetchone()
            if not row or row[0] == 0:
                return False
            self._conn.execute(  # type: ignore[attr-defined]
                "DELETE FROM stories WHERE project_id = ? AND id = ?",
                [self.project_id, story_id],  # type: ignore[attr-defined]
            )
            self._conn.execute(  # type: ignore[attr-defined]
                """INSERT INTO status_log (project_id, story_id, status, summary, extra)
                   VALUES (?, ?, ?, ?, ?)""",
                [self.project_id, story_id, "deleted", "Deleted via edit command", "{}"],  # type: ignore[attr-defined]
            )
            return True

    def get_implemented_summary(self) -> str:
        with self._hold_conn():  # type: ignore[attr-defined]
            row = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM status_log WHERE project_id = ?",
                [self.project_id],  # type: ignore[attr-defined]
            ).fetchone()
            count = row[0] if row else 0
            if count == 0:
                return ""
            return f"## Previously Implemented Stories\n\n{count} status transitions tracked in DuckDB"

    def load_status_log(self) -> list[dict]:
        """Load all status log entries for this project."""
        with self._hold_conn():  # type: ignore[attr-defined]
            result = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT story_id, status, summary, extra, logged_at FROM status_log WHERE project_id = ? ORDER BY logged_at",
                [self.project_id],  # type: ignore[attr-defined]
            )
            cols = [desc[0] for desc in result.description]
            entries = []
            for row in result.fetchall():
                d = dict(zip(cols, row))
                extra = json.loads(d.pop("extra", "{}"))
                d.update(extra)
                entries.append(d)
            return entries

    # -- phase state (DuckDB) --

    def load_phase_state(self, phase: str) -> PhaseState:
        with self._hold_conn():  # type: ignore[attr-defined]
            result = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM phase_state WHERE project_id = ? AND phase = ?",
                [self.project_id, phase],  # type: ignore[attr-defined]
            )
            row = result.fetchone()
            if row is None:
                return PhaseState(phase=phase)
            cols = [desc[0] for desc in result.description]
        d = dict(zip(cols, row))
        return PhaseState(
            phase=d["phase"],
            current_iteration=d.get("current_iteration", 0),
            consecutive_empty=d.get("consecutive_empty", 0),
            consecutive_errors=d.get("consecutive_errors", 0),
            completed=bool(d.get("completed", False)),
            completion_reason=d.get("completion_reason", ""),
            total_cost_usd=d.get("total_cost_usd", 0.0),
            last_error=d.get("last_error", ""),
            last_summary=d.get("last_summary", ""),
            active_session_id=d.get("active_session_id", ""),
            active_story_id=d.get("active_story_id", ""),
            active_session_started=d.get("active_session_started", ""),
        )

    def save_phase_state(self, state: PhaseState) -> None:
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            self._conn.execute(  # type: ignore[attr-defined]
                """INSERT OR REPLACE INTO phase_state
                   (project_id, phase, current_iteration, consecutive_empty,
                    consecutive_errors, completed, completion_reason, total_cost_usd,
                    last_error, last_summary, active_session_id, active_story_id,
                    active_session_started)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    self.project_id,  # type: ignore[attr-defined]
                    state.phase,
                    state.current_iteration,
                    state.consecutive_empty,
                    state.consecutive_errors,
                    state.completed,
                    state.completion_reason,
                    state.total_cost_usd,
                    state.last_error,
                    state.last_summary,
                    state.active_session_id,
                    state.active_story_id,
                    state.active_session_started,
                ],
            )

    def load_all_phase_states(self) -> list[dict]:
        """Load all phase states for this project, returning list of dicts."""
        with self._hold_conn():  # type: ignore[attr-defined]
            result = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM phase_state WHERE project_id = ? ORDER BY phase",
                [self.project_id],  # type: ignore[attr-defined]
            )
            cols = [desc[0] for desc in result.description]
            return [dict(zip(cols, r)) for r in result.fetchall()]

    # -- run log (DuckDB) --

    def log_iteration(self, result: IterationResult) -> None:
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            self._conn.execute(  # type: ignore[attr-defined]
                """INSERT INTO run_log
                   (project_id, iteration, phase, mode, success, stories_generated,
                    impl_status, error, duration, cost_usd, story_id,
                    input_tokens, output_tokens, cache_read_input_tokens,
                    cache_creation_input_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    self.project_id,  # type: ignore[attr-defined]
                    result.iteration,
                    result.phase,
                    result.mode,
                    result.success,
                    result.stories_generated,
                    result.impl_status,
                    result.error,
                    result.duration,
                    result.cost_usd,
                    result.story_id,
                    result.input_tokens,
                    result.output_tokens,
                    result.cache_read_input_tokens,
                    result.cache_creation_input_tokens,
                ],
            )

    def load_run_log(self) -> list[dict]:
        """Load all run log entries for this project."""
        with self._hold_conn():  # type: ignore[attr-defined]
            result = self._conn.execute(  # type: ignore[attr-defined]
                """SELECT iteration, phase, mode, success, stories_generated,
                          impl_status, error, duration, cost_usd, story_id,
                          input_tokens, output_tokens, cache_read_input_tokens,
                          cache_creation_input_tokens, logged_at
                   FROM run_log WHERE project_id = ?
                   ORDER BY logged_at""",
                [self.project_id],  # type: ignore[attr-defined]
            )
            cols = [desc[0] for desc in result.description]
            return [dict(zip(cols, r)) for r in result.fetchall()]

    def get_story_tokens(self) -> dict[str, dict[str, int]]:
        """Aggregate tokens per story from run_log."""
        with self._hold_conn():  # type: ignore[attr-defined]
            rows = self._conn.execute(  # type: ignore[attr-defined]
                """SELECT story_id,
                          SUM(input_tokens) as input_tokens,
                          SUM(output_tokens) as output_tokens,
                          SUM(cache_read_input_tokens) as cache_read_input_tokens,
                          SUM(cache_creation_input_tokens) as cache_creation_input_tokens
                   FROM run_log
                   WHERE project_id = ? AND story_id != ''
                   GROUP BY story_id""",
                [self.project_id],  # type: ignore[attr-defined]
            ).fetchall()
            totals: dict[str, dict[str, int]] = {}
            for row in rows:
                totals[row[0]] = {
                    "input_tokens": int(row[1] or 0),
                    "output_tokens": int(row[2] or 0),
                    "cache_read_input_tokens": int(row[3] or 0),
                    "cache_creation_input_tokens": int(row[4] or 0),
                }
            return totals

    # -- solutions (compound learning) --

    def has_solutions(self) -> bool:
        with self._hold_conn():  # type: ignore[attr-defined]
            row = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM solutions_index WHERE project_id = ?",
                [self.project_id],  # type: ignore[attr-defined]
            ).fetchone()
            return row is not None and row[0] > 0

    def save_solution(
        self,
        category: str,
        filename: str,
        content: str,
        index_entry: dict,
    ) -> Path:
        """Write a solution markdown file and add to DuckDB index."""
        with self._lock, self._hold_conn():  # type: ignore[attr-defined]
            cat_dir = self.solutions_dir / category  # type: ignore[attr-defined]
            cat_dir.mkdir(parents=True, exist_ok=True)
            solution_path = cat_dir / filename
            solution_path.write_text(content)

            self._conn.execute(  # type: ignore[attr-defined]
                """INSERT INTO solutions_index
                   (project_id, title, category, filename, tags, error_signature, story_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    self.project_id,  # type: ignore[attr-defined]
                    index_entry.get("title", ""),
                    index_entry.get("category", ""),
                    index_entry.get("filename", ""),
                    json.dumps(index_entry.get("tags", [])),
                    index_entry.get("error_signature", ""),
                    index_entry.get("story_id", ""),
                ],
            )

            return solution_path

    def load_solutions_index(self) -> list[dict]:
        """Read all solution index entries for this project."""
        with self._hold_conn():  # type: ignore[attr-defined]
            result = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT title, category, filename, tags, error_signature, story_id FROM solutions_index WHERE project_id = ?",
                [self.project_id],  # type: ignore[attr-defined]
            )
            cols = [desc[0] for desc in result.description]
            entries = []
            for row in result.fetchall():
                d = dict(zip(cols, row))
                d["tags"] = json.loads(d.get("tags", "[]"))
                entries.append(d)
            return entries

    def search_solutions(self, query: str, max_results: int = 5) -> list[dict]:
        """Keyword search on title/tags/error_signature."""
        entries = self.load_solutions_index()
        if not entries:
            return []

        query_lower = query.lower()
        keywords = query_lower.split()

        scored: list[tuple[int, dict]] = []
        for entry in entries:
            score = 0
            title = (entry.get("title") or "").lower()
            tags = [t.lower() for t in (entry.get("tags") or [])]
            error_sig = (entry.get("error_signature") or "").lower()
            category = (entry.get("category") or "").lower()

            for kw in keywords:
                if kw in title:
                    score += 3
                if any(kw in tag for tag in tags):
                    score += 2
                if kw in error_sig:
                    score += 2
                if kw in category:
                    score += 1

            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:max_results]]

    def get_solutions_summary(self, max_chars: int = 4000) -> str:
        """Compact summary of all solutions for prompt context."""
        entries = self.load_solutions_index()
        if not entries:
            return ""

        lines: list[str] = []
        total_len = 0
        for entry in entries:
            title = entry.get("title", "?")
            category = entry.get("category", "?")
            tags = ", ".join(entry.get("tags", []))
            line = f"- [{category}] {title} (tags: {tags})"
            if total_len + len(line) + 1 > max_chars:
                lines.append(f"(... and {len(entries) - len(lines)} more solutions)")
                break
            lines.append(line)
            total_len += len(line) + 1

        return "\n".join(lines)
