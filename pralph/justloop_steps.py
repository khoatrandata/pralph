"""justloop step attempts and the stop rules enforced in code.

Each typed iteration result updates a per-run StepRecord for the step it
names. Stops: the same blocker twice on one step (``repeated_blocker``), and
too many unfinished attempts on one step (``step_attempts_exhausted``).
Attempts count per run; a blocker from the previous run is carried over for
one retry.
"""
from __future__ import annotations

import re

from pralph import plan_file as planfile
from pralph.models import IterationResult, PhaseState, StepRecord

DEFAULT_MAX_STEP_ATTEMPTS = 3

_TMP_ROOTS = ("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")
_PATH_RE = re.compile(r"(?:~|\.{1,2})?(?:/[\w.@+-]+)+/?|[\w.@+-]+(?:/[\w.@+-]+)+")
# Hex runs of 7+ chars that mix letters and digits (shas, ids), optional 0x
_HEX_RE = re.compile(r"\b(?:0x)?(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,}\b")
_DIGITS_RE = re.compile(r"\d+")


def _trim_path(m: re.Match) -> str:
    path = m.group(0)
    parts = [p for p in path.split("/") if p and p not in ("~", ".", "..")]
    if not parts:
        return path
    if path.startswith(_TMP_ROOTS):
        # Temp dirs have random names; keep only the leaf
        return "<tmp>/" + parts[-1]
    return "/".join(parts[-2:])


def blocker_signature(text: str) -> str:
    """Normalise blocker text so the same failure matches across attempts.

    Lowercases, trims paths to their last two components (temp-dir paths to
    ``<tmp>/<leaf>``), replaces hex runs with ``<hex>`` and digits with
    ``<n>``, and collapses whitespace.
    """
    s = text.strip().lower()
    s = _PATH_RE.sub(_trim_path, s)
    s = _HEX_RE.sub("<hex>", s)
    s = _DIGITS_RE.sub("<n>", s)
    return " ".join(s.split())


def step_key(step: str) -> str:
    """Key for a step record: the step text without indent and checkbox."""
    return planfile.step_text(step).strip()


def record_step_result(ps: PhaseState, result: IterationResult, max_attempts: int) -> str:
    """Update the step record from a typed result. Returns a stop reason or ''.

    Results without a typed status or without a step are not tracked.
    """
    key = step_key(result.step)
    if not result.status or not key:
        return ""
    rec = ps.steps.setdefault(key, StepRecord())
    ps.last_step = key
    rec.attempts += 1
    rec.last_status = result.status
    rec.last_blocker = result.blocker if result.status == "blocked" else ""

    if result.status in ("done", "nothing_left"):
        return ""

    if result.status == "blocked" and result.blocker:
        sig = blocker_signature(result.blocker)
        if sig in rec.signatures or (rec.prior_signature and sig == rec.prior_signature):
            return "repeated_blocker"
        rec.signatures.append(sig)

    if rec.attempts >= max_attempts:
        return "step_attempts_exhausted"
    return ""


def stop_message(ps: PhaseState, reason: str, plan_content: str | None = None) -> str:
    """Human-readable explanation of a step stop, naming the step and blocker."""
    rec = ps.steps.get(ps.last_step, StepRecord())
    label = _short_step_label(ps.last_step, plan_content)
    if reason == "repeated_blocker":
        return f"{label} hit the same blocker twice: {rec.last_blocker}"
    if reason == "step_attempts_exhausted":
        msg = f"{label} not finished after {rec.attempts} attempts (last status: {rec.last_status})"
        if rec.last_blocker:
            msg += f": {rec.last_blocker}"
        return msg
    return reason


def carried_blocker(prev: PhaseState | None) -> tuple[str, str] | None:
    """The (step, blocker) the previous run ended blocked on, if any."""
    if prev is None or not prev.last_step:
        return None
    rec = prev.steps.get(prev.last_step)
    if rec is None or rec.last_status != "blocked" or not rec.last_blocker:
        return None
    return prev.last_step, rec.last_blocker


def seed_carried_blocker(ps: PhaseState, step: str, blocker: str) -> None:
    """Give a fresh run's step record the previous run's blocker (one retry)."""
    rec = ps.steps.setdefault(step_key(step), StepRecord())
    rec.prior_blocker = blocker
    rec.prior_signature = blocker_signature(blocker)


def _short_step_label(key: str, plan_content: str | None) -> str:
    """'step 3' when the plan file has the step, else the step text cut to 60 chars."""
    if plan_content is not None:
        found = planfile.find_step(plan_content, key)
        if found:
            return f"step {found.number}"
    text = key if len(key) <= 60 else key[:57] + "..."
    return f"step '{text}'"


def _step_label(key: str, plan_content: str | None) -> str:
    if plan_content is not None:
        found = planfile.find_step(plan_content, key)
        if found:
            return f"step {found.number} ({key})"
    return f"step '{key}'"


def _prior_line(label: str, rec: StepRecord) -> str:
    return (f"The previous run stopped on {label} with: {rec.prior_blocker}. "
            "Try it once; if it fails the same way, report blocked.")


def build_step_context(
    ps: PhaseState, max_attempts: int, plan_content: str | None = None,
) -> tuple[str, str]:
    """Context for the next iteration: (prompt text, short header label).

    With a plan file, names the next unchecked step, its attempt number and
    what happened last time. Without one, reports the last tracked step if it
    was left blocked or in progress.
    """
    lines: list[str] = []
    header = ""

    if plan_content is not None:
        nxt = planfile.next_step(plan_content)
        if nxt is None:
            return "", ""
        rec = ps.steps.get(step_key(nxt.line), StepRecord())
        attempt = rec.attempts + 1
        header = f"step {nxt.number}, attempt {attempt}/{max_attempts}"
        lines.append(f"Next step: step {nxt.number}, attempt {attempt} of {max_attempts}.")
        lines.append(f"Step line: {nxt.line.strip()}")
        if rec.last_status == "blocked":
            lines.append(f"Last attempt was blocked: {rec.last_blocker}")
        elif rec.last_status == "in_progress":
            lines.append("Last attempt ended in progress; continue it.")
        if rec.prior_blocker and rec.attempts == 0:
            lines.append(_prior_line(f"step {nxt.number}", rec))
        return "\n".join(lines), header

    rec = ps.steps.get(ps.last_step) if ps.last_step else None
    if rec and rec.last_status in ("blocked", "in_progress"):
        header = f"{ps.last_step[:40]}, attempt {rec.attempts + 1}/{max_attempts}"
        line = f"The last iteration worked on {_step_label(ps.last_step, None)} (attempt {rec.attempts}) and reported {rec.last_status}"
        lines.append(line + (f": {rec.last_blocker}" if rec.last_blocker else "."))
    for key, r in ps.steps.items():
        if r.prior_blocker and r.attempts == 0:
            lines.append(_prior_line(_step_label(key, None), r))
    return "\n".join(lines), header
