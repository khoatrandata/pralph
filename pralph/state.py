from __future__ import annotations

import fcntl
import json
import os
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pralph.file_state import FileStateMixin

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


class ProjectNotInitializedError(Exception):
    """Raised when a command is run in a directory without a project.json."""
    pass


class _BaseStateManager(FileStateMixin):
    """Base class with shared init logic — no storage mixin yet."""

    def __init__(self, project_dir: str, *, project_name: str | None = None, readonly: bool = False, domains: list[str] | None = None) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.state_dir = self.project_dir / ".pralph"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._readonly = readonly
        self._domain_override = domains
        self._detected_domains: list[str] | None = None

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

    # -- domain detection --

    @property
    def domains_path(self) -> Path:
        return self.state_dir / "domains.txt"

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

    # -- project identity --

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
            or (self.state_dir / "design-doc.md").exists()
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

        def __init__(self, project_dir: str, *, project_name: str | None = None, readonly: bool = False, domains: list[str] | None = None) -> None:
            super().__init__(project_dir, project_name=project_name, readonly=readonly, domains=domains)
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
        def __init__(self, project_dir: str, *, project_name: str | None = None, readonly: bool = False, domains: list[str] | None = None) -> None:
            super().__init__(project_dir, project_name=project_name, readonly=readonly, domains=domains)
            if not readonly:
                self._migrate_data_dir()

    return JsonlStateManager


# Cache built classes
_duckdb_cls = None
_jsonl_cls = None


def StateManager(project_dir: str, *, project_name: str | None = None, readonly: bool = False, domains: list[str] | None = None) -> _BaseStateManager:
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
        return _duckdb_cls(project_dir, project_name=project_name, readonly=readonly, domains=domains)

    if _jsonl_cls is None:
        _jsonl_cls = _build_jsonl_class()
    return _jsonl_cls(project_dir, project_name=project_name, readonly=readonly, domains=domains)
