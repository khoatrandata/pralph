"""File-system state — path properties and file I/O for design docs, prompts, etc."""
from __future__ import annotations

from pathlib import Path


class FileStateMixin:
    """Mixin providing file-system path properties and I/O methods.

    Expects the host class to provide:
    - self.state_dir: Path
    """

    state_dir: Path

    # -- file paths (markdown files remain on disk) --

    @property
    def design_doc_path(self) -> Path:
        return self.state_dir / "design-doc.md"

    @property
    def guardrails_path(self) -> Path:
        return self.state_dir / "guardrails.md"

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

    def read_phase_prompt(self, phase: str) -> str:
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
        tools = [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]
        return ",".join(tools)

    # -- review feedback (stays on disk — Claude reads by path) --

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
        encoded = str(self.project_dir).replace("/", "-")  # type: ignore[attr-defined]
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

    # -- solutions directory (under ~/.pralph/<project-id>/) --

    @property
    def solutions_dir(self) -> Path:
        return self.data_dir / "solutions"  # type: ignore[attr-defined]

    def read_solution(self, filename: str) -> str:
        """Read a specific solution file."""
        path = self.solutions_dir / filename
        if path.exists():
            return path.read_text()
        return ""
