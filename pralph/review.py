"""Post-implementation review subsystem."""
from __future__ import annotations

from dataclasses import dataclass

import click

from pralph.assembler import assemble_review_prompt
from pralph.models import Story
from pralph.parser import parse_review_output
from pralph.runner import REVIEW_TOOLS, run_with_retry
from pralph.state import StateManager


@dataclass
class ReviewResult:
    approved: bool
    feedback: str
    issues: list
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def run_review(
    state: StateManager,
    story: Story,
    *,
    model: str,
    system_prompt: str,
    verbose: bool,
    dangerously_skip_permissions: bool,
    max_budget_usd: float | None,
) -> ReviewResult | None:
    """Run a review on a freshly implemented story. Returns None on review error."""
    click.echo(click.style(f"  🔍 Reviewing: {story.id}", fg='magenta', bold=True))

    review_prompt = assemble_review_prompt(state, story)
    result = run_with_retry(
        review_prompt,
        model=model,
        allowed_tools=REVIEW_TOOLS,
        system_prompt=system_prompt,
        dangerously_skip_permissions=dangerously_skip_permissions,
        max_budget_usd=max_budget_usd,
        timeout=600,
        verbose=verbose,
        project_dir=str(state.project_dir),
    )

    if not result.success:
        click.echo(click.style(f"  ⚠ Review failed (error): {result.error[:120]}", fg='yellow'))
        click.echo(click.style("  → Auto-approving due to review error", fg='yellow'))
        return None

    parsed = parse_review_output(result.result)
    approved = parsed["approved"]
    feedback = parsed["feedback"]
    issues = parsed.get("issues", [])

    if approved:
        state.clear_review_feedback(story.id)
        click.echo(click.style(f"  ✓ Review approved", fg='green', bold=True) + f" — {feedback[:120]}")
    else:
        # Build feedback text for the rework file
        feedback_lines = [f"# Review Feedback for {story.id}\n", f"**Summary:** {feedback}\n"]
        for issue in issues:
            sev = issue.get("severity", "?")
            desc = issue.get("description", "")
            feedback_lines.append(f"- **[{sev}]** {desc}")
        feedback_text = "\n".join(feedback_lines)
        state.write_review_feedback(story.id, feedback_text)
        click.echo(click.style(f"  ✗ Review rejected", fg='red', bold=True) + f" — {feedback[:120]}")
        for issue in issues:
            sev = issue.get("severity", "?")
            desc = issue.get("description", "")
            color = 'red' if sev in ("critical", "major") else 'yellow'
            click.echo(f"    {click.style(f'[{sev}]', fg=color)} {desc[:100]}")

    return ReviewResult(
        approved=approved,
        feedback=feedback,
        issues=issues,
        cost_usd=result.cost_usd,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_input_tokens=result.cache_read_input_tokens,
        cache_creation_input_tokens=result.cache_creation_input_tokens,
    )
