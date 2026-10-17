"""Permission denials and other hand-off items for justloop ("Needs a person").

Denials come from two places in a ``claude -p`` stream: the result event's
``permission_denials`` list, and error tool results whose text is a
permission refusal. Both are matched to the tool call by ``tool_use_id``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_REFUSAL_RE = re.compile(
    r"requested permissions? to|haven't granted|has not been granted|requires approval"
    r"|permission to use .* (?:was|has been) denied|denied by (?:the )?(?:user|permission)",
    re.IGNORECASE,
)
_TARGET_KEYS = ("file_path", "notebook_path", "path", "command", "url", "pattern", "query")
_MAX_TARGET = 80


def is_refusal(text: str) -> bool:
    """True when a tool error reads like a permission refusal."""
    return bool(_REFUSAL_RE.search(text or ""))


def short_target(tool: str, tool_input: Any, project_dir: str = "") -> str:
    """A short form of what the tool call was aimed at.

    File paths inside the project are made relative; shell commands keep
    their first three words.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in _TARGET_KEYS:
        val = tool_input.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        val = val.strip()
        if key == "command":
            val = " ".join(val.split()[:3])
        elif key in ("file_path", "notebook_path", "path") and project_dir:
            try:
                rel = os.path.relpath(val, project_dir)
                if not rel.startswith(".."):
                    val = rel
            except ValueError:
                pass
        return val if len(val) <= _MAX_TARGET else val[: _MAX_TARGET - 3] + "..."
    return ""


class DenialCollector:
    """Collects permission denials while a stream is read.

    Feed every stream event to ``feed``; ``denials()`` returns one item per
    refused tool call: ``{"tool", "tool_input", "reason", "tool_use_id"}``.
    """

    def __init__(self) -> None:
        self._calls: dict[str, tuple[str, Any]] = {}
        self._items: dict[str, dict[str, Any]] = {}

    def feed(self, event: dict) -> None:
        etype = event.get("type")
        msg = event.get("message")
        if etype == "assistant" and isinstance(msg, dict):
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    self._calls[block["id"]] = (block.get("name", ""), block.get("input", {}))
        elif etype == "user" and isinstance(msg, dict):
            for block in msg.get("content") or []:
                if not (isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error")):
                    continue
                text = _content_text(block.get("content"))
                if is_refusal(text):
                    tid = block.get("tool_use_id", "")
                    tool, inp = self._calls.get(tid, ("", {}))
                    self._add(tid, tool, inp, text)
        elif etype == "result":
            for d in event.get("permission_denials") or []:
                if isinstance(d, dict):
                    self._add(d.get("tool_use_id", ""), d.get("tool_name", ""), d.get("tool_input", {}), "")

    def _add(self, tid: str, tool: str, inp: Any, reason: str) -> None:
        key = tid or f"{tool}:{inp!r}"
        item = self._items.setdefault(key, {"tool": "", "tool_input": {}, "reason": "", "tool_use_id": tid})
        item["tool"] = item["tool"] or tool
        item["tool_input"] = item["tool_input"] or inp
        item["reason"] = item["reason"] or " ".join(reason.split())[:300]

    def denials(self) -> list[dict[str, Any]]:
        return list(self._items.values())


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def denial_items(denials: list[dict], step: str, step_number: int, project_dir: str = "") -> list[dict]:
    """Needs-a-person items for this iteration's tool denials."""
    return [
        {
            "source": "denied",
            "step": step,
            "step_number": step_number,
            "tool": d.get("tool", "") or "?",
            "target": short_target(d.get("tool", ""), d.get("tool_input"), project_dir),
            "reason": _relative(d.get("reason", ""), project_dir) or "permission denied",
        }
        for d in denials
    ]


def _relative(text: str, project_dir: str) -> str:
    """Shorten absolute project paths in a refusal message."""
    if project_dir:
        text = text.replace(project_dir.rstrip("/") + "/", "")
    return text


def model_items(needs: list[str], step: str, step_number: int, project_dir: str = "") -> list[dict]:
    """Needs-a-person items the model reported in its typed result."""
    return [
        {"source": "model", "step": step, "step_number": step_number,
         "tool": "", "target": "", "reason": _relative(" ".join(n.split()), project_dir)[:300]}
        for n in needs if n.strip()
    ]


def _dedupe_key(item: dict) -> tuple[str, str]:
    if item.get("tool") or item.get("target"):
        return item.get("tool", ""), item.get("target", "")
    return "", item.get("reason", "").lower()


def merge_items(existing: list[dict], new: list[dict]) -> list[dict]:
    """Append ``new`` items, dropping ones whose (tool, target) is already listed.

    Items without a tool or target (model-reported) are matched by reason,
    and dropped when they name the target of a refused call already listed,
    since that refusal is recorded from the stream.
    """
    seen = {_dedupe_key(i) for i in existing}
    merged = list(existing)
    for item in new:
        key = _dedupe_key(item)
        if key in seen or _restates_denial(item, merged):
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _restates_denial(item: dict, items: list[dict]) -> bool:
    if item.get("source") != "model":
        return False
    reason = item.get("reason", "")
    return any(
        i.get("source") == "denied" and len(i.get("target", "")) >= 3 and i["target"] in reason
        for i in items
    )


def _step_label(item: dict) -> str:
    if item.get("step_number"):
        return f"step {item['step_number']}"
    step = item.get("step", "")
    if not step:
        return "no step"
    return f"step '{step if len(step) <= 60 else step[:57] + '...'}'"


def item_line(item: dict) -> str:
    """One checklist line, e.g. ``Write .claude/settings.json (step 3): reason``."""
    label = _step_label(item)
    if item.get("source") == "denied":
        what = " ".join(p for p in (item.get("tool", ""), item.get("target", "")) if p)
        return f"{what} was refused ({label}): {item.get('reason', '')}"
    return f"{item.get('reason', '')} ({label})"


def render_checklist(items: list[dict]) -> str:
    """Markdown checklist for ``.pralph/needs-human.md``."""
    lines = ["# Needs a person", ""]
    lines.append("Actions the last justloop run could not take. Do them, then re-run.")
    lines.append("")
    lines += [f"- [ ] {item_line(i)}" for i in items]
    return "\n".join(lines) + "\n"


def write_checklist(path: str | Path, items: list[dict]) -> bool:
    """Write the checklist, or remove a stale one when there is nothing to hand off.

    Returns True when a file was written.
    """
    path = Path(path)
    if not items:
        if path.exists():
            path.unlink()
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_checklist(items))
    return True
