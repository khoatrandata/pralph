"""Export of the last justloop run's state, for other tools (``query --loop-state``).

Works with both storage backends. The shape is versioned: a consumer should
refuse a ``version`` it does not know.
"""
from __future__ import annotations

from typing import Any

from pralph import justloop_steps as steps

LOOP_STATE_VERSION = 1


def empty_loop_state(project_id: str | None = None) -> dict[str, Any]:
    return {"version": LOOP_STATE_VERSION, "project_id": project_id, "phase_state": None, "steps": []}


def build_loop_state(state) -> dict[str, Any]:
    """The last justloop run: its outcome, hand-off items and step records.

    ``phase_state`` is None when the project has no justloop run. A step's
    ``commit`` is the latest commit reported for it in this run's log rows.
    """
    out = empty_loop_state(state.project_id)
    ps = state.load_phase_state("justloop")
    if not ps.run_id and not ps.current_iteration and not ps.steps:
        return out

    out["phase_state"] = {
        "run_id": ps.run_id,
        "started_at": ps.started_at,
        "completed": ps.completed,
        "completion_reason": ps.completion_reason,
        "last_error": ps.last_error,
        "last_step": ps.last_step,
        "current_iteration": ps.current_iteration,
        "total_cost_usd": round(ps.total_cost_usd, 6),
        "needs_human": ps.needs_human,
    }

    commits = _latest_commits(state, ps.run_id) if ps.run_id else {}
    out["steps"] = [
        {
            "key": key,
            "step_number": rec.step_number,
            "text": rec.text or key,
            "attempts": rec.attempts,
            "last_status": rec.last_status,
            "last_blocker": rec.last_blocker,
            "review_findings": rec.review_findings,
            "commit": commits.get(key, ""),
        }
        for key, rec in ps.steps.items()
    ]
    return out


def _latest_commits(state, run_id: str) -> dict[str, str]:
    """Step key → the last non-empty commit reported in this run's log rows."""
    latest: dict[str, str] = {}
    for row in state.load_run_log():
        if row.get("phase") != "justloop" or row.get("run_id") != run_id or not row.get("commit"):
            continue
        key = steps.step_key(row.get("step") or "", row.get("step_number") or 0)
        latest[key or steps.UNNAMED_STEP] = row["commit"]
    return latest
