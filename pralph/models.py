from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class StoryStatus(enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    implemented = "implemented"
    rework = "rework"
    skipped = "skipped"
    duplicate = "duplicate"
    external = "external"
    error = "error"


@dataclass
class Story:
    id: str
    title: str
    content: str
    acceptance_criteria: list[str] = field(default_factory=list)
    priority: int = 3
    category: str = ""
    complexity: str = "medium"
    dependencies: list[str] = field(default_factory=list)
    source: str = "extract"
    status: StoryStatus = StoryStatus.pending
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "acceptance_criteria": self.acceptance_criteria,
            "priority": self.priority,
            "category": self.category,
            "complexity": self.complexity,
            "dependencies": self.dependencies,
            "source": self.source,
            "status": self.status.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Story:
        status = d.get("status", "pending")
        if isinstance(status, str):
            status = StoryStatus(status)
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            content=d.get("content", ""),
            acceptance_criteria=d.get("acceptance_criteria", []),
            priority=d.get("priority", 3),
            category=d.get("category", ""),
            complexity=d.get("complexity", "medium"),
            dependencies=d.get("dependencies", []),
            source=d.get("source", "extract"),
            status=status,
            metadata=d.get("metadata", {}),
        )


@dataclass
class IterationResult:
    iteration: int
    phase: str
    mode: str
    success: bool
    stories_generated: int = 0
    impl_status: str = ""
    raw_output: str = ""
    error: str = ""
    duration: float = 0.0
    cost_usd: float = 0.0
    story_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "iteration": self.iteration,
            "phase": self.phase,
            "mode": self.mode,
            "success": self.success,
            "stories_generated": self.stories_generated,
            "impl_status": self.impl_status,
            "error": self.error,
            "duration": self.duration,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }
        if self.story_id:
            d["story_id"] = self.story_id
        if self.session_id:
            d["session_id"] = self.session_id
        return d


@dataclass
class PhaseState:
    phase: str
    current_iteration: int = 0
    consecutive_empty: int = 0
    consecutive_errors: int = 0
    completed: bool = False
    completion_reason: str = ""
    total_cost_usd: float = 0.0
    last_error: str = ""
    last_summary: str = ""
    active_session_id: str = ""
    active_story_id: str = ""
    active_session_started: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "phase": self.phase,
            "current_iteration": self.current_iteration,
            "consecutive_empty": self.consecutive_empty,
            "consecutive_errors": self.consecutive_errors,
            "completed": self.completed,
            "completion_reason": self.completion_reason,
            "total_cost_usd": self.total_cost_usd,
            "last_error": self.last_error,
            "last_summary": self.last_summary,
        }
        if self.active_session_id:
            d["active_session_id"] = self.active_session_id
        if self.active_story_id:
            d["active_story_id"] = self.active_story_id
        if self.active_session_started:
            d["active_session_started"] = self.active_session_started
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PhaseState:
        return cls(
            phase=d["phase"],
            current_iteration=d.get("current_iteration", 0),
            consecutive_empty=d.get("consecutive_empty", 0),
            consecutive_errors=d.get("consecutive_errors", 0),
            completed=d.get("completed", False),
            completion_reason=d.get("completion_reason", ""),
            total_cost_usd=d.get("total_cost_usd", 0.0),
            last_error=d.get("last_error", ""),
            last_summary=d.get("last_summary", ""),
            active_session_id=d.get("active_session_id", ""),
            active_story_id=d.get("active_story_id", ""),
            active_session_started=d.get("active_session_started", ""),
        )
