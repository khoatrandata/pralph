"""Fresh-process review of each justloop step (--review-cmd / --review fresh).

A step the model reports ``done`` is reviewed before pralph ticks it. A clean
review lets the tick happen; findings keep the step open and go into the next
prompt; a review that cannot run stops the loop. There is no auto-approve.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass

import click

from pralph import plan_file as planfile
from pralph.prompts.justloop import JUSTLOOP_REVIEW_PROMPT

MAX_FINDINGS_CHARS = 4000
MAX_DIFF_CHARS = 100_000
REVIEW_TIMEOUT = 1800
FRESH_REVIEW_TOOLS = "Read,Glob,Grep,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"

FRESH_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "clean": {"type": "boolean"},
        "findings": {"type": "string"},
    },
    "required": ["clean", "findings"],
    "additionalProperties": False,
}


@dataclass
class ReviewOutcome:
    verdict: str          # "clean", "findings" or "error"
    findings: str = ""    # findings text (capped), or the error for "error"
    cost_usd: float = 0.0


def _cap(text: str, limit: int = MAX_FINDINGS_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n[... findings truncated by pralph]"


def _git(project_dir: str, *args: str) -> str:
    """Run git and return stripped stdout, or '' on failure."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=project_dir, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def resolve_commit(project_dir: str, ref: str) -> str:
    """Full sha for ``ref`` (a sha or 'HEAD'), or '' when it is not a commit."""
    if not ref:
        return ""
    return _git(project_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def run_review_cmd(
    cmd: str, project_dir: str, *, step: str, step_number: int, commit: str, base: str,
    timeout: int = REVIEW_TIMEOUT,
) -> ReviewOutcome:
    """Run a user review command. Exit 0 is clean; exit 1 is findings.

    Any other exit (the review did not complete, a limit stopped it, the
    command was not found), a signal or a timeout is an error.
    """
    env = dict(os.environ)
    env.update({
        "PRALPH_STEP": step,
        "PRALPH_STEP_NUMBER": str(step_number or ""),
        "PRALPH_COMMIT": commit,
        "PRALPH_BASE": base,
    })
    try:
        # Own process group, so a timeout also kills the command's children
        proc = subprocess.Popen(
            cmd, shell=True, cwd=project_dir, env=env, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError as e:
        return ReviewOutcome("error", f"review command failed to start: {e}")
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return ReviewOutcome("error", f"review command timed out after {timeout}s")

    if proc.returncode == 0:
        return ReviewOutcome("clean")
    stderr_tail = (stderr or "").strip()[-500:]
    if proc.returncode != 1:
        detail = f": {stderr_tail}" if stderr_tail else ""
        return ReviewOutcome("error", f"review command exited {proc.returncode}{detail}")
    findings = (stdout or "").strip() or stderr_tail
    if not findings:
        findings = f"review command exited {proc.returncode} with no output"
    return ReviewOutcome("findings", _cap(findings))


def run_fresh_review(
    project_dir: str, *, step: str, commit: str, base: str, model: str,
    timeout: int = REVIEW_TIMEOUT, verbose: bool = False,
    max_budget_usd: float | None = None,
) -> ReviewOutcome:
    """Review the step's diff with ``claude -p`` in a new read-only session."""
    from pralph.runner import run_with_retry

    rng = f"{base}..{commit}" if base else commit
    diff = _git(project_dir, "diff", rng) if base else _git(project_dir, "show", commit)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n[... diff truncated; run `git diff {rng}` for the rest]"
    prompt = (JUSTLOOP_REVIEW_PROMPT
              .replace("{{step}}", step or "(the last unit of work)")
              .replace("{{base}}", base[:12] or "(root)")
              .replace("{{commit}}", commit[:12])
              .replace("{{diff}}", diff or "(empty diff)"))
    result = run_with_retry(
        prompt,
        model=model,
        allowed_tools=FRESH_REVIEW_TOOLS,
        timeout=timeout,
        verbose=verbose,
        project_dir=project_dir,
        max_budget_usd=max_budget_usd,
        json_schema=json.dumps(FRESH_REVIEW_SCHEMA),
    )
    if not result.success:
        return ReviewOutcome("error", f"fresh review failed: {result.error}", result.cost_usd)
    out = result.structured_output
    if not isinstance(out, dict) or not isinstance(out.get("clean"), bool):
        return ReviewOutcome("error", "fresh review returned no verdict", result.cost_usd)
    if out["clean"]:
        return ReviewOutcome("clean", cost_usd=result.cost_usd)
    findings = str(out.get("findings") or "").strip() or "the reviewer reported problems without details"
    return ReviewOutcome("findings", _cap(findings), result.cost_usd)


class StepReviewer:
    """Decides when to review and keeps the review base for one run.

    ``base`` is the last reviewed commit (HEAD at the start of the run), so a
    review after findings covers the step and its fixes together. With
    ``every`` > 1 only every Nth done step is reviewed (the plan's last step
    always is), and the review covers all steps since the last one.
    """

    def __init__(
        self, project_dir: str, *, cmd: str = "", fresh: bool = False, every: int = 1,
        model: str = "sonnet", verbose: bool = False, max_budget_usd: float | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.cmd = cmd
        self.fresh = fresh
        self.every = max(1, every)
        self.model = model
        self.verbose = verbose
        self.max_budget_usd = max_budget_usd
        self.base = resolve_commit(project_dir, "HEAD")
        self.pending = 0  # done steps since the last clean review

    def review(self, step: str, step_number: int, commit: str, is_last: bool) -> ReviewOutcome | None:
        """Review a done step. None means no review was due (the step may be ticked)."""
        resolved = resolve_commit(self.project_dir, commit) or resolve_commit(self.project_dir, "HEAD")
        if not resolved:
            return ReviewOutcome("error", f"cannot resolve commit {commit or 'HEAD'} (is this a git repository?)")
        if resolved == self.base:
            click.echo(click.style("  review: skipped (no new commits since the last review)", dim=True))
            return None
        self.pending += 1
        if self.pending < self.every and not is_last:
            click.echo(click.style(f"  review: deferred ({self.pending}/{self.every} steps)", dim=True))
            return None

        label = f"step {step_number}" if step_number else (planfile.step_text(step) or "the last unit of work")
        click.echo(click.style(f"  review: {label} ({self.base[:10] or 'root'}..{resolved[:10]})", fg="cyan"))
        if self.cmd:
            outcome = run_review_cmd(
                self.cmd, self.project_dir, step=step, step_number=step_number,
                commit=resolved, base=self.base,
            )
        else:
            outcome = run_fresh_review(
                self.project_dir, step=step, commit=resolved, base=self.base, model=self.model,
                verbose=self.verbose, max_budget_usd=self.max_budget_usd,
            )
        if outcome.verdict == "clean":
            self.base, self.pending = resolved, 0
            click.echo(click.style("  review: clean", fg="green"))
        elif outcome.verdict == "findings":
            click.echo(click.style("  review: findings — step stays open", fg="yellow"))
            for line in outcome.findings.splitlines()[:10]:
                click.echo(click.style(f"    {line[:200]}", fg="yellow"))
        else:
            click.echo(click.style(f"  review: error — {outcome.findings[:300]}", fg="red"))
        return outcome
