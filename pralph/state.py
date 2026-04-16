from __future__ import annotations

import json
import threading
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
