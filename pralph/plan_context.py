"""Focused plan context for justloop --plan-file, and per-step model tags.

Each iteration gets the plan's Rules section, the current step with its
sub-bullets, and the progress notes relevant to that step, instead of the
whole file. A file whose layout is not recognised is sent whole.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pralph import plan_file as planfile

NOTES_CHAR_CAP = 3000
RECENT_NOTES = 2

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.*?)\s*#*\s*$")
_RULES_TITLE_RE = re.compile(r"^rules?\b", re.IGNORECASE)
_NOTES_TITLE_RE = re.compile(r"^progress\s+notes\b", re.IGNORECASE)
_BULLET_RE = re.compile(r"^(?P<indent>\s*)[-*+]\s+")

# Tokens that tie a note to a step: backticked spans, file paths, ticket keys,
# and identifiers that cannot be ordinary words (snake_case, dotted, CamelCase).
_TOKEN_RES = (
    re.compile(r"`([^`\s][^`]*?)`"),
    re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+|[\w-]+\.(?:py|sql|md|json|ya?ml|toml|sh|js|ts|tsx|html|txt|cfg|ini))(?![\w/-])"),
    re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b"),
    re.compile(r"\b([A-Za-z]\w*_\w+|[a-z]+[A-Z]\w*|[A-Z][a-z]+[A-Z]\w*)\b"),
)

# --model values: CLI aliases, or a full model name
MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku", "opusplan", "default")
_FULL_MODEL_RE = re.compile(r"^claude-[a-z0-9.-]+$")
_MODEL_TAG_RE = re.compile(r"\s*\(model:\s*(?P<model>[^)\s]*)\s*\)\s*$", re.IGNORECASE)


@dataclass
class Note:
    number: int  # 1-based position in the Progress notes section
    text: str


@dataclass
class PlanParts:
    rules: str = ""
    notes: list[Note] = field(default_factory=list)
    recognised: bool = False


@dataclass
class PlanContext:
    text: str
    focused: bool
    note_ids: list[int] = field(default_factory=list)
    chars: int = 0

    def summary(self) -> dict:
        """What was sent, for the iteration record."""
        return {"focused": self.focused, "notes": self.note_ids, "chars": self.chars}


def _sections(lines: list[str]) -> list[tuple[str, int, int, int]]:
    """(title, level, start, end) for each heading; ``start`` is the heading line."""
    heads = []
    for idx, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            heads.append((m.group("title"), len(m.group("hashes")), idx))
    out = []
    for i, (title, level, start) in enumerate(heads):
        end = len(lines)
        for _, lvl, nxt in heads[i + 1:]:
            if lvl <= level:
                end = nxt
                break
        out.append((title, level, start, end))
    return out


def parse_plan(content: str) -> PlanParts:
    """Split a plan into its Rules text and Progress notes.

    The layout is recognised when the file has at least one top-level
    checklist step. Rules and notes are optional and found by heading,
    case-insensitively. Each top-level bullet under Progress notes is one
    note, with any indented continuation lines.
    """
    parts = PlanParts(recognised=bool(planfile.list_steps(content)))
    if not parts.recognised:
        return parts
    lines = content.splitlines()
    for title, _level, start, end in _sections(lines):
        if not parts.rules and _RULES_TITLE_RE.match(title):
            parts.rules = "\n".join(lines[start + 1:end]).strip()
        elif not parts.notes and _NOTES_TITLE_RE.match(title):
            parts.notes = _parse_notes(lines[start + 1:end])
    return parts


def _parse_notes(lines: list[str]) -> list[Note]:
    notes: list[list[str]] = []
    top = None
    for line in lines:
        if _HEADING_RE.match(line):
            break
        m = _BULLET_RE.match(line)
        if m and (top is None or len(m.group("indent")) <= top):
            top = len(m.group("indent"))
            notes.append([line[m.end():].strip()])
        elif notes and line.strip():
            notes[-1].append(line.strip())
    return [Note(i + 1, " ".join(n)) for i, n in enumerate(notes)]


def step_block(content: str, step: planfile.PlanStep) -> str:
    """The step line plus the indented lines under it (sub-bullets, details)."""
    lines = content.splitlines()
    indent = len(step.line) - len(step.line.lstrip())
    block = [step.line.strip()]
    for line in lines[step.line_index + 1:]:
        if not line.strip():
            block.append("")
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line.rstrip())
    while block and not block[-1]:
        block.pop()
    return "\n".join(block)


def step_tokens(text: str) -> set[str]:
    """Paths, ticket keys and identifiers in a step's text."""
    tokens: set[str] = set()
    for rx in _TOKEN_RES:
        for m in rx.finditer(text):
            tok = m.group(1).strip().strip(".,;:")
            if len(tok) >= 3:
                tokens.add(tok)
    return tokens


def _mentions(note: str, token: str) -> bool:
    return re.search(rf"(?<![\w/.-]){re.escape(token)}(?![\w/-]|\.\w)", note) is not None


def select_notes(notes: list[Note], step_text: str, cap: int = NOTES_CHAR_CAP) -> list[Note]:
    """The last two notes, plus earlier notes that share a token with the step.

    Newest first, within ``cap`` characters; a note that would overflow the
    cap is cut short when it is one of the last two, and skipped otherwise.
    """
    tokens = step_tokens(step_text)
    recent = notes[-RECENT_NOTES:]
    earlier = [n for n in notes[:-RECENT_NOTES] if any(_mentions(n.text, t) for t in tokens)]
    chosen: list[Note] = []
    used = 0
    for note in list(reversed(recent)) + list(reversed(earlier)):
        room = cap - used
        if len(note.text) <= room:
            chosen.append(note)
            used += len(note.text)
        elif note in recent and room > 80:
            chosen.append(Note(note.number, note.text[: room - 3] + "..."))
            used = cap
    return chosen


def build_plan_context(
    content: str, step: planfile.PlanStep | None, plan_display: str, record: str = "",
) -> PlanContext:
    """The plan part of an iteration prompt.

    In order: the Rules section, the current step with its sub-bullets, the
    step's record (``record``), and the notes relevant to the step. An
    unrecognised layout sends the whole file instead.
    """
    parts = parse_plan(content)
    if not parts.recognised or step is None:
        text = f"### Plan file `{plan_display}` (full)\n\n{content.strip()}"
        if record:
            text += f"\n\n### This step so far\n\n{record}"
        return PlanContext(text=text, focused=False, chars=len(text))

    sections = []
    if parts.rules:
        sections.append(f"### Rules\n\n{parts.rules}")
    sections.append(f"### Current step (step {step.number})\n\n{step_block(content, step)}")
    if record:
        sections.append(f"### This step so far\n\n{record}")
    notes = select_notes(parts.notes, step_block(content, step))
    if notes:
        body = "\n".join(f"- (note {n.number}) {n.text}" for n in notes)
        sections.append(f"### Relevant progress notes (newest first)\n\n{body}")
    sections.append(
        f"The full plan is in `{plan_display}`. Read it only when this step refers to "
        "earlier work that is not shown here."
    )
    text = "\n\n".join(sections)
    return PlanContext(text=text, focused=True, note_ids=[n.number for n in notes], chars=len(text))


# ── model tags ────────────────────────────────────────────────────────


def model_tag(step_text: str) -> str:
    """The model in a trailing ``(model: X)`` tag, lower-cased, or ''."""
    m = _MODEL_TAG_RE.search(step_text)
    return m.group("model").lower() if m else ""


def valid_model(name: str) -> bool:
    return name in MODEL_ALIASES or bool(_FULL_MODEL_RE.match(name))
