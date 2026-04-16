"""Compound learning — capture reusable solutions after implementation."""
from __future__ import annotations

import re as _re
from dataclasses import dataclass
from datetime import datetime

import click

from pralph.assembler import assemble_compound_prompt, build_guardrails_system_prompt
from pralph.models import Story
from pralph.parser import parse_compound_output
from pralph.runner import COMPOUND_TOOLS, ClaudeResult, run_with_retry
from pralph.state import StateManager


def _token_kwargs(cr: ClaudeResult) -> dict:
    return {
        "input_tokens": cr.input_tokens,
        "output_tokens": cr.output_tokens,
        "cache_read_input_tokens": cr.cache_read_input_tokens,
        "cache_creation_input_tokens": cr.cache_creation_input_tokens,
    }


def _slugify(text: str) -> str:
    """Generate a filename-safe slug from text."""
    slug = text.lower().strip()
    slug = _re.sub(r"[^\w\s-]", "", slug)
    slug = _re.sub(r"[\s_]+", "-", slug)
    slug = _re.sub(r"-+", "-", slug)
    return slug[:80].strip("-")


@dataclass
class CompoundResult:
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def run_compound_capture(
    state: StateManager,
    story: Story,
    *,
    model: str,
    system_prompt: str,
    verbose: bool,
    dangerously_skip_permissions: bool,
    max_budget_usd: float | None,
    save_global: bool = False,
) -> CompoundResult:
    """Run compound learning capture after a successful implementation. Returns result with cost and tokens."""
    click.echo(click.style(f"  Capturing learnings: {story.id}", fg='magenta', bold=True))

    prompt = assemble_compound_prompt(state, story)
    result = run_with_retry(
        prompt,
        model=model,
        allowed_tools=COMPOUND_TOOLS,
        system_prompt=system_prompt,
        dangerously_skip_permissions=dangerously_skip_permissions,
        max_budget_usd=max_budget_usd,
        timeout=300,
        verbose=verbose,
        project_dir=str(state.project_dir),
    )

    if not result.success:
        click.echo(click.style(f"  Compound capture failed: {result.error[:120]}", fg='yellow'))
        return CompoundResult(cost_usd=result.cost_usd, **_token_kwargs(result))

    parsed = parse_compound_output(result.result)

    if not parsed["captured"]:
        click.echo(click.style(f"  Nothing notable: {parsed['reason'][:120]}", fg='yellow'))
        return CompoundResult(cost_usd=result.cost_usd, **_token_kwargs(result))

    solutions = parsed.get("solutions", [])
    for sol in solutions:
        title = sol.get("title", "Untitled")
        category = sol.get("category", "logic-errors")
        tags = sol.get("tags", [])
        error_sig = sol.get("error_signature", "")
        content = sol.get("content", "")

        if not content:
            # Build content from fields if not provided as full doc
            parts = [f"# {title}\n"]
            if sol.get("problem"):
                parts.append(f"## Problem\n\n{sol['problem']}\n")
            if error_sig:
                parts.append(f"## Error Signature\n\n`{error_sig}`\n")
            if sol.get("solution"):
                parts.append(f"## Solution\n\n{sol['solution']}\n")
            if sol.get("prevention"):
                parts.append(f"## Prevention\n\n{sol['prevention']}\n")
            if sol.get("related_files"):
                files = "\n".join(f"- {f}" for f in sol["related_files"])
                parts.append(f"## Related Files\n\n{files}\n")
            content = "\n".join(parts)

        category = _slugify(category) or "general"
        filename_slug = _slugify(title) + ".md"
        index_entry = {
            "filename": f"{category}/{filename_slug}",
            "category": category,
            "title": title,
            "tags": tags,
            "story_id": story.id,
            "created": datetime.now().isoformat(),
            "error_signature": error_sig,
        }

        path = state.save_solution(category, filename_slug, content, index_entry)
        click.echo(click.style(f"  + {title}", fg='green') + f" → {path}")

        # Optionally save to global domain-scoped store
        if save_global:
            global_paths = state.save_solution_global(category, filename_slug, content, index_entry)
            if global_paths:
                domains = state.detect_domains()
                click.echo(click.style(f"    ↑ global", fg='cyan') + f" [{', '.join(domains)}]")

    click.echo(click.style(f"  Captured {len(solutions)} solution(s)", fg='green', bold=True))
    return CompoundResult(cost_usd=result.cost_usd, **_token_kwargs(result))


def run_compound(
    state: StateManager,
    *,
    story_id: str | None = None,
    description: str = "",
    model: str = "sonnet",
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
    save_global: bool = False,
) -> float:
    """Standalone compound capture. Returns cost."""
    system_prompt = build_guardrails_system_prompt("implement", state)

    if story_id:
        stories = state.load_stories()
        story = next((s for s in stories if s.id == story_id), None)
        if not story:
            click.echo(f"Error: Story '{story_id}' not found")
            return 0.0
    else:
        # Create a synthetic story for ad-hoc capture
        story = Story(
            id="COMPOUND",
            title=description or "Ad-hoc compound capture",
            content=description,
        )

    cr = run_compound_capture(
        state, story,
        model=model,
        system_prompt=system_prompt,
        verbose=verbose,
        dangerously_skip_permissions=dangerously_skip_permissions,
        max_budget_usd=max_budget_usd,
        save_global=save_global,
    )
    return cr.cost_usd
