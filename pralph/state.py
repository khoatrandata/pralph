from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pralph.models import IterationResult, PhaseState, Story, StoryStatus

# Map file patterns to domain names for auto-detection.
# Each entry: (glob_pattern, domain_name)
_DOMAIN_DETECTION_RULES: list[tuple[str, str]] = [
    # Languages
    ("*.swift", "swift-ios"),
    ("Package.swift", "swift-ios"),
    ("*.xcodeproj", "swift-ios"),
    ("*.xcworkspace", "swift-ios"),
    ("Cargo.toml", "rust"),
    ("*.rs", "rust"),
    ("*.go", "go"),
    ("go.mod", "go"),
    ("*.py", "python"),
    ("pyproject.toml", "python"),
    ("*.ts", "typescript"),
    ("*.tsx", "typescript"),
    ("package.json", "typescript"),
    ("*.js", "javascript"),
    ("*.jsx", "javascript"),
    ("*.java", "java"),
    ("*.kt", "kotlin"),
    ("*.cs", "csharp"),
    ("*.rb", "ruby"),
    ("Gemfile", "ruby"),
    ("*.ex", "elixir"),
    ("*.exs", "elixir"),
    ("*.zig", "zig"),
    ("*.cpp", "cpp"),
    ("*.c", "c"),
    ("CMakeLists.txt", "cmake"),
    # Platforms / tools
    ("Dockerfile", "docker"),
    ("docker-compose.yml", "docker"),
    ("docker-compose.yaml", "docker"),
    ("terraform/*.tf", "terraform"),
    ("*.tf", "terraform"),
    ("serverless.yml", "serverless"),
    ("template.yaml", "cloudformation"),
    ("cdk.json", "aws-cdk"),
    ("pulumi.yaml", "pulumi"),
    ("k8s/*.yaml", "kubernetes"),
    ("*.proto", "protobuf"),
    ("flutter/pubspec.yaml", "flutter"),
    ("pubspec.yaml", "flutter"),
    ("*.dart", "flutter"),
    ("android/build.gradle", "android"),
    ("Podfile", "cocoapods"),
]


class StateManager:
    def __init__(self, project_dir: str, *, domains: list[str] | None = None) -> None:
        self.project_dir = Path(project_dir)
        self.state_dir = self.project_dir / ".pralph"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._domain_override = domains

    # -- config --

    def _load_config(self, path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def get_config(self, key: str, default=None):
        """Resolve a config value: project .pralph/config.json > ~/.pralph/config.json > default."""
        project_cfg = self._load_config(self.state_dir / "config.json")
        if key in project_cfg:
            return project_cfg[key]
        home_cfg = self._load_config(self.home_dir / "config.json")
        if key in home_cfg:
            return home_cfg[key]
        return default

    @property
    def global_compound(self) -> bool:
        """Whether compound learnings should be saved globally."""
        return bool(self.get_config("global_compound", False))

    # -- file paths --

    @property
    def design_doc_path(self) -> Path:
        return self.state_dir / "design-doc.md"

    @property
    def guardrails_path(self) -> Path:
        return self.state_dir / "guardrails.md"

    @property
    def stories_path(self) -> Path:
        return self.state_dir / "stories.jsonl"

    @property
    def status_path(self) -> Path:
        return self.state_dir / "status.jsonl"

    @property
    def run_log_path(self) -> Path:
        return self.state_dir / "run-log.jsonl"

    @property
    def phase_state_path(self) -> Path:
        return self.state_dir / "phase-state.json"

    @property
    def phase1_analysis_path(self) -> Path:
        return self.state_dir / "phase1-analysis.json"

    @property
    def research_notes_path(self) -> Path:
        return self.state_dir / "research-notes.md"

    @property
    def ideas_path(self) -> Path:
        return self.state_dir / "ideas.md"

    def phase_prompt_path(self, phase: str) -> Path:
        return self.state_dir / f"{phase}-prompt.md"

    @property
    def home_dir(self) -> Path:
        return Path.home() / ".pralph"

    @property
    def domains_path(self) -> Path:
        return self.state_dir / "domains.txt"

    # -- domain detection --

    def detect_domains(self) -> list[str]:
        """Return the active domains for this project.

        Resolution order: CLI override > .pralph/domains.txt > auto-detect from files.
        """
        if self._domain_override:
            return self._domain_override

        # Project-level explicit config
        if self.domains_path.exists():
            raw = self.domains_path.read_text().strip()
            if raw:
                return [d.strip() for d in raw.splitlines() if d.strip() and not d.strip().startswith("#")]

        # Auto-detect from project files (only scan top two levels for speed)
        import fnmatch as _fnmatch

        found: set[str] = set()
        try:
            entries = list(self.project_dir.iterdir())
        except OSError:
            return []

        # Level 0 (project root)
        names_l0 = [e.name for e in entries]
        for pattern, domain in _DOMAIN_DETECTION_RULES:
            if "/" in pattern:
                continue  # skip nested patterns for level 0
            for name in names_l0:
                if _fnmatch.fnmatch(name, pattern):
                    found.add(domain)
                    break

        # Level 1 (immediate subdirs) — only check dirs, skip hidden/vendor
        _SKIP_DIRS = {".git", ".pralph", "node_modules", ".build", "build", "target", "vendor", "__pycache__", ".venv", "venv"}
        for entry in entries:
            if not entry.is_dir() or entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            try:
                subnames = [e.name for e in entry.iterdir()]
            except OSError:
                continue
            for pattern, domain in _DOMAIN_DETECTION_RULES:
                if "/" in pattern:
                    # e.g. "terraform/*.tf" — check if entry.name matches prefix
                    parts = pattern.split("/", 1)
                    if _fnmatch.fnmatch(entry.name, parts[0]):
                        for sn in subnames:
                            if _fnmatch.fnmatch(sn, parts[1]):
                                found.add(domain)
                                break
                else:
                    for sn in subnames:
                        if _fnmatch.fnmatch(sn, pattern):
                            found.add(domain)
                            break

        return sorted(found)

    def read_phase_prompt(self, phase: str) -> str:
        # Project-level overrides home-level
        path = self.phase_prompt_path(phase)
        if path.exists():
            return path.read_text().strip()
        home_path = self.home_dir / f"{phase}-prompt.md"
        if home_path.exists():
            return home_path.read_text().strip()
        return ""

    def resolve_prompt_template(self, name: str, default: str) -> str:
        """Resolve a prompt template: project prompts/ > home prompts/ > built-in default."""
        project_path = self.state_dir / "prompts" / f"{name}.md"
        if project_path.exists():
            return project_path.read_text()
        home_path = self.home_dir / "prompts" / f"{name}.md"
        if home_path.exists():
            return home_path.read_text()
        return default

    @property
    def extra_tools_path(self) -> Path:
        return self.state_dir / "extra-tools.txt"

    def read_extra_tools(self) -> str:
        """Read project-level extra tools (one per line or comma-separated)."""
        if not self.extra_tools_path.exists():
            return ""
        raw = self.extra_tools_path.read_text().strip()
        # Normalize: support one-per-line or comma-separated
        tools = [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]
        return ",".join(tools)

    # -- review feedback --

    @property
    def review_feedback_dir(self) -> Path:
        return self.state_dir / "review-feedback"

    def review_feedback_path(self, story_id: str) -> Path:
        return self.review_feedback_dir / f"{story_id}.md"

    def write_review_feedback(self, story_id: str, feedback: str) -> None:
        self.review_feedback_dir.mkdir(parents=True, exist_ok=True)
        self.review_feedback_path(story_id).write_text(feedback)

    def read_review_feedback(self, story_id: str) -> str:
        path = self.review_feedback_path(story_id)
        if path.exists():
            return path.read_text().strip()
        return ""

    def clear_review_feedback(self, story_id: str) -> None:
        self.review_feedback_path(story_id).unlink(missing_ok=True)

    # -- claude session validation --

    def claude_session_exists(self, session_id: str) -> bool:
        """Check if a Claude session file exists on disk."""
        encoded = str(self.project_dir).replace("/", "-")
        path = Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
        return path.exists()

    # -- design doc --

    def read_design_doc(self) -> str:
        if self.design_doc_path.exists():
            return self.design_doc_path.read_text()
        return ""

    def write_design_doc(self, content: str) -> None:
        self.design_doc_path.write_text(content)

    def has_design_doc(self) -> bool:
        return self.design_doc_path.exists() and self.design_doc_path.stat().st_size > 0

    # -- guardrails --

    def read_guardrails(self) -> str:
        if self.guardrails_path.exists():
            return self.guardrails_path.read_text()
        return ""

    # -- stories --

    def load_stories(self) -> list[Story]:
        stories: list[Story] = []
        if not self.stories_path.exists():
            return stories
        for line in self.stories_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                stories.append(Story.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
        return stories

    def append_stories(self, stories: list[Story]) -> None:
        with open(self.stories_path, "a") as f:
            for s in stories:
                f.write(json.dumps(s.to_dict()) + "\n")

    def get_pending_stories(self) -> list[Story]:
        return [s for s in self.load_stories() if s.status == StoryStatus.pending]

    def get_actionable_stories(self) -> list[Story]:
        """Return stories that are pending or need rework (rework first)."""
        stories = self.load_stories()
        rework = [s for s in stories if s.status == StoryStatus.rework]
        pending = [s for s in stories if s.status == StoryStatus.pending]
        return rework + pending

    def reset_error_stories(self) -> list[Story]:
        """Find stories with error status and reset them to pending."""
        stories = self.load_stories()
        reset: list[Story] = []

        for s in stories:
            if s.status == StoryStatus.error:
                s.status = StoryStatus.pending
                s.metadata.pop("error_reason", None)
                s.metadata.pop("error_output", None)
                s.metadata.pop("error_at", None)
                reset.append(s)

        if reset:
            self._rewrite_stories(stories)
            with open(self.status_path, "a") as f:
                for s in reset:
                    entry = {
                        "story_id": s.id,
                        "status": "pending",
                        "summary": "Reset from error status",
                    }
                    f.write(json.dumps(entry) + "\n")

        return reset

    def recover_orphaned_stories(self) -> list[Story]:
        """Find in_progress stories (orphans from crashes) and reset to pending."""
        stories = self.load_stories()
        recovered: list[Story] = []

        for s in stories:
            if s.status == StoryStatus.in_progress:
                s.status = StoryStatus.pending
                s.metadata["previous_attempt"] = {
                    "was_in_progress": True,
                    "recovered_at": datetime.now().isoformat(),
                }
                recovered.append(s)

        if recovered:
            self._rewrite_stories(stories)
            with open(self.status_path, "a") as f:
                for s in recovered:
                    entry = {
                        "story_id": s.id,
                        "status": "pending",
                        "summary": "Recovered from crash (was in_progress)",
                        "recovery": True,
                    }
                    f.write(json.dumps(entry) + "\n")

        return recovered

    def get_story_ids(self) -> set[str]:
        return {s.id for s in self.load_stories()}

    def get_category_stats(self) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "next_id": 1})
        for story in self.load_stories():
            cat = story.category.upper()
            if not cat:
                continue
            stats[cat]["count"] += 1
            # Parse numeric suffix from id like "AUTH-043"
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
        entry = {
            "story_id": story_id,
            "status": status.value,
            "summary": summary,
            **(extra or {}),
        }
        if error_reason:
            entry["error_reason"] = error_reason
        with open(self.status_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        # Also rewrite stories.jsonl with updated status
        stories = self.load_stories()
        for s in stories:
            if s.id == story_id:
                s.status = status
                if status == StoryStatus.error:
                    s.metadata["error_reason"] = error_reason or summary
                    if error_output:
                        # Keep last 2000 chars of output for context
                        s.metadata["error_output"] = error_output[-2000:]
                    s.metadata["error_at"] = datetime.now().isoformat()
                break
        self._rewrite_stories(stories)

    def _rewrite_stories(self, stories: list[Story]) -> None:
        with open(self.stories_path, "w") as f:
            for s in stories:
                f.write(json.dumps(s.to_dict()) + "\n")

    def get_implemented_summary(self) -> str:
        if not self.status_path.exists():
            return ""
        count = 0
        for raw in self.status_path.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                json.loads(raw)
                count += 1
            except json.JSONDecodeError:
                continue
        if count == 0:
            return ""
        return f"## Previously Implemented Stories\n\n{count} stories tracked in {self.status_path}"

    # -- phase state --

    def load_phase_state(self, phase: str) -> PhaseState:
        if self.phase_state_path.exists():
            try:
                data = json.loads(self.phase_state_path.read_text())
                if data.get("phase") == phase:
                    return PhaseState.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                pass
        return PhaseState(phase=phase)

    def save_phase_state(self, state: PhaseState) -> None:
        self.phase_state_path.write_text(json.dumps(state.to_dict(), indent=2) + "\n")

    # -- run log --

    def log_iteration(self, result: IterationResult) -> None:
        with open(self.run_log_path, "a") as f:
            f.write(json.dumps(result.to_dict()) + "\n")

    def get_story_tokens(self) -> dict[str, dict[str, int]]:
        """Aggregate tokens per story from run-log.jsonl."""
        totals: dict[str, dict[str, int]] = {}
        if not self.run_log_path.exists():
            return totals
        for line in self.run_log_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            story_id = entry.get("story_id")
            if not story_id:
                continue
            if story_id not in totals:
                totals[story_id] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
            totals[story_id]["input_tokens"] += entry.get("input_tokens", 0)
            totals[story_id]["output_tokens"] += entry.get("output_tokens", 0)
            totals[story_id]["cache_read_input_tokens"] += entry.get("cache_read_input_tokens", 0)
            totals[story_id]["cache_creation_input_tokens"] += entry.get("cache_creation_input_tokens", 0)
        return totals

    # -- solutions (compound learning) --

    @property
    def solutions_dir(self) -> Path:
        return self.state_dir / "solutions"

    @property
    def solutions_index_path(self) -> Path:
        return self.solutions_dir / "index.jsonl"

    def has_solutions(self) -> bool:
        return self.solutions_index_path.exists() and self.solutions_index_path.stat().st_size > 0

    def save_solution(
        self,
        category: str,
        filename: str,
        content: str,
        index_entry: dict,
    ) -> Path:
        """Write a solution markdown file and append to index."""
        cat_dir = self.solutions_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        solution_path = cat_dir / filename
        solution_path.write_text(content)

        # Append index entry
        with open(self.solutions_index_path, "a") as f:
            f.write(json.dumps(index_entry) + "\n")

        return solution_path

    def load_solutions_index(self) -> list[dict]:
        """Read all index entries."""
        entries: list[dict] = []
        if not self.solutions_index_path.exists():
            return entries
        for line in self.solutions_index_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    entries.append(parsed)
            except json.JSONDecodeError:
                continue
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
            if not isinstance(entry, dict):
                continue
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

    def read_solution(self, filename: str) -> str:
        """Read a specific solution file."""
        path = self.solutions_dir / filename
        if path.exists():
            return path.read_text()
        return ""

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

    # -- global solutions (cross-project compound learning) --

    def _global_domain_solutions_dir(self, domain: str) -> Path:
        return self.home_dir / "solutions" / domain

    def _global_domain_index_path(self, domain: str) -> Path:
        return self._global_domain_solutions_dir(domain) / "index.jsonl"

    def save_solution_global(
        self,
        category: str,
        filename: str,
        content: str,
        index_entry: dict,
    ) -> list[Path]:
        """Save a solution to ~/.pralph/solutions/{domain}/ for each detected domain.

        The index_entry is augmented with source_project for traceability.
        Returns list of paths written.
        """
        domains = self.detect_domains()
        if not domains:
            return []

        entry = {
            **index_entry,
            "source_project": str(self.project_dir),
        }

        paths: list[Path] = []
        for domain in domains:
            domain_dir = self._global_domain_solutions_dir(domain)
            cat_dir = domain_dir / category
            cat_dir.mkdir(parents=True, exist_ok=True)
            solution_path = cat_dir / filename
            solution_path.write_text(content)

            idx_path = self._global_domain_index_path(domain)
            with open(idx_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

            paths.append(solution_path)

        return paths

    def has_global_solutions(self) -> bool:
        """Check if any global solutions exist for the project's detected domains."""
        for domain in self.detect_domains():
            idx = self._global_domain_index_path(domain)
            if idx.exists() and idx.stat().st_size > 0:
                return True
        return False

    def load_global_solutions_index(self) -> list[dict]:
        """Load index entries from all detected domains in ~/.pralph/solutions/."""
        seen_filenames: set[str] = set()
        entries: list[dict] = []
        for domain in self.detect_domains():
            idx = self._global_domain_index_path(domain)
            if not idx.exists():
                continue
            for line in idx.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if not isinstance(parsed, dict):
                        continue
                    # Deduplicate: same filename within a domain
                    key = f"{domain}/{parsed.get('filename', '')}"
                    if key in seen_filenames:
                        continue
                    seen_filenames.add(key)
                    parsed["_domain"] = domain
                    entries.append(parsed)
                except json.JSONDecodeError:
                    continue
        return entries

    def search_global_solutions(self, query: str, max_results: int = 5) -> list[dict]:
        """Keyword search across global domain-matched solutions."""
        entries = self.load_global_solutions_index()
        if not entries:
            return []

        keywords = query.lower().split()

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

    def read_global_solution(self, domain: str, filename: str) -> str:
        """Read a specific solution file from global domain store."""
        path = self._global_domain_solutions_dir(domain) / filename
        if path.exists():
            return path.read_text()
        return ""

    def get_global_solutions_summary(self, max_chars: int = 3000) -> str:
        """Compact summary of global solutions for the detected domains."""
        entries = self.load_global_solutions_index()
        if not entries:
            return ""

        lines: list[str] = []
        total_len = 0
        for entry in entries:
            title = entry.get("title", "?")
            category = entry.get("category", "?")
            domain = entry.get("_domain", "?")
            tags = ", ".join(entry.get("tags", []))
            line = f"- [{domain}/{category}] {title} (tags: {tags})"
            if total_len + len(line) + 1 > max_chars:
                lines.append(f"(... and {len(entries) - len(lines)} more global solutions)")
                break
            lines.append(line)
            total_len += len(line) + 1

        return "\n".join(lines)

    def search_all_solutions(self, query: str, max_results: int = 5) -> list[dict]:
        """Search both project-local and global solutions, merged by relevance.

        Project-local results are prioritised (score boosted) over global ones.
        """
        keywords = query.lower().split()

        def _score(entry: dict) -> int:
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
            return score

        scored: list[tuple[int, dict]] = []
        seen_titles: set[str] = set()

        # Project-local (boosted by +5)
        for entry in self.load_solutions_index():
            if not isinstance(entry, dict):
                continue
            s = _score(entry)
            if s > 0:
                entry["_source"] = "local"
                scored.append((s + 5, entry))
                seen_titles.add((entry.get("title") or "").lower())

        # Global (no boost, skip duplicates by title)
        for entry in self.load_global_solutions_index():
            title_key = (entry.get("title") or "").lower()
            if title_key in seen_titles:
                continue
            s = _score(entry)
            if s > 0:
                entry["_source"] = "global"
                scored.append((s, entry))
                seen_titles.add(title_key)

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:max_results]]

    def read_any_solution(self, entry: dict) -> str:
        """Read a solution file, handling both local and global entries."""
        filename = entry.get("filename", "")
        if entry.get("_source") == "global":
            domain = entry.get("_domain", "")
            if domain:
                return self.read_global_solution(domain, filename)
        return self.read_solution(filename)
