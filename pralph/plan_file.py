"""Plan-file management for justloop --plan-file.

pralph owns the checklist and the progress notes: it ticks the step the model
reports as done and appends the model's note. The model no longer edits either.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_CHECKBOX_RE = re.compile(r"^(?P<indent>\s*)(?P<bullet>[-*+])\s+\[(?P<mark>[ xX])\]\s+(?P<text>.*?)\s*$")
_NOTES_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s*progress\s+notes\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s")


class PlanFileError(Exception):
    """A plan-file operation could not be applied (e.g. the step line is missing)."""


@dataclass
class PlanStep:
    number: int      # 1-based position among top-level checklist lines
    line_index: int  # 0-based line index in the file
    line: str        # the line as written, without the trailing newline
    text: str        # the step text after the checkbox
    checked: bool


def step_text(line: str) -> str:
    """Return a checklist line's text without indent, bullet and checkbox."""
    m = _CHECKBOX_RE.match(line)
    return m.group("text") if m else line.strip()


def list_steps(content: str) -> list[PlanStep]:
    """Top-level checklist lines in order; indented sub-bullets are not steps."""
    matches = []
    for idx, line in enumerate(content.splitlines()):
        m = _CHECKBOX_RE.match(line)
        if m:
            matches.append((idx, line, m))
    if not matches:
        return []
    top = min(len(m.group("indent")) for _, _, m in matches)
    steps = []
    for idx, line, m in matches:
        if len(m.group("indent")) != top:
            continue
        steps.append(PlanStep(
            number=len(steps) + 1, line_index=idx, line=line,
            text=m.group("text"), checked=m.group("mark") != " ",
        ))
    return steps


def next_step(content: str) -> PlanStep | None:
    """The first unchecked top-level step, or None when all are ticked."""
    return next((s for s in list_steps(content) if not s.checked), None)


def find_step(content: str, step: str) -> PlanStep | None:
    """Find the top-level step matching a reported step line.

    Steps can share text, so the first unchecked match wins; a checked match
    is returned only when no unchecked one exists.
    """
    want = step_text(step)
    matches = [s for s in list_steps(content) if s.text == want]
    return next((s for s in matches if not s.checked), matches[0] if matches else None)


def step_at_line(content: str, line_index: int) -> PlanStep | None:
    """The top-level step on a 0-based line, or None (e.g. a sub-bullet)."""
    return next((s for s in list_steps(content) if s.line_index == line_index), None)


def tick_line(content: str, step: str) -> tuple[str, int]:
    """Tick exactly one unchecked checklist line matching ``step``.

    The step is compared by its text after the checkbox, so "- [ ] X" and
    "X" both match "- [ ] X" but not "- [ ] X and Y". When several unchecked
    lines match, a line equal to ``step`` as written (indent included) wins,
    then the first one in file order. Returns the new content and the 0-based
    line index. Raises PlanFileError when no unchecked line matches.
    """
    want = step_text(step)
    if not want:
        raise PlanFileError("empty step")
    lines = content.splitlines(keepends=True)
    candidates = []
    for idx, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        m = _CHECKBOX_RE.match(line)
        if m and m.group("mark") == " " and m.group("text") == want:
            candidates.append(idx)
    if not candidates:
        raise PlanFileError(f"step not found in plan file: {step.strip()}")
    exact = [i for i in candidates if lines[i].rstrip("\r\n").rstrip() == step.rstrip()]
    idx = (exact or candidates)[0]
    lines[idx] = lines[idx].replace("[ ]", "[x]", 1)
    return "".join(lines), idx


def add_note(content: str, note: str) -> str:
    """Append ``- note`` at the end of the Progress notes section.

    The section is found by heading (case-insensitive). Without one, a
    "## Progress notes" section is added at the end of the file.
    """
    entry = f"- {' '.join(note.split())}\n"
    lines = content.splitlines(keepends=True)
    start = level = None
    for idx, raw in enumerate(lines):
        m = _NOTES_HEADING_RE.match(raw)
        if m:
            start, level = idx, len(m.group("hashes"))
            break
    if start is None:
        sep = "" if not content or content.endswith("\n") else "\n"
        return f"{content}{sep}\n## Progress notes\n\n{entry}"

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        m = _HEADING_RE.match(lines[idx])
        if m and len(m.group("hashes")) <= level:
            end = idx
            break
    # Insert after the section's last non-blank line
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += "\n"
    if insert_at == start + 1:
        entry = "\n" + entry
    # Keep a blank line before a heading that directly follows the notes
    trailer = "\n" if insert_at < len(lines) and lines[insert_at].strip() else ""
    lines.insert(insert_at, entry + trailer)
    return "".join(lines)


def _write_atomic(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_plan(path: str | Path) -> str:
    return Path(path).read_text()


def tick_step(path: str | Path, step: str) -> int:
    """Tick ``step`` in the plan file. Returns its 0-based line index."""
    path = Path(path)
    content, idx = tick_line(path.read_text(), step)
    _write_atomic(path, content)
    return idx


def append_note(path: str | Path, note: str) -> None:
    """Append a progress note to the plan file. Empty notes are ignored."""
    if not note.strip():
        return
    path = Path(path)
    _write_atomic(path, add_note(path.read_text(), note))
