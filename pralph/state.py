from __future__ import annotations

import fcntl
import json
import os
from collections import defaultdict
from contextlib import contextmanager
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

# Secondary tag-to-domain associations for common keywords that aren't exact
# domain names but strongly imply one.
_TAG_DOMAIN_HINTS: dict[str, str] = {
    "container": "docker",
    "dockerfile": "docker",
    "docker-compose": "docker",
    "pip": "python",
    "pytest": "python",
    "pyproject": "python",
    "npm": "typescript",
    "yarn": "typescript",
    "node": "typescript",
    "cargo": "rust",
    "crate": "rust",
    "helm": "kubernetes",
    "k8s": "kubernetes",
    "pod": "kubernetes",
    "gradle": "java",
    "maven": "java",
    "cocoapods": "cocoapods",
    "xcode": "swift-ios",
    "swift-package": "swift-ios",
    "flutter": "flutter",
    "dart": "flutter",
    "tf": "terraform",
    "hcl": "terraform",
}

# Known error prefixes/substrings mapped to domains.
_ERROR_DOMAIN_HINTS: list[tuple[str, str]] = [
    ("ModuleNotFoundError", "python"),
    ("ImportError", "python"),
    ("SyntaxError", "python"),
    ("IndentationError", "python"),
    ("cannot find module", "typescript"),
    ("Module not found", "typescript"),
    ("TS2305", "typescript"),
    ("TS2307", "typescript"),
    ("ReferenceError", "javascript"),
    ("cargo build", "rust"),
    ("rustc", "rust"),
    ("go build", "go"),
    ("undefined reference", "cpp"),
]


def _infer_solution_domains(
    related_files: list[str],
    tags: list[str],
    error_signature: str,
    available_domains: list[str],
) -> set[str]:
    """Infer which domain(s) a solution applies to from its metadata.

    Returns the subset of *available_domains* that match.  Returns an empty set
    if nothing could be inferred (caller should fall back to all domains).
    """
    import fnmatch as _fnmatch

    all_domains_set = set(available_domains)
    matched: set[str] = set()

    # A) File-extension matching via _DOMAIN_DETECTION_RULES
    for fpath in related_files:
        fname = fpath.rsplit("/", 1)[-1] if "/" in fpath else fpath
        for pattern, domain in _DOMAIN_DETECTION_RULES:
            if "/" in pattern:
                continue  # skip path-based rules
            if _fnmatch.fnmatch(fname, pattern):
                matched.add(domain)

    # B) Tag matching — exact domain name or hint lookup
    for tag in tags:
        tag_lower = tag.lower()
        if tag_lower in all_domains_set:
            matched.add(tag_lower)
        if tag_lower in _TAG_DOMAIN_HINTS:
            matched.add(_TAG_DOMAIN_HINTS[tag_lower])

    # C) Error signature matching
    if error_signature:
        sig_lower = error_signature.lower()
        for pattern, domain in _ERROR_DOMAIN_HINTS:
            if pattern.lower() in sig_lower:
                matched.add(domain)

    # D) Intersect with available domains
    return matched & all_domains_set


def _infer_domains_llm(
    *,
    content: str,
    title: str,
    category: str,
    tags: list[str],
    error_signature: str,
    available_domains: list[str],
) -> set[str]:
    """Ask Haiku to infer domain(s) from solution content.

    Returns the subset of *available_domains* that match, or an empty set on
    failure (caller should fall back to all domains).
    """
    from pralph.parser import extract_json_from_text
    from pralph.prompts.compact import INFER_DOMAIN_PROMPT
    from pralph.runner import run_with_retry

    prompt = INFER_DOMAIN_PROMPT
    prompt = prompt.replace("{{available_domains}}", ", ".join(available_domains))
    prompt = prompt.replace("{{title}}", title)
    prompt = prompt.replace("{{category}}", category)
    prompt = prompt.replace("{{tags}}", ", ".join(tags))
    prompt = prompt.replace("{{error_signature}}", error_signature or "(none)")
    # Cap content to avoid blowing context for a cheap inference call
    truncated = content[:4000] if len(content) > 4000 else content
    prompt = prompt.replace("{{content}}", truncated)

    result = run_with_retry(prompt, model="haiku", timeout=30, max_retries=1)
    if not result.success:
        return set()

    parsed = extract_json_from_text(result.result)
    if not isinstance(parsed, dict) or "domains" not in parsed:
        return set()

    domains_set = set(available_domains)
    return {d for d in parsed["domains"] if isinstance(d, str) and d in domains_set}


def _safe_resolve(base: Path, untrusted: str) -> Path | None:
    """Resolve *untrusted* relative to *base*, returning None if it escapes."""
    resolved = (base / untrusted).resolve()
    if not resolved.is_relative_to(base.resolve()):
        return None
    return resolved


@contextmanager
def _index_lock(index_path: Path):
    """Acquire an exclusive file lock next to *index_path*.

    Both solution saves and index compaction use this lock so that compaction
    blocks new writes and vice-versa.  The lock is blocking (waits until
    released).
    """
    lock_path = index_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


class StateManager:
    def __init__(self, project_dir: str, *, domains: list[str] | None = None) -> None:
        self.project_dir = Path(project_dir)
        self.state_dir = self.project_dir / ".pralph"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._domain_override = domains
        self._detected_domains: list[str] | None = None

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
        Results are cached for the lifetime of this StateManager instance.
        """
        if self._detected_domains is not None:
            return self._detected_domains

        if self._domain_override:
            self._detected_domains = self._domain_override
            return self._detected_domains

        # Project-level explicit config
        if self.domains_path.exists():
            raw = self.domains_path.read_text().strip()
            if raw:
                self._detected_domains = [d.strip() for d in raw.splitlines() if d.strip() and not d.strip().startswith("#")]
                return self._detected_domains

        # Auto-detect from project files (only scan top two levels for speed)
        import fnmatch as _fnmatch

        found: set[str] = set()
        try:
            entries = list(self.project_dir.iterdir())
        except OSError:
            self._detected_domains = []
            return self._detected_domains

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

        self._detected_domains = sorted(found)
        return self._detected_domains

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
        solution_path = _safe_resolve(self.solutions_dir, f"{category}/{filename}")
        if solution_path is None:
            raise ValueError(f"Invalid solution path: {category}/{filename}")

        with _index_lock(self.solutions_index_path):
            solution_path.parent.mkdir(parents=True, exist_ok=True)
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

    def read_solution(self, filename: str) -> str:
        """Read a specific solution file."""
        path = _safe_resolve(self.solutions_dir, filename)
        if path is not None and path.exists():
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
        """Save a solution to ~/.pralph/solutions/{domain}/ for relevant detected domains.

        Uses heuristics (related_files, tags, error_signature) to infer which
        domain(s) a solution applies to.  Falls back to Haiku LLM inference when
        heuristics return nothing.

        The index_entry is augmented with source_project for traceability.
        Returns list of paths written.
        """
        all_domains = self.detect_domains()
        if not all_domains:
            return []

        inferred = _infer_solution_domains(
            index_entry.get("related_files", []),
            index_entry.get("tags", []),
            index_entry.get("error_signature", ""),
            all_domains,
        )
        if not inferred:
            inferred = _infer_domains_llm(
                content=content,
                title=index_entry.get("title", ""),
                category=index_entry.get("category", ""),
                tags=index_entry.get("tags", []),
                error_signature=index_entry.get("error_signature", ""),
                available_domains=all_domains,
            )
        domains = inferred if inferred else set(all_domains)

        entry = {
            **index_entry,
            "source_project": str(self.project_dir),
        }

        paths: list[Path] = []
        for domain in domains:
            domain_dir = self._global_domain_solutions_dir(domain)
            solution_path = _safe_resolve(domain_dir, f"{category}/{filename}")
            if solution_path is None:
                continue

            idx_path = self._global_domain_index_path(domain)
            with _index_lock(idx_path):
                solution_path.parent.mkdir(parents=True, exist_ok=True)
                solution_path.write_text(content)

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

    def read_global_solution(self, domain: str, filename: str) -> str:
        """Read a specific solution file from global domain store."""
        path = _safe_resolve(self._global_domain_solutions_dir(domain), filename)
        if path is not None and path.exists():
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

    # -- index compaction --

    def compact_local_index(
        self,
        *,
        model: str = "haiku",
        verbose: bool = False,
        dangerously_skip_permissions: bool = False,
    ) -> dict:
        """Compact the project-local solutions index using an LLM to merge duplicates.

        Returns stats dict with original, kept, merged, removed counts and cost.
        """
        return self._compact_index(
            self.solutions_index_path,
            self.solutions_dir,
            model=model,
            verbose=verbose,
            dangerously_skip_permissions=dangerously_skip_permissions,
        )

    def compact_global_indexes(
        self,
        *,
        model: str = "haiku",
        verbose: bool = False,
        dangerously_skip_permissions: bool = False,
    ) -> list[dict]:
        """Compact global solution indexes for all detected domains.

        Returns a list of stats dicts, one per domain.
        """
        results = []
        for domain in self.detect_domains():
            idx_path = self._global_domain_index_path(domain)
            if not idx_path.exists():
                continue
            stats = self._compact_index(
                idx_path,
                self._global_domain_solutions_dir(domain),
                model=model,
                verbose=verbose,
                dangerously_skip_permissions=dangerously_skip_permissions,
            )
            stats["domain"] = domain
            results.append(stats)
        return results

    @staticmethod
    def _compact_index(
        index_path: Path,
        solutions_base: Path,
        *,
        model: str = "haiku",
        verbose: bool = False,
        dangerously_skip_permissions: bool = False,
    ) -> dict:
        """Use an LLM to semantically merge duplicate solutions and prune orphans.

        Acquires an exclusive lock so concurrent saves wait until compaction
        finishes, and only one compaction runs at a time.  Rewrites index and
        solution files atomically.  Returns stats.
        """
        import re as _re

        from pralph.parser import extract_json_from_text
        from pralph.prompts.compact import COMPACT_INDEX_PROMPT
        from pralph.runner import run_with_retry

        if not index_path.exists():
            return {"original": 0, "kept": 0, "merged": 0, "removed": 0, "cost": 0.0}

        with _index_lock(index_path):
            # -- read current state under lock --
            raw_lines = index_path.read_text().splitlines()
            entries: list[dict] = []
            for line in raw_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        entries.append(parsed)
                except json.JSONDecodeError:
                    continue

            if not entries:
                return {"original": 0, "kept": 0, "merged": 0, "removed": 0, "cost": 0.0}

            original = len(entries)

            # Pre-prune exact filename duplicates (keep last) and orphans
            # to reduce what we send to the LLM
            seen: dict[str, int] = {}
            for i, entry in enumerate(entries):
                seen[entry.get("filename", "")] = i
            deduped = [entries[i] for i in sorted(seen.values())]
            exact_dupes = original - len(deduped)

            valid: list[dict] = []
            pre_orphans = 0
            for entry in deduped:
                filename = entry.get("filename", "")
                if not filename:
                    pre_orphans += 1
                    continue
                solution_path = _safe_resolve(solutions_base, filename)
                if solution_path is not None and solution_path.exists():
                    valid.append(entry)
                else:
                    pre_orphans += 1

            if len(valid) <= 1:
                # Nothing to merge — just rewrite the cleaned index
                tmp_path = index_path.with_suffix(".tmp")
                with open(tmp_path, "w") as f:
                    for entry in valid:
                        f.write(json.dumps(entry) + "\n")
                os.replace(tmp_path, index_path)
                return {
                    "original": original,
                    "kept": len(valid),
                    "merged": 0,
                    "removed": exact_dupes + pre_orphans,
                    "cost": 0.0,
                }

            # -- build prompt with index + solution contents --
            index_json = json.dumps(valid, indent=2)

            contents_parts: list[str] = []
            for entry in valid:
                filename = entry.get("filename", "")
                path = _safe_resolve(solutions_base, filename)
                if path is not None and path.exists():
                    body = path.read_text().strip()
                    # Cap per-file to avoid blowing context
                    if len(body) > 3000:
                        body = body[:3000] + "\n\n(truncated)"
                    contents_parts.append(f"### {filename}\n\n{body}")

            contents_text = "\n\n---\n\n".join(contents_parts)

            prompt = COMPACT_INDEX_PROMPT
            prompt = prompt.replace("{{index_entries}}", index_json)
            prompt = prompt.replace("{{solution_contents}}", contents_text)

            # -- call LLM --
            result = run_with_retry(
                prompt,
                model=model,
                timeout=120,
                verbose=verbose,
                dangerously_skip_permissions=dangerously_skip_permissions,
            )

            cost = result.cost_usd

            if not result.success:
                # LLM failed — fall back to the pre-pruned state (still useful)
                tmp_path = index_path.with_suffix(".tmp")
                with open(tmp_path, "w") as f:
                    for entry in valid:
                        f.write(json.dumps(entry) + "\n")
                os.replace(tmp_path, index_path)
                return {
                    "original": original,
                    "kept": len(valid),
                    "merged": 0,
                    "removed": exact_dupes + pre_orphans,
                    "cost": cost,
                }

            # -- parse LLM response --
            parsed = extract_json_from_text(result.result)
            if not isinstance(parsed, dict) or "entries" not in parsed:
                # Bad parse — keep pre-pruned state
                tmp_path = index_path.with_suffix(".tmp")
                with open(tmp_path, "w") as f:
                    for entry in valid:
                        f.write(json.dumps(entry) + "\n")
                os.replace(tmp_path, index_path)
                return {
                    "original": original,
                    "kept": len(valid),
                    "merged": 0,
                    "removed": exact_dupes + pre_orphans,
                    "cost": cost,
                }

            new_entries = parsed["entries"]
            merges = parsed.get("merges", [])
            removed = parsed.get("removed", [])

            # -- slugify helper (inline to keep static) --
            def _slugify(text: str) -> str:
                slug = text.lower().strip()
                slug = _re.sub(r"[^\w\s-]", "", slug)
                slug = _re.sub(r"[\s_]+", "-", slug)
                slug = _re.sub(r"-+", "-", slug)
                return slug[:80].strip("-")

            # -- write merged solution files + rebuild index --
            old_filenames = {e.get("filename", "") for e in valid}
            new_filenames: set[str] = set()
            final_entries: list[dict] = []

            for entry in new_entries:
                if not isinstance(entry, dict):
                    continue
                content = entry.pop("content", "")
                filename = entry.get("filename", "")

                # Generate filename from title if missing or changed
                if not filename:
                    category = entry.get("category", "misc")
                    title = entry.get("title", "untitled")
                    filename = f"{category}/{_slugify(title)}.md"
                    entry["filename"] = filename

                resolved = _safe_resolve(solutions_base, filename)
                if resolved is None:
                    continue

                if content:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    resolved.write_text(content)

                new_filenames.add(filename)
                final_entries.append(entry)

            # Clean up files that were merged away
            merged_sources: set[str] = set()
            for merge in merges:
                if isinstance(merge, dict):
                    for src in merge.get("sources", []):
                        merged_sources.add(src)

            removed_filenames: set[str] = set()
            for rem in removed:
                if isinstance(rem, dict):
                    removed_filenames.add(rem.get("filename", ""))

            for old_fn in (merged_sources | removed_filenames) - new_filenames:
                if old_fn:
                    old_path = _safe_resolve(solutions_base, old_fn)
                    if old_path is not None and old_path.exists():
                        old_path.unlink()

            # Atomic index rewrite
            tmp_path = index_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                for entry in final_entries:
                    f.write(json.dumps(entry) + "\n")
            os.replace(tmp_path, index_path)

        return {
            "original": original,
            "kept": len(final_entries),
            "merged": len(merges),
            "removed": exact_dupes + pre_orphans + len(removed),
            "cost": cost,
        }
