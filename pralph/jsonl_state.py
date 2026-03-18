"""JSONL state — file-based CRUD operations for stories, phase state, run log, solutions."""
from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pralph.models import IterationResult, PhaseState, Story, StoryStatus


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, skipping blank/invalid lines."""
    if not path.exists():
        return []
    entries: list[dict] = []
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


def _append_jsonl(path: Path, entry: dict) -> None:
    """Append a single JSON object as a line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """Atomically rewrite a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry, default=str) + "\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _write_json(path: Path, data: dict) -> None:
    """Atomically write a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _read_json(path: Path) -> dict | None:
    """Read a JSON file, returning None if missing or invalid."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


class JsonlStateMixin:
    """Mixin providing JSONL/JSON file-based CRUD operations.

    Expects the host class to provide:
    - self.project_id: str
    - self._lock: threading.RLock
    - self.data_dir: Path (~/.pralph/<project-id>/)
    - self.solutions_dir: Path (from FileStateMixin)
    """

    # -- file paths (all under ~/.pralph/<project-id>/) --

    @property
    def _stories_path(self) -> Path:
        return self.data_dir / "stories.jsonl"  # type: ignore[attr-defined]

    @property
    def _status_log_path(self) -> Path:
        return self.data_dir / "status.jsonl"  # type: ignore[attr-defined]

    @property
    def _run_log_path(self) -> Path:
        return self.data_dir / "run-log.jsonl"  # type: ignore[attr-defined]

    @property
    def _phase_state_dir(self) -> Path:
        d = self.data_dir / "phases"  # type: ignore[attr-defined]
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def _phase1_analysis_path(self) -> Path:
        return self.data_dir / "phase1-analysis.json"  # type: ignore[attr-defined]

    @property
    def _solutions_index_path(self) -> Path:
        return self.solutions_dir / "index.jsonl"  # type: ignore[attr-defined]

    # -- phase1 analysis --

    def has_phase1_analysis(self) -> bool:
        return self._phase1_analysis_path.exists()

    def load_phase1_analysis(self) -> dict | None:
        return _read_json(self._phase1_analysis_path)

    def save_phase1_analysis(self, data: dict) -> None:
        with self._lock:  # type: ignore[attr-defined]
            _write_json(self._phase1_analysis_path, data)

    def delete_phase1_analysis(self) -> None:
        with self._lock:  # type: ignore[attr-defined]
            self._phase1_analysis_path.unlink(missing_ok=True)

    # -- stories --

    def _load_story_dicts(self) -> list[dict]:
        return _read_jsonl(self._stories_path)

    def load_stories(self) -> list[Story]:
        return [Story.from_dict(d) for d in self._load_story_dicts() if d.get("id")]

    def has_stories(self) -> bool:
        return self._stories_path.exists() and any(d.get("id") for d in _read_jsonl(self._stories_path))

    def append_stories(self, stories: list[Story]) -> None:
        with self._lock:  # type: ignore[attr-defined]
            existing_ids = self.get_story_ids()
            for s in stories:
                if s.id not in existing_ids:
                    _append_jsonl(self._stories_path, s.to_dict())
                    existing_ids.add(s.id)

    def get_pending_stories(self) -> list[Story]:
        return [s for s in self.load_stories() if s.status == StoryStatus.pending]

    def get_actionable_stories(self) -> list[Story]:
        stories = self.load_stories()
        rework = [s for s in stories if s.status == StoryStatus.rework]
        pending = [s for s in stories if s.status == StoryStatus.pending]
        return rework + pending

    def reset_error_stories(self) -> list[Story]:
        with self._lock:  # type: ignore[attr-defined]
            all_stories = self.load_stories()
            error_stories = [s for s in all_stories if s.status == StoryStatus.error]
            if not error_stories:
                return []

            error_ids = {s.id for s in error_stories}
            for s in error_stories:
                s.status = StoryStatus.pending
                s.metadata.pop("error_reason", None)
                s.metadata.pop("error_output", None)
                s.metadata.pop("error_at", None)
                _append_jsonl(self._status_log_path, {
                    "story_id": s.id, "status": "pending",
                    "summary": "Reset from error status",
                })

            updated = [s if s.id not in error_ids else next(e for e in error_stories if e.id == s.id) for s in all_stories]
            _write_jsonl(self._stories_path, [s.to_dict() for s in updated])
            return error_stories

    def recover_orphaned_stories(self) -> list[Story]:
        with self._lock:  # type: ignore[attr-defined]
            all_stories = self.load_stories()
            in_progress = [s for s in all_stories if s.status == StoryStatus.in_progress]
            if not in_progress:
                return []

            now = datetime.now().isoformat()
            orphan_ids = set()
            for s in in_progress:
                s.status = StoryStatus.pending
                s.metadata["previous_attempt"] = {"was_in_progress": True, "recovered_at": now}
                orphan_ids.add(s.id)
                _append_jsonl(self._status_log_path, {
                    "story_id": s.id, "status": "pending",
                    "summary": "Recovered from crash (was in_progress)",
                    "recovery": True,
                })

            updated = [s if s.id not in orphan_ids else next(o for o in in_progress if o.id == s.id) for s in all_stories]
            _write_jsonl(self._stories_path, [s.to_dict() for s in updated])
            return in_progress

    def get_story_ids(self) -> set[str]:
        return {d["id"] for d in _read_jsonl(self._stories_path) if d.get("id")}

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

    # -- story status --

    def mark_story_status(
        self,
        story_id: str,
        status: StoryStatus,
        summary: str = "",
        extra: dict | None = None,
        error_reason: str = "",
        error_output: str = "",
    ) -> None:
        with self._lock:  # type: ignore[attr-defined]
            log_extra = dict(extra or {})
            if error_reason:
                log_extra["error_reason"] = error_reason
            _append_jsonl(self._status_log_path, {
                "story_id": story_id, "status": status.value,
                "summary": summary, **log_extra,
            })

            all_stories = self.load_stories()
            for s in all_stories:
                if s.id == story_id:
                    s.status = status
                    if status == StoryStatus.error:
                        s.metadata["error_reason"] = error_reason or summary
                        if error_output:
                            s.metadata["error_output"] = error_output[-2000:]
                        s.metadata["error_at"] = datetime.now().isoformat()
                    break

            _write_jsonl(self._stories_path, [s.to_dict() for s in all_stories])

    def _rewrite_stories(self, stories: list[Story]) -> None:
        with self._lock:  # type: ignore[attr-defined]
            _write_jsonl(self._stories_path, [s.to_dict() for s in stories])

    def update_story(self, story: Story) -> None:
        with self._lock:  # type: ignore[attr-defined]
            all_stories = self.load_stories()
            for i, s in enumerate(all_stories):
                if s.id == story.id:
                    all_stories[i] = story
                    break
            _write_jsonl(self._stories_path, [s.to_dict() for s in all_stories])

    def delete_story(self, story_id: str) -> bool:
        with self._lock:  # type: ignore[attr-defined]
            all_stories = self.load_stories()
            before = len(all_stories)
            all_stories = [s for s in all_stories if s.id != story_id]
            if len(all_stories) == before:
                return False
            _write_jsonl(self._stories_path, [s.to_dict() for s in all_stories])
            _append_jsonl(self._status_log_path, {
                "story_id": story_id, "status": "deleted",
                "summary": "Deleted via edit command",
            })
            return True

    def get_implemented_summary(self) -> str:
        entries = _read_jsonl(self._status_log_path)
        count = len(entries)
        if count == 0:
            return ""
        return f"## Previously Implemented Stories\n\n{count} status transitions tracked"

    def load_status_log(self) -> list[dict]:
        return _read_jsonl(self._status_log_path)

    # -- phase state --

    def _phase_state_path(self, phase: str) -> Path:
        return self._phase_state_dir / f"{phase}.json"

    def load_phase_state(self, phase: str) -> PhaseState:
        data = _read_json(self._phase_state_path(phase))
        if data is None:
            return PhaseState(phase=phase)
        return PhaseState.from_dict(data)

    def save_phase_state(self, state: PhaseState) -> None:
        with self._lock:  # type: ignore[attr-defined]
            _write_json(self._phase_state_path(state.phase), state.to_dict())

    def load_all_phase_states(self) -> list[dict]:
        """Load all phase state files, returning list of dicts."""
        results = []
        if not self._phase_state_dir.exists():
            return results
        for p in sorted(self._phase_state_dir.glob("*.json")):
            data = _read_json(p)
            if data:
                results.append(data)
        return results

    # -- run log --

    def log_iteration(self, result: IterationResult) -> None:
        with self._lock:  # type: ignore[attr-defined]
            entry = result.to_dict()
            entry["logged_at"] = datetime.now().isoformat()
            _append_jsonl(self._run_log_path, entry)

    def load_run_log(self) -> list[dict]:
        """Load all run log entries."""
        return _read_jsonl(self._run_log_path)

    def get_story_tokens(self) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = defaultdict(
            lambda: {"input_tokens": 0, "output_tokens": 0,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        )
        for entry in _read_jsonl(self._run_log_path):
            sid = entry.get("story_id", "")
            if not sid:
                continue
            t = totals[sid]
            t["input_tokens"] += entry.get("input_tokens", 0)
            t["output_tokens"] += entry.get("output_tokens", 0)
            t["cache_read_input_tokens"] += entry.get("cache_read_input_tokens", 0)
            t["cache_creation_input_tokens"] += entry.get("cache_creation_input_tokens", 0)
        return dict(totals)

    # -- solutions (compound learning) --

    def has_solutions(self) -> bool:
        return self._solutions_index_path.exists() and bool(_read_jsonl(self._solutions_index_path))

    def save_solution(
        self,
        category: str,
        filename: str,
        content: str,
        index_entry: dict,
    ) -> Path:
        with self._lock:  # type: ignore[attr-defined]
            cat_dir = self.solutions_dir / category  # type: ignore[attr-defined]
            cat_dir.mkdir(parents=True, exist_ok=True)
            solution_path = cat_dir / filename
            solution_path.write_text(content)
            _append_jsonl(self._solutions_index_path, index_entry)
            return solution_path

    def load_solutions_index(self) -> list[dict]:
        entries = _read_jsonl(self._solutions_index_path)
        for e in entries:
            if isinstance(e.get("tags"), str):
                e["tags"] = json.loads(e["tags"])
        return entries

    def search_solutions(self, query: str, max_results: int = 5) -> list[dict]:
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
