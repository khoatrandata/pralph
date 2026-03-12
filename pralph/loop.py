from __future__ import annotations

import json
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import click

from pralph.assembler import (
    assemble_add_prompt,
    assemble_compound_prompt,
    assemble_ideate_prompt,
    assemble_implement_prompt,
    assemble_phase1_analyze_prompt,
    assemble_plan_prompt,
    assemble_refine_prompt,
    assemble_review_prompt,
    assemble_stories_prompt,
    build_guardrails_system_prompt,
)
from pralph.models import IterationResult, PhaseState, Story, StoryStatus
from pralph.parser import (
    detect_completion_signal,
    detect_ideation_complete,
    extract_json_from_text,
    parse_compound_output,
    parse_implement_output,
    parse_plan_output,
    parse_review_output,
    parse_stories_output,
)
from pralph.runner import (
    ADD_TOOLS,
    COMPOUND_TOOLS,
    IDEATE_TOOLS,
    IMPLEMENT_TOOLS,
    PLAN_TOOLS,
    REFINE_TOOLS,
    REVIEW_TOOLS,
    STORIES_TOOLS_EXTRACT,
    STORIES_TOOLS_RESEARCH,
    ClaudeResult,
    resume_interactive,
    ProcessGroup,
    handle_parallel_interrupt,
    run_with_retry,
    run_with_retry_parallel,
)
from pralph.state import StateManager

# Foundation categories to prioritize in implementation
FOUNDATION_CATEGORIES = frozenset({
    "FND", "DBM", "SEC", "ARC", "ADM", "DAT", "DEP", "SYS", "INF",
    "INFRA", "ARCH", "DB", "AUTH", "SETUP", "FOUNDATION",
})


def _token_kwargs(cr: ClaudeResult) -> dict:
    """Extract token fields from a ClaudeResult as keyword arguments for IterationResult."""
    return {
        "input_tokens": cr.input_tokens,
        "output_tokens": cr.output_tokens,
        "cache_read_input_tokens": cr.cache_read_input_tokens,
        "cache_creation_input_tokens": cr.cache_creation_input_tokens,
    }


# ── session resume support ───────────────────────────────────────────


def _session_resume_prompt(ps: PhaseState) -> str:
    """Show resume prompt for interrupted session. Returns choice string."""
    click.echo()
    click.echo(click.style("  \U0001f504 Found interrupted session", fg="yellow", bold=True))
    click.echo(f"   Phase: {ps.phase}", nl=False)
    if ps.active_story_id:
        click.echo(f", Story: {ps.active_story_id}", nl=False)
    click.echo()
    if ps.active_session_started:
        click.echo(f"   Started: {ps.active_session_started}")
    click.echo()
    click.echo("   [1] Resume headlessly  \u2014 continue automated session")
    click.echo("   [2] Resume interactive \u2014 open interactive Claude session")
    click.echo("   [3] Start fresh        \u2014 start a new session")
    click.echo("   [4] Abort")
    click.echo()
    choice = click.prompt(
        "   Choice", type=click.Choice(["1", "2", "3", "4"]), default="1",
    )
    return {"1": "headless", "2": "interactive", "3": "fresh", "4": "abort"}[choice]


def _clear_session_tracking(ps: PhaseState) -> None:
    """Clear active session tracking fields on PhaseState."""
    ps.active_session_id = ""
    ps.active_story_id = ""
    ps.active_session_started = ""


# ── generic iteration loop ───────────────────────────────────────────


def _run_loop(
    phase: str,
    state: StateManager,
    max_iterations: int,
    cooldown: int,
    iteration_fn: Callable[[int, PhaseState], IterationResult],
    completion_fn: Callable[[IterationResult, PhaseState], bool],
    resume_fn: Callable[[str, PhaseState], IterationResult] | None = None,
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
) -> PhaseState:
    """Generic iteration loop shared by all phases."""
    ps = state.load_phase_state(phase)

    # Check for resumable session from previous crash
    if ps.active_session_id and resume_fn:
        if not state.claude_session_exists(ps.active_session_id):
            click.echo("  Previous session not found on disk, starting fresh")
            _clear_session_tracking(ps)
            state.save_phase_state(ps)
        else:
            choice = _session_resume_prompt(ps)
            if choice == "headless":
                result = resume_fn(ps.active_session_id, ps)
                state.log_iteration(result)
                ps.total_cost_usd += result.cost_usd
                _clear_session_tracking(ps)
                if result.success:
                    ps.consecutive_errors = 0
                if completion_fn(result, ps):
                    ps.completed = True
                    state.save_phase_state(ps)
                    return ps
                state.save_phase_state(ps)
            elif choice == "interactive":
                resume_interactive(ps.active_session_id, str(state.project_dir), dangerously_skip_permissions)
                _clear_session_tracking(ps)
                state.save_phase_state(ps)
            elif choice == "abort":
                ps.completed = True
                ps.completion_reason = "user_aborted"
                state.save_phase_state(ps)
                return ps
            else:  # "fresh"
                _clear_session_tracking(ps)
                state.save_phase_state(ps)
    elif ps.active_session_id:
        # No resume_fn but stale tracking — clear it
        _clear_session_tracking(ps)
        state.save_phase_state(ps)

    # Only truly "done" completions block re-running.
    # Everything else (errors, max_iterations) is resumable.
    DONE_REASONS = {"generation_complete", "planning_complete", "all_stories_done", "single_story_done"}

    if ps.completed:
        if ps.completion_reason == "all_stories_done" and state.get_actionable_stories():
            # Stories were reset back to pending/rework — resume
            click.echo(f"  Phase '{phase}' resuming (stories reset to actionable)...")
            ps.completed = False
            ps.completion_reason = ""
        elif ps.completion_reason in DONE_REASONS:
            click.echo(f"  Phase '{phase}' already completed: {ps.completion_reason}")
            return ps
        else:
            # Resumable — reset and continue from where we left off
            click.echo(f"  Phase '{phase}' resuming (previously: {ps.completion_reason})...")
            ps.completed = False
            ps.consecutive_errors = 0
            ps.completion_reason = ""

    start_iter = ps.current_iteration + 1 if ps.current_iteration > 0 else 1

    for i in range(start_iter, start_iter + max_iterations):
        ps.current_iteration = i
        header = f"  [{phase}] iteration {i}"
        click.echo(f"\n{click.style('='*60, fg='cyan')}")
        click.echo(click.style(header, fg='cyan', bold=True))
        click.echo(click.style('='*60, fg='cyan'))

        t0 = time.time()
        result = iteration_fn(i, ps)
        result.duration = time.time() - t0

        state.log_iteration(result)
        ps.total_cost_usd += result.cost_usd

        # User abort — stop immediately
        if result.error == "aborted":
            ps.completed = True
            ps.completion_reason = "user_aborted"
            state.save_phase_state(ps)
            click.echo(click.style(f"\n  Phase '{phase}' aborted by user", fg='yellow'))
            return ps

        if result.success:
            ps.consecutive_errors = 0
            ps.last_error = ""
            ps.last_summary = result.raw_output[:500] if result.raw_output else result.impl_status
            click.echo(click.style(f"  ✓ success", fg='green', bold=True) + f" (${result.cost_usd:.4f}, {result.duration:.1f}s)")
        elif result.error == "interrupted":
            # User interrupted but chose to continue — don't count as error
            click.echo(click.style(f"  ⏭ skipped (interrupted)", fg='yellow'))
        else:
            ps.consecutive_errors += 1
            ps.last_error = result.error[:500]
            click.echo(click.style(f"  ✗ error:", fg='red', bold=True) + f" {result.error[:120]}")

        if completion_fn(result, ps):
            ps.completed = True
            state.save_phase_state(ps)
            click.echo(click.style(f"\n  Phase '{phase}' complete: {ps.completion_reason}", fg='green'))
            return ps

        state.save_phase_state(ps)

        if i < start_iter + max_iterations - 1:
            time.sleep(cooldown)

    ps.completed = True
    ps.completion_reason = "max_iterations"
    state.save_phase_state(ps)
    click.echo(click.style(f"\n  Phase '{phase}' complete: max iterations reached", fg='yellow'))
    return ps


# ── Phase 1: Plan ────────────────────────────────────────────────────


def run_plan_loop(
    state: StateManager,
    *,
    model: str = "sonnet",
    max_iterations: int = 5,
    cooldown: int = 5,
    user_prompt: str = "",
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
) -> PhaseState:
    """Run the plan refinement loop."""
    total = max_iterations
    system_prompt = build_guardrails_system_prompt("plan", state)

    def iteration_fn(i: int, ps: PhaseState) -> IterationResult:
        prompt = assemble_plan_prompt(state, iteration=i, total=total, user_prompt=user_prompt, phase_state=ps)

        sid = str(uuid.uuid4())
        ps.active_session_id = sid
        ps.active_session_started = datetime.now().isoformat()
        state.save_phase_state(ps)

        result = run_with_retry(
            prompt,
            model=model,
            allowed_tools=PLAN_TOOLS,
            system_prompt=system_prompt,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
            timeout=900,
            verbose=verbose,
            project_dir=str(state.project_dir),
            session_id=sid,
        )

        _clear_session_tracking(ps)

        if not result.success:
            return IterationResult(
                iteration=i, phase="plan", mode="refine",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )

        parsed = parse_plan_output(result.result)
        summary = parsed.get("changes_summary", "")
        if summary:
            click.echo(click.style("  Changes:", fg='blue') + f" {summary[:200]}")

        return IterationResult(
            iteration=i, phase="plan", mode="create" if i == 1 else "refine",
            success=True, raw_output=result.result,
            cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def resume_fn(session_id: str, ps: PhaseState) -> IterationResult:
        result = run_with_retry(
            "Continue refining the design document.",
            resume_session_id=session_id,
            timeout=900,
            project_dir=str(state.project_dir),
            dangerously_skip_permissions=dangerously_skip_permissions,
            verbose=verbose,
        )
        if not result.success:
            return IterationResult(
                iteration=ps.current_iteration, phase="plan", mode="resume",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )
        parsed = parse_plan_output(result.result)
        summary = parsed.get("changes_summary", "")
        if summary:
            click.echo(click.style("  Changes:", fg='blue') + f" {summary[:200]}")
        return IterationResult(
            iteration=ps.current_iteration, phase="plan", mode="resume",
            success=True, raw_output=result.result,
            cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def completion_fn(result: IterationResult, ps: PhaseState) -> bool:
        if result.raw_output and any(
            line.strip() == "[PLANNING_COMPLETE]" for line in result.raw_output.splitlines()
        ):
            ps.completion_reason = "planning_complete"
            return True
        if ps.consecutive_errors >= 5:
            ps.completion_reason = "consecutive_errors"
            return True
        return False

    return _run_loop("plan", state, max_iterations, cooldown, iteration_fn, completion_fn, resume_fn, verbose, dangerously_skip_permissions)


# ── Phase 2: Stories ──────────────────────────────────────────────────


def run_stories_loop(
    state: StateManager,
    *,
    model: str = "sonnet",
    max_iterations: int = 50,
    cooldown: int = 5,
    extract_weight: int = 80,
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
) -> PhaseState:
    """Run the story extraction loop."""
    if not state.has_design_doc():
        click.echo("Error: No design document found. Run 'pralph plan' first.")
        return PhaseState(phase="stories", completed=True, completion_reason="no_design_doc")

    system_prompt = build_guardrails_system_prompt("stories", state)

    def _pick_mode() -> str:
        return "extract" if random.randint(1, 100) <= extract_weight else "research"

    def iteration_fn(i: int, ps: PhaseState) -> IterationResult:
        mode = _pick_mode()
        prompt = assemble_stories_prompt(state, mode=mode, phase_state=ps)
        tools = STORIES_TOOLS_RESEARCH if mode == "research" else STORIES_TOOLS_EXTRACT

        sid = str(uuid.uuid4())
        ps.active_session_id = sid
        ps.active_session_started = datetime.now().isoformat()
        state.save_phase_state(ps)

        result = run_with_retry(
            prompt,
            model=model,
            allowed_tools=tools,
            system_prompt=system_prompt,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
            timeout=900 if mode == "research" else 600,
            verbose=verbose,
            project_dir=str(state.project_dir),
            session_id=sid,
        )

        _clear_session_tracking(ps)

        if not result.success:
            return IterationResult(
                iteration=i, phase="stories", mode=mode,
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )

        stories, is_complete = parse_stories_output(result.result)

        # Deduplicate against existing
        existing_ids = state.get_story_ids()
        new_stories = [s for s in stories if s.id not in existing_ids]

        if new_stories:
            state.append_stories(new_stories)
            click.echo(click.style(f"  +{len(new_stories)} stories", fg='green', bold=True) + f" (mode={mode})")
            for s in new_stories:
                click.echo(f"    {click.style(s.id, fg='blue')}: {s.title}")
            ps.consecutive_empty = 0
        else:
            ps.consecutive_empty += 1
            click.echo(click.style(f"  0 new stories", fg='yellow') + f" (mode={mode}, consecutive_empty={ps.consecutive_empty})")

        return IterationResult(
            iteration=i, phase="stories", mode=mode,
            success=True, stories_generated=len(new_stories),
            raw_output=result.result, cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def _stories_resume_process(result: ClaudeResult) -> IterationResult:
        """Shared result processing for stories resume."""
        if not result.success:
            return IterationResult(
                iteration=0, phase="stories", mode="resume",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )
        stories, _ = parse_stories_output(result.result)
        existing_ids = state.get_story_ids()
        new_stories = [s for s in stories if s.id not in existing_ids]
        if new_stories:
            state.append_stories(new_stories)
            click.echo(click.style(f"  +{len(new_stories)} stories (resumed)", fg='green', bold=True))
            for s in new_stories:
                click.echo(f"    {click.style(s.id, fg='blue')}: {s.title}")
        return IterationResult(
            iteration=0, phase="stories", mode="resume",
            success=True, stories_generated=len(new_stories),
            raw_output=result.result, cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def resume_fn(session_id: str, ps: PhaseState) -> IterationResult:
        result = run_with_retry(
            "Continue extracting stories.",
            resume_session_id=session_id,
            timeout=900,
            project_dir=str(state.project_dir),
            dangerously_skip_permissions=dangerously_skip_permissions,
            verbose=verbose,
        )
        ir = _stories_resume_process(result)
        ir.iteration = ps.current_iteration
        return ir

    def completion_fn(result: IterationResult, ps: PhaseState) -> bool:
        if detect_completion_signal(result.raw_output):
            ps.completion_reason = "generation_complete"
            return True
        if ps.consecutive_empty >= 3:
            ps.completion_reason = "consecutive_empty"
            return True
        if ps.consecutive_errors >= 5:
            ps.completion_reason = "consecutive_errors"
            return True
        return False

    ps = _run_loop("stories", state, max_iterations, cooldown, iteration_fn, completion_fn, resume_fn, verbose, dangerously_skip_permissions)

    total = len(state.load_stories())
    click.echo(f"\n  Total stories: {total}")
    return ps


# ── Add (single story) ────────────────────────────────────────────────


def run_add(
    state: StateManager,
    *,
    idea: str,
    is_next: bool = False,
    model: str = "sonnet",
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
) -> Story | None:
    """Create a single story from an idea. One Claude call, no loop."""
    system_prompt = build_guardrails_system_prompt("stories", state)
    prompt = assemble_add_prompt(state, idea=idea, is_next=is_next)

    result = run_with_retry(
        prompt,
        model=model,
        allowed_tools=ADD_TOOLS,
        system_prompt=system_prompt,
        dangerously_skip_permissions=dangerously_skip_permissions,
        max_budget_usd=max_budget_usd,
        timeout=600,
        verbose=verbose,
        project_dir=str(state.project_dir),
    )

    if not result.success:
        click.echo(click.style(f"  Error: {result.error[:200]}", fg='red'))
        state.log_iteration(IterationResult(
            iteration=1, phase="add", mode="add",
            success=False, error=result.error, cost_usd=result.cost_usd,
            **_token_kwargs(result),
        ))
        return None

    stories, _ = parse_stories_output(result.result)
    if not stories:
        click.echo(click.style("  No story could be parsed from Claude's response", fg='red'))
        state.log_iteration(IterationResult(
            iteration=1, phase="add", mode="add",
            success=False, error="no stories parsed", cost_usd=result.cost_usd,
            **_token_kwargs(result),
        ))
        return None

    story = stories[0]
    story.source = "manual"
    if is_next:
        story.priority = 1

    # Dedup check
    existing_ids = state.get_story_ids()
    if story.id in existing_ids:
        # Append suffix to avoid collision
        base = story.id
        for suffix in range(2, 100):
            candidate = f"{base}b{suffix}"
            if candidate not in existing_ids:
                story.id = candidate
                break

    state.append_stories([story])
    state.log_iteration(IterationResult(
        iteration=1, phase="add", mode="add",
        success=True, stories_generated=1,
        raw_output=result.result, cost_usd=result.cost_usd,
        story_id=story.id,
        **_token_kwargs(result),
    ))

    return story


# ── Refine (replace stories) ──────────────────────────────────────────


def run_refine(
    state: StateManager,
    *,
    instruction: str,
    original_stories: list[Story],
    model: str = "sonnet",
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
) -> list[Story]:
    """Refine existing stories based on an instruction. One Claude call, no loop."""
    system_prompt = build_guardrails_system_prompt("stories", state)
    prompt = assemble_refine_prompt(state, instruction=instruction, original_stories=original_stories)

    result = run_with_retry(
        prompt,
        model=model,
        allowed_tools=REFINE_TOOLS,
        system_prompt=system_prompt,
        dangerously_skip_permissions=dangerously_skip_permissions,
        max_budget_usd=max_budget_usd,
        timeout=600,
        verbose=verbose,
        project_dir=str(state.project_dir),
    )

    if not result.success:
        click.echo(click.style(f"  Error: {result.error[:200]}", fg='red'))
        state.log_iteration(IterationResult(
            iteration=1, phase="refine", mode="refine",
            success=False, error=result.error, cost_usd=result.cost_usd,
            **_token_kwargs(result),
        ))
        return []

    stories, _ = parse_stories_output(result.result)
    if not stories:
        click.echo(click.style("  No stories could be parsed from Claude's response", fg='red'))
        state.log_iteration(IterationResult(
            iteration=1, phase="refine", mode="refine",
            success=False, error="no stories parsed", cost_usd=result.cost_usd,
            **_token_kwargs(result),
        ))
        return []

    # Set source and refined_from metadata
    original_ids = [s.id for s in original_stories]
    for s in stories:
        s.source = "refine"
        s.metadata["refined_from"] = original_ids

    # Dedup IDs against existing
    existing_ids = state.get_story_ids()
    for s in stories:
        if s.id in existing_ids:
            base = s.id
            for suffix in range(2, 100):
                candidate = f"{base}b{suffix}"
                if candidate not in existing_ids:
                    s.id = candidate
                    break
        existing_ids.add(s.id)

    # Append new stories
    state.append_stories(stories)

    # Mark originals as skipped
    new_ids = [s.id for s in stories]
    refined_into = ", ".join(new_ids)
    for orig in original_stories:
        state.mark_story_status(orig.id, StoryStatus.skipped, summary=f"Refined into: {refined_into}")

    state.log_iteration(IterationResult(
        iteration=1, phase="refine", mode="refine",
        success=True, stories_generated=len(stories),
        raw_output=result.result, cost_usd=result.cost_usd,
        **_token_kwargs(result),
    ))

    return stories


# ── Ideate (batch) ────────────────────────────────────────────────────


def run_ideate_loop(
    state: StateManager,
    *,
    ideas_text: str,
    model: str = "sonnet",
    max_iterations: int = 10,
    cooldown: int = 5,
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
) -> PhaseState:
    """Process a batch of ideas into stories via the standard loop."""
    system_prompt = build_guardrails_system_prompt("stories", state)

    def iteration_fn(i: int, ps: PhaseState) -> IterationResult:
        prompt = assemble_ideate_prompt(state, ideas_text=ideas_text, phase_state=ps)

        sid = str(uuid.uuid4())
        ps.active_session_id = sid
        ps.active_session_started = datetime.now().isoformat()
        state.save_phase_state(ps)

        result = run_with_retry(
            prompt,
            model=model,
            allowed_tools=IDEATE_TOOLS,
            system_prompt=system_prompt,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
            timeout=900,
            verbose=verbose,
            project_dir=str(state.project_dir),
            session_id=sid,
        )

        _clear_session_tracking(ps)

        if not result.success:
            return IterationResult(
                iteration=i, phase="ideate", mode="ideate",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )

        stories, _ = parse_stories_output(result.result)

        # Override source on all stories
        for s in stories:
            s.source = "ideate"

        # Deduplicate against existing
        existing_ids = state.get_story_ids()
        new_stories = [s for s in stories if s.id not in existing_ids]

        if new_stories:
            state.append_stories(new_stories)
            click.echo(click.style(f"  +{len(new_stories)} stories", fg='green', bold=True))
            for s in new_stories:
                click.echo(f"    {click.style(s.id, fg='blue')}: {s.title}")
            ps.consecutive_empty = 0
        else:
            ps.consecutive_empty += 1
            click.echo(click.style(f"  0 new stories", fg='yellow') + f" (consecutive_empty={ps.consecutive_empty})")

        return IterationResult(
            iteration=i, phase="ideate", mode="ideate",
            success=True, stories_generated=len(new_stories),
            raw_output=result.result, cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def resume_fn(session_id: str, ps: PhaseState) -> IterationResult:
        result = run_with_retry(
            "Continue processing ideas into stories.",
            resume_session_id=session_id,
            timeout=900,
            project_dir=str(state.project_dir),
            dangerously_skip_permissions=dangerously_skip_permissions,
            verbose=verbose,
        )
        if not result.success:
            return IterationResult(
                iteration=ps.current_iteration, phase="ideate", mode="resume",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )
        stories, _ = parse_stories_output(result.result)
        for s in stories:
            s.source = "ideate"
        existing_ids = state.get_story_ids()
        new_stories = [s for s in stories if s.id not in existing_ids]
        if new_stories:
            state.append_stories(new_stories)
            click.echo(click.style(f"  +{len(new_stories)} stories (resumed)", fg='green', bold=True))
            for s in new_stories:
                click.echo(f"    {click.style(s.id, fg='blue')}: {s.title}")
        return IterationResult(
            iteration=ps.current_iteration, phase="ideate", mode="resume",
            success=True, stories_generated=len(new_stories),
            raw_output=result.result, cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def completion_fn(result: IterationResult, ps: PhaseState) -> bool:
        if detect_ideation_complete(result.raw_output):
            ps.completion_reason = "ideation_complete"
            return True
        if ps.consecutive_empty >= 2:
            ps.completion_reason = "consecutive_empty"
            return True
        if ps.consecutive_errors >= 3:
            ps.completion_reason = "consecutive_errors"
            return True
        return False

    ps = _run_loop("ideate", state, max_iterations, cooldown, iteration_fn, completion_fn, resume_fn, verbose, dangerously_skip_permissions)

    total = len(state.load_stories())
    click.echo(f"\n  Total stories: {total}")
    return ps


# ── Phase 2b: Webgen ─────────────────────────────────────────────────


def run_webgen_loop(
    state: StateManager,
    *,
    model: str = "sonnet",
    max_iterations: int = 50,
    cooldown: int = 5,
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
) -> PhaseState:
    """Run the web-gen requirements discovery loop."""
    if not state.has_design_doc():
        click.echo("Error: No design document found. Run 'pralph plan' first.")
        return PhaseState(phase="webgen", completed=True, completion_reason="no_design_doc")

    if not state.has_stories():
        click.echo(click.style("Warning: No stories found. Run 'pralph stories' before 'webgen'.", fg='yellow'))
        click.echo("  webgen discovers gaps relative to existing stories — without them it has no baseline.")
        if not click.confirm("  Continue anyway?", default=False):
            return PhaseState(phase="webgen", completed=True, completion_reason="no_stories")

    system_prompt = build_guardrails_system_prompt("stories", state)

    def iteration_fn(i: int, ps: PhaseState) -> IterationResult:
        prompt = assemble_stories_prompt(state, mode="webgen", phase_state=ps)

        sid = str(uuid.uuid4())
        ps.active_session_id = sid
        ps.active_session_started = datetime.now().isoformat()
        state.save_phase_state(ps)

        result = run_with_retry(
            prompt,
            model=model,
            allowed_tools=STORIES_TOOLS_RESEARCH,
            system_prompt=system_prompt,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
            timeout=900,
            verbose=verbose,
            project_dir=str(state.project_dir),
            session_id=sid,
        )

        _clear_session_tracking(ps)

        if not result.success:
            return IterationResult(
                iteration=i, phase="webgen", mode="webgen",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )

        stories, is_complete = parse_stories_output(result.result)

        existing_ids = state.get_story_ids()
        new_stories = [s for s in stories if s.id not in existing_ids]

        if new_stories:
            for s in new_stories:
                s.source = "webgen"
            state.append_stories(new_stories)
            click.echo(f"  +{len(new_stories)} webgen stories")
            for s in new_stories:
                click.echo(f"    {s.id}: {s.title}")
            ps.consecutive_empty = 0
        else:
            ps.consecutive_empty += 1
            click.echo(f"  0 new stories (consecutive_empty={ps.consecutive_empty})")

        return IterationResult(
            iteration=i, phase="webgen", mode="webgen",
            success=True, stories_generated=len(new_stories),
            raw_output=result.result, cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def resume_fn(session_id: str, ps: PhaseState) -> IterationResult:
        result = run_with_retry(
            "Continue discovering web-gen requirements.",
            resume_session_id=session_id,
            timeout=900,
            project_dir=str(state.project_dir),
            dangerously_skip_permissions=dangerously_skip_permissions,
            verbose=verbose,
        )
        if not result.success:
            return IterationResult(
                iteration=ps.current_iteration, phase="webgen", mode="resume",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )
        stories, _ = parse_stories_output(result.result)
        existing_ids = state.get_story_ids()
        new_stories = [s for s in stories if s.id not in existing_ids]
        if new_stories:
            for s in new_stories:
                s.source = "webgen"
            state.append_stories(new_stories)
            click.echo(click.style(f"  +{len(new_stories)} webgen stories (resumed)", fg='green', bold=True))
            for s in new_stories:
                click.echo(f"    {click.style(s.id, fg='blue')}: {s.title}")
        return IterationResult(
            iteration=ps.current_iteration, phase="webgen", mode="resume",
            success=True, stories_generated=len(new_stories),
            raw_output=result.result, cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def completion_fn(result: IterationResult, ps: PhaseState) -> bool:
        if detect_completion_signal(result.raw_output):
            ps.completion_reason = "generation_complete"
            return True
        if ps.consecutive_empty >= 3:
            ps.completion_reason = "consecutive_empty"
            return True
        if ps.consecutive_errors >= 5:
            ps.completion_reason = "consecutive_errors"
            return True
        return False

    ps = _run_loop("webgen", state, max_iterations, cooldown, iteration_fn, completion_fn, resume_fn, verbose, dangerously_skip_permissions)

    total = len(state.load_stories())
    click.echo(f"\n  Total stories: {total}")
    return ps


# ── Phase 3: Implement ───────────────────────────────────────────────


def run_implement_loop(
    state: StateManager,
    *,
    model: str = "sonnet",
    max_iterations: int = 50,
    cooldown: int = 5,
    story_id: str | None = None,
    phase1: bool = True,
    review: bool = True,
    compound: bool = False,
    user_prompt: str = "",
    extra_tools: str = "",
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
    parallel: int = 1,
) -> PhaseState:
    """Run the implementation loop."""
    if not state.has_stories():
        click.echo("Error: No stories found. Run 'pralph stories' first.")
        return PhaseState(phase="implement", completed=True, completion_reason="no_stories")

    # Recover any stories orphaned by a previous crash
    recovered = state.recover_orphaned_stories()
    if recovered:
        click.echo(click.style(f"  Recovered {len(recovered)} orphaned stories from previous crash:", fg='yellow', bold=True))
        for s in recovered:
            click.echo(f"    {click.style(s.id, fg='blue')}: {s.title}")

    system_prompt = build_guardrails_system_prompt("implement", state)

    # Build tools list with extras
    tools = IMPLEMENT_TOOLS
    if extra_tools:
        tools = tools + "," + extra_tools

    # If specific story requested, just implement it
    if story_id:
        return _implement_single(state, story_id, model=model, system_prompt=system_prompt,
                                 tools=tools, user_prompt=user_prompt, review=review,
                                 compound=compound, verbose=verbose,
                                 dangerously_skip_permissions=dangerously_skip_permissions,
                                 max_budget_usd=max_budget_usd)

    # Parallel mode: run up to N stories concurrently
    if parallel > 1:
        return _run_parallel_implement(
            state,
            parallel=parallel,
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            user_prompt=user_prompt,
            phase1=phase1,
            review=review,
            compound=compound,
            cooldown=cooldown,
            verbose=verbose,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
        )

    # State-based mode selection (evaluated each iteration)
    def _pick_implement_mode() -> str:
        if phase1:
            has_analysis = state.has_phase1_analysis()
            has_foundation = any(
                s.category.upper() in FOUNDATION_CATEGORIES
                for s in state.get_pending_stories()
            )
            if not has_analysis and has_foundation:
                return "phase1_analyze"
        return "implement"

    story_queue: list[Story] = []

    def _refresh_queue() -> list[Story]:
        actionable = state.get_actionable_stories()
        # Rework stories are already at the front from get_actionable_stories()
        rework = [s for s in actionable if s.status == StoryStatus.rework]
        pending = [s for s in actionable if s.status == StoryStatus.pending]

        analysis_data = state.load_phase1_analysis()
        if analysis_data is not None:
            impl_order = analysis_data.get("implementation_order", analysis_data.get("phase_1_group", []))
            pending_ids = {s.id for s in pending}
            ordered_ids = [sid for sid in impl_order if sid in pending_ids]
            ordered = [s for sid in ordered_ids for s in pending if s.id == sid]
            remaining = [s for s in pending if s.id not in set(ordered_ids)]
            return rework + ordered + _sort_stories(remaining)
        return rework + _sort_stories(pending)

    def iteration_fn(i: int, ps: PhaseState) -> IterationResult:
        nonlocal story_queue
        mode = _pick_implement_mode()

        # ── phase1_analyze: identify foundation stories ──
        if mode == "phase1_analyze":
            click.echo(click.style("  Mode: phase1_analyze", fg='magenta', bold=True) + " — identifying foundation stories...")
            analyze_prompt = assemble_phase1_analyze_prompt(state)
            result = run_with_retry(
                analyze_prompt,
                model=model,
                allowed_tools="Read,Glob,Grep",
                system_prompt=system_prompt,
                dangerously_skip_permissions=dangerously_skip_permissions,
                max_budget_usd=max_budget_usd,
                timeout=600,
                verbose=verbose,
                project_dir=str(state.project_dir),
            )

            if not result.success:
                return IterationResult(
                    iteration=i, phase="implement", mode="phase1_analyze",
                    success=False, error=result.error, cost_usd=result.cost_usd,
                    **_token_kwargs(result),
                )

            data = extract_json_from_text(result.result)
            if not isinstance(data, dict) or "phase_1_group" not in data:
                return IterationResult(
                    iteration=i, phase="implement", mode="phase1_analyze",
                    success=False, error="Could not parse phase1 group from analysis",
                    cost_usd=result.cost_usd,
                    **_token_kwargs(result),
                )

            # Save analysis to DuckDB for the implement step
            state.save_phase1_analysis(data)

            group = data["phase_1_group"]
            reasoning = data.get("reasoning", {})
            click.echo(click.style(f"  Phase 1 group ({len(group)} stories):", fg='blue', bold=True) + f" {', '.join(group)}")
            for sid, reason in reasoning.items():
                click.echo(f"    {click.style(sid, fg='blue')}: {reason[:80]}")

            return IterationResult(
                iteration=i, phase="implement", mode="phase1_analyze",
                success=True, raw_output=result.result, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )

        # ── implement: one story per iteration ──
        if not story_queue:
            story_queue = _refresh_queue()

        if not story_queue:
            return IterationResult(
                iteration=i, phase="implement", mode="implement",
                success=True, impl_status="all_done",
            )

        story = story_queue.pop(0)
        state.mark_story_status(story.id, StoryStatus.in_progress)
        click.echo(click.style(f"  Implementing: {story.id}", fg='yellow', bold=True) + f" — {story.title}")

        # Log iteration start
        state.log_iteration(IterationResult(
            iteration=i, phase="implement", mode="implement_started",
            success=True, story_id=story.id,
        ))

        prompt = assemble_implement_prompt(state, story, phase_state=ps, user_prompt=user_prompt)

        sid = str(uuid.uuid4())
        ps.active_session_id = sid
        ps.active_story_id = story.id
        ps.active_session_started = datetime.now().isoformat()
        state.save_phase_state(ps)

        result = run_with_retry(
            prompt,
            model=model,
            allowed_tools=tools,
            system_prompt=system_prompt,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
            timeout=1800,
            verbose=verbose,
            project_dir=str(state.project_dir),
            session_id=sid,
        )

        _clear_session_tracking(ps)

        if not result.success:
            if result.error in ("interrupted", "aborted"):
                state.mark_story_status(story.id, StoryStatus.pending, summary=f"User {result.error}")
                story_queue.clear()
                return IterationResult(
                    iteration=i, phase="implement", mode="implement",
                    success=result.error == "interrupted",
                    error=result.error if result.error == "aborted" else "",
                    impl_status=result.error,
                    cost_usd=result.cost_usd, story_id=story.id,
                    **_token_kwargs(result),
                )
            state.mark_story_status(
                story.id, StoryStatus.error, summary=result.error[:200],
                error_reason=result.error, error_output=result.result or "",
            )
            return IterationResult(
                iteration=i, phase="implement", mode="implement",
                success=False, error=result.error, cost_usd=result.cost_usd,
                story_id=story.id,
                **_token_kwargs(result),
            )

        parsed = parse_implement_output(result.result)
        status_str = parsed.get("status", "error")
        summary = parsed.get("summary", "")

        try:
            new_status = StoryStatus(status_str)
        except ValueError:
            new_status = StoryStatus.implemented if status_str == "completed" else StoryStatus.error

        state.mark_story_status(
            story.id, new_status, summary=summary, extra=parsed,
            error_reason=parsed.get("reason", "") if new_status == StoryStatus.error else "",
            error_output=result.result if new_status == StoryStatus.error else "",
        )
        status_color = 'green' if new_status == StoryStatus.implemented else 'yellow'
        click.echo(f"  → {story.id}: {click.style(new_status.value, fg=status_color)} — {summary[:120]}")

        total_cost = result.cost_usd
        total_input = result.input_tokens
        total_output = result.output_tokens
        total_cache_read = result.cache_read_input_tokens
        total_cache_create = result.cache_creation_input_tokens

        # Review step: run on fresh Claude instance after successful implementation
        if new_status == StoryStatus.implemented and review:
            review_result = _run_review(
                state, story,
                model=model,
                system_prompt=system_prompt,
                verbose=verbose,
                dangerously_skip_permissions=dangerously_skip_permissions,
                max_budget_usd=max_budget_usd,
            )
            if review_result is not None:
                total_cost += review_result.cost_usd
                total_input += review_result.input_tokens
                total_output += review_result.output_tokens
                total_cache_read += review_result.cache_read_input_tokens
                total_cache_create += review_result.cache_creation_input_tokens
                if not review_result.approved:
                    new_status = StoryStatus.rework
                    state.mark_story_status(story.id, StoryStatus.rework, summary="Review rejected")
                    story_queue.clear()  # Force refresh to pick up rework story

        # Compound learning: capture solutions after successful implementation
        if new_status == StoryStatus.implemented and compound:
            compound_result = _run_compound_capture(
                state, story,
                model=model,
                system_prompt=system_prompt,
                verbose=verbose,
                dangerously_skip_permissions=dangerously_skip_permissions,
                max_budget_usd=max_budget_usd,
            )
            total_cost += compound_result.cost_usd
            total_input += compound_result.input_tokens
            total_output += compound_result.output_tokens
            total_cache_read += compound_result.cache_read_input_tokens
            total_cache_create += compound_result.cache_creation_input_tokens

        # Clean up analysis when all its stories are done
        if new_status == StoryStatus.implemented and state.has_phase1_analysis():
            p1_data = state.load_phase1_analysis()
            group_ids = set(p1_data.get("implementation_order", p1_data.get("phase_1_group", [])))
            pending_ids = {s.id for s in state.get_pending_stories()}
            if not group_ids & pending_ids:
                state.delete_phase1_analysis()
                story_queue.clear()
                click.echo(click.style("  Phase 1 analysis complete — switching to normal ordering", fg='green'))

        return IterationResult(
            iteration=i, phase="implement", mode="implement",
            success=new_status != StoryStatus.error, impl_status=new_status.value,
            error=parsed.get("reason", "") if new_status == StoryStatus.error else "",
            cost_usd=total_cost, story_id=story.id,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_read_input_tokens=total_cache_read,
            cache_creation_input_tokens=total_cache_create,
        )

    def resume_fn(session_id: str, ps: PhaseState) -> IterationResult:
        story = None
        if ps.active_story_id:
            story = next((s for s in state.load_stories() if s.id == ps.active_story_id), None)
        if not story:
            return IterationResult(
                iteration=ps.current_iteration, phase="implement", mode="resume",
                success=False, error=f"Story '{ps.active_story_id}' not found for resume",
            )

        result = run_with_retry(
            "Continue implementing the story.",
            resume_session_id=session_id,
            timeout=1800,
            project_dir=str(state.project_dir),
            dangerously_skip_permissions=dangerously_skip_permissions,
            verbose=verbose,
        )

        if not result.success:
            state.mark_story_status(
                story.id, StoryStatus.error, summary=f"Resume failed: {result.error[:200]}",
                error_reason=f"Resume failed: {result.error}", error_output=result.result or "",
            )
            return IterationResult(
                iteration=ps.current_iteration, phase="implement", mode="resume",
                success=False, error=result.error, cost_usd=result.cost_usd,
                story_id=story.id,
                **_token_kwargs(result),
            )

        parsed = parse_implement_output(result.result)
        status_str = parsed.get("status", "error")
        summary = parsed.get("summary", "")
        try:
            new_status = StoryStatus(status_str)
        except ValueError:
            new_status = StoryStatus.implemented if status_str == "completed" else StoryStatus.error

        state.mark_story_status(
            story.id, new_status, summary=summary, extra=parsed,
            error_reason=parsed.get("reason", "") if new_status == StoryStatus.error else "",
            error_output=result.result if new_status == StoryStatus.error else "",
        )
        status_color = 'green' if new_status == StoryStatus.implemented else 'yellow'
        click.echo(f"  \u2192 {story.id}: {click.style(new_status.value, fg=status_color)} \u2014 {summary[:120]}")

        total_cost = result.cost_usd
        total_input = result.input_tokens
        total_output = result.output_tokens
        total_cache_read = result.cache_read_input_tokens
        total_cache_create = result.cache_creation_input_tokens

        if new_status == StoryStatus.implemented and review:
            review_result = _run_review(
                state, story,
                model=model,
                system_prompt=system_prompt,
                verbose=verbose,
                dangerously_skip_permissions=dangerously_skip_permissions,
                max_budget_usd=max_budget_usd,
            )
            if review_result is not None:
                total_cost += review_result.cost_usd
                total_input += review_result.input_tokens
                total_output += review_result.output_tokens
                total_cache_read += review_result.cache_read_input_tokens
                total_cache_create += review_result.cache_creation_input_tokens
                if not review_result.approved:
                    new_status = StoryStatus.rework
                    state.mark_story_status(story.id, StoryStatus.rework, summary="Review rejected")

        return IterationResult(
            iteration=ps.current_iteration, phase="implement", mode="resume",
            success=new_status != StoryStatus.error, impl_status=new_status.value,
            error=parsed.get("reason", "") if new_status == StoryStatus.error else "",
            cost_usd=total_cost, story_id=story.id,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_read_input_tokens=total_cache_read,
            cache_creation_input_tokens=total_cache_create,
        )

    def completion_fn(result: IterationResult, ps: PhaseState) -> bool:
        if result.impl_status == "all_done":
            ps.completion_reason = "all_stories_done"
            return True
        if ps.consecutive_errors >= 5:
            ps.completion_reason = "consecutive_errors"
            return True
        return False

    return _run_loop("implement", state, max_iterations, cooldown, iteration_fn, completion_fn, resume_fn, verbose, dangerously_skip_permissions)


@dataclass
class _ReviewResult:
    approved: bool
    feedback: str
    issues: list
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def _run_review(
    state: StateManager,
    story: Story,
    *,
    model: str,
    system_prompt: str,
    verbose: bool,
    dangerously_skip_permissions: bool,
    max_budget_usd: float | None,
) -> _ReviewResult | None:
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

    return _ReviewResult(
        approved=approved,
        feedback=feedback,
        issues=issues,
        cost_usd=result.cost_usd,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_input_tokens=result.cache_read_input_tokens,
        cache_creation_input_tokens=result.cache_creation_input_tokens,
    )


def _implement_single(
    state: StateManager,
    story_id: str,
    *,
    model: str,
    system_prompt: str,
    tools: str = IMPLEMENT_TOOLS,
    user_prompt: str = "",
    review: bool = True,
    compound: bool = False,
    verbose: bool,
    dangerously_skip_permissions: bool,
    max_budget_usd: float | None,
) -> PhaseState:
    """Implement a single story by ID."""
    stories = state.load_stories()
    story = next((s for s in stories if s.id == story_id), None)
    if not story:
        click.echo(f"Error: Story '{story_id}' not found")
        return PhaseState(phase="implement", completed=True, completion_reason="story_not_found")

    # If story was already in_progress (crash recovery via --story), add metadata
    if story.status == StoryStatus.in_progress:
        story.metadata["previous_attempt"] = {
            "was_in_progress": True,
            "recovered_at": datetime.now().isoformat(),
        }

    state.mark_story_status(story.id, StoryStatus.in_progress)
    click.echo(f"  Implementing: {story.id} — {story.title}")

    prompt = assemble_implement_prompt(state, story, user_prompt=user_prompt)
    result = run_with_retry(
        prompt,
        model=model,
        allowed_tools=tools,
        system_prompt=system_prompt,
        dangerously_skip_permissions=dangerously_skip_permissions,
        max_budget_usd=max_budget_usd,
        timeout=1800,
        verbose=verbose,
        project_dir=str(state.project_dir),
    )

    if not result.success:
        if result.error in ("interrupted", "aborted"):
            state.mark_story_status(story.id, StoryStatus.pending, summary=f"User {result.error}")
            reason = "user_aborted" if result.error == "aborted" else "user_interrupted"
            return PhaseState(phase="implement", completed=True, completion_reason=reason)
        state.mark_story_status(
            story.id, StoryStatus.error, summary=result.error[:200],
            error_reason=result.error, error_output=result.result or "",
        )
        click.echo(f"  Error: {result.error[:200]}")
        return PhaseState(phase="implement", completed=True, completion_reason="error")

    parsed = parse_implement_output(result.result)
    status_str = parsed.get("status", "error")
    summary = parsed.get("summary", "")
    try:
        new_status = StoryStatus(status_str)
    except ValueError:
        new_status = StoryStatus.implemented

    state.mark_story_status(story.id, new_status, summary=summary, extra=parsed)
    click.echo(f"  → {story.id}: {new_status.value} — {summary[:120]}")

    # Review step for single story implementation
    if new_status == StoryStatus.implemented and review:
        review_result = _run_review(
            state, story,
            model=model,
            system_prompt=system_prompt,
            verbose=verbose,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
        )
        if review_result is not None and not review_result.approved:
            state.mark_story_status(story.id, StoryStatus.rework, summary="Review rejected")
            return PhaseState(phase="implement", completed=True, completion_reason="review_rejected")

    # Compound learning: capture solutions after successful implementation
    if new_status == StoryStatus.implemented and compound:
        _run_compound_capture(
            state, story,
            model=model,
            system_prompt=system_prompt,
            verbose=verbose,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
        )

    return PhaseState(phase="implement", completed=True, completion_reason="single_story_done")


def _slugify(text: str) -> str:
    """Generate a filename-safe slug from text."""
    import re as _re
    slug = text.lower().strip()
    slug = _re.sub(r"[^\w\s-]", "", slug)
    slug = _re.sub(r"[\s_]+", "-", slug)
    slug = _re.sub(r"-+", "-", slug)
    return slug[:80].strip("-")


@dataclass
class _CompoundResult:
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def _run_compound_capture(
    state: StateManager,
    story: Story,
    *,
    model: str,
    system_prompt: str,
    verbose: bool,
    dangerously_skip_permissions: bool,
    max_budget_usd: float | None,
) -> _CompoundResult:
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
        return _CompoundResult(cost_usd=result.cost_usd, **_token_kwargs(result))

    parsed = parse_compound_output(result.result)

    if not parsed["captured"]:
        click.echo(click.style(f"  Nothing notable: {parsed['reason'][:120]}", fg='yellow'))
        return _CompoundResult(cost_usd=result.cost_usd, **_token_kwargs(result))

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

    click.echo(click.style(f"  Captured {len(solutions)} solution(s)", fg='green', bold=True))
    return _CompoundResult(cost_usd=result.cost_usd, **_token_kwargs(result))


def run_compound(
    state: StateManager,
    *,
    story_id: str | None = None,
    description: str = "",
    model: str = "sonnet",
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
) -> float:
    """Standalone compound capture. Returns cost."""
    from pralph.assembler import build_guardrails_system_prompt

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

    cr = _run_compound_capture(
        state, story,
        model=model,
        system_prompt=system_prompt,
        verbose=verbose,
        dangerously_skip_permissions=dangerously_skip_permissions,
        max_budget_usd=max_budget_usd,
    )
    return cr.cost_usd


def _sort_stories(stories: list[Story]) -> list[Story]:
    """Sort stories: foundation categories first → priority → dependency order."""

    def sort_key(s: Story) -> tuple[int, int, str]:
        is_foundation = 0 if s.category.upper() in FOUNDATION_CATEGORIES else 1
        return (is_foundation, s.priority, s.id)

    sorted_stories = sorted(stories, key=sort_key)

    # Simple topological adjustment: if A depends on B, B comes first
    id_to_idx = {s.id: i for i, s in enumerate(sorted_stories)}
    result: list[Story] = []
    visited: set[str] = set()

    def visit(story: Story) -> None:
        if story.id in visited:
            return
        visited.add(story.id)
        for dep_id in story.dependencies:
            if dep_id in id_to_idx and dep_id not in visited:
                dep_story = sorted_stories[id_to_idx[dep_id]]
                visit(dep_story)
        result.append(story)

    for s in sorted_stories:
        visit(s)

    return result


# ── Parallel implementation ──────────────────────────────────────────


_DONE_STATUSES = frozenset({
    StoryStatus.implemented, StoryStatus.skipped,
    StoryStatus.duplicate, StoryStatus.external,
    StoryStatus.error,
})


def _get_ready_stories(
    state: StateManager,
    in_flight: set[str],
    max_count: int,
) -> list[Story]:
    """Return stories whose dependencies are all resolved, up to max_count.

    A story is ready if:
    - It is pending or rework
    - All its dependencies are resolved (implemented, skipped, or errored)
    - It is not already in-flight

    Note: errored dependencies count as resolved so dependents are not blocked
    forever. The dependent story's claude session will see the error status and
    can decide how to proceed.
    """
    all_stories = state.load_stories()
    done_ids = {s.id for s in all_stories if s.status in _DONE_STATUSES}
    actionable = [
        s for s in all_stories
        if s.status in (StoryStatus.pending, StoryStatus.rework) and s.id not in in_flight
    ]

    # Rework stories first
    rework = [s for s in actionable if s.status == StoryStatus.rework]
    pending = [s for s in actionable if s.status == StoryStatus.pending]
    candidates = rework + _sort_stories(pending)

    ready: list[Story] = []
    for s in candidates:
        if len(ready) >= max_count:
            break
        deps_met = all(dep_id in done_ids for dep_id in s.dependencies)
        if deps_met:
            ready.append(s)

    return ready


def _run_parallel_implement(
    state: StateManager,
    *,
    parallel: int,
    model: str,
    system_prompt: str,
    tools: str,
    user_prompt: str,
    phase1: bool,
    review: bool,
    compound: bool,
    cooldown: int,
    verbose: bool,
    dangerously_skip_permissions: bool,
    max_budget_usd: float | None,
) -> PhaseState:
    """Run parallel implementation: up to N stories concurrently with dependency ordering."""
    if phase1:
        click.echo(click.style("  Note: --phase1 analysis is skipped in parallel mode", fg='yellow'))

    ps = state.load_phase_state("implement")

    # Resume logic (same as _run_loop)
    DONE_REASONS = {"all_stories_done", "single_story_done"}
    if ps.completed:
        if ps.completion_reason in DONE_REASONS:
            click.echo(f"  Phase 'implement' already completed: {ps.completion_reason}")
            return ps
        else:
            click.echo(f"  Phase 'implement' resuming (previously: {ps.completion_reason})...")
            ps.completed = False
            ps.consecutive_errors = 0
            ps.completion_reason = ""

    process_group = ProcessGroup()
    process_group.start_monitor()

    in_flight: set[str] = set()
    total_cost = 0.0
    aborted = False

    def _implement_one_story(story: Story) -> tuple[Story, ClaudeResult, dict]:
        """Worker function: implement a single story. Returns (story, claude_result, parsed)."""
        prompt = assemble_implement_prompt(state, story, phase_state=ps, user_prompt=user_prompt)
        result = run_with_retry_parallel(
            prompt,
            story_id=story.id,
            process_group=process_group,
            model=model,
            allowed_tools=tools,
            system_prompt=system_prompt,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
            timeout=1800,
            verbose=verbose,
            project_dir=str(state.project_dir),
        )
        parsed = {}
        if result.success:
            parsed = parse_implement_output(result.result)
        return story, result, parsed

    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {}

            while True:
                # Check for ESC interrupt at the group level
                if process_group.is_interrupted:
                    process_group.stop_monitor()
                    choice = handle_parallel_interrupt()

                    if choice == "continue":
                        process_group.resume_all()
                        process_group.start_monitor()
                    else:  # abort
                        process_group.kill_all()
                        aborted = True
                        # Wait for workers to finish (subprocesses are dead,
                        # so workers will return promptly). This prevents the
                        # ThreadPoolExecutor context manager from blocking on
                        # shutdown and avoids swallowed exceptions.
                        for fut in list(futures):
                            try:
                                fut.result(timeout=10)
                            except Exception:
                                pass
                        futures.clear()
                        # Reset in-flight stories to pending
                        for sid in in_flight:
                            state.mark_story_status(sid, StoryStatus.pending, summary="User aborted (parallel)")
                        in_flight.clear()
                        break

                # Find ready stories to fill worker slots
                slots = parallel - len(futures)
                if slots > 0:
                    ready = _get_ready_stories(state, in_flight, slots)
                    for story in ready:
                        state.mark_story_status(story.id, StoryStatus.in_progress)
                        in_flight.add(story.id)
                        click.echo(click.style(f"  \u25b6 Starting: {story.id}", fg='yellow', bold=True) + f" \u2014 {story.title}")
                        state.log_iteration(IterationResult(
                            iteration=ps.current_iteration + 1, phase="implement", mode="implement_started",
                            success=True, story_id=story.id,
                        ))
                        fut = pool.submit(_implement_one_story, story)
                        futures[fut] = story.id

                # If nothing running and nothing ready, check if we're done or deadlocked
                if not futures:
                    remaining = _get_ready_stories(state, in_flight, 1)
                    if not remaining:
                        # Check if there are any stories still pending (but blocked)
                        all_stories = state.load_stories()
                        still_pending = [
                            s for s in all_stories
                            if s.status in (StoryStatus.pending, StoryStatus.rework)
                        ]
                        if still_pending:
                            status_by_id = {s.id: s.status for s in all_stories}
                            click.echo(click.style(
                                f"  \u26a0 Deadlock: {len(still_pending)} stories remaining but none are ready",
                                fg='red', bold=True,
                            ))
                            for s in still_pending[:5]:
                                dep_details = []
                                for dep_id in s.dependencies:
                                    dep_status = status_by_id.get(dep_id)
                                    if dep_status is None:
                                        dep_details.append(f"{dep_id}(missing)")
                                    elif dep_status == StoryStatus.in_progress:
                                        dep_details.append(f"{dep_id}(in_progress)")
                                    elif dep_status in (StoryStatus.pending, StoryStatus.rework):
                                        dep_details.append(f"{dep_id}(pending)")
                                    else:
                                        dep_details.append(dep_id)
                                click.echo(f"    {s.id}: deps=[{', '.join(dep_details) or 'none'}]")
                            ps.completed = True
                            ps.completion_reason = "dependency_deadlock"
                        else:
                            ps.completed = True
                            ps.completion_reason = "all_stories_done"
                        break

                # Wait for at least one completion
                done_futures = []
                while not done_futures:
                    if process_group.is_interrupted:
                        break
                    for fut in list(futures):
                        if fut.done():
                            done_futures.append(fut)
                    if not done_futures:
                        time.sleep(0.3)

                if process_group.is_interrupted:
                    continue  # Re-enter loop to handle interrupt

                # Process completed futures
                for fut in done_futures:
                    story_id = futures.pop(fut)
                    in_flight.discard(story_id)
                    ps.current_iteration += 1

                    try:
                        story, result, parsed = fut.result()
                    except Exception as e:
                        state.mark_story_status(story_id, StoryStatus.error, summary=str(e)[:200])
                        ps.consecutive_errors += 1
                        click.echo(click.style(f"  \u2717 {story_id}: error", fg='red', bold=True) + f" \u2014 {str(e)[:120]}")
                        state.log_iteration(IterationResult(
                            iteration=ps.current_iteration, phase="implement", mode="implement",
                            success=False, error=str(e)[:200], story_id=story_id,
                        ))
                        continue

                    total_cost += result.cost_usd

                    if not result.success:
                        if result.error in ("interrupted", "aborted"):
                            state.mark_story_status(story_id, StoryStatus.pending, summary=f"User {result.error}")
                        else:
                            state.mark_story_status(story_id, StoryStatus.error, summary=result.error[:200])
                            ps.consecutive_errors += 1
                        click.echo(click.style(f"  \u2717 {story_id}: {result.error[:80]}", fg='red'))
                        state.log_iteration(IterationResult(
                            iteration=ps.current_iteration, phase="implement", mode="implement",
                            success=False, error=result.error, cost_usd=result.cost_usd,
                            story_id=story_id, **_token_kwargs(result),
                        ))
                        continue

                    # Success path
                    ps.consecutive_errors = 0
                    status_str = parsed.get("status", "error")
                    summary = parsed.get("summary", "")
                    try:
                        new_status = StoryStatus(status_str)
                    except ValueError:
                        new_status = StoryStatus.implemented if status_str == "completed" else StoryStatus.error

                    state.mark_story_status(story_id, new_status, summary=summary, extra=parsed)
                    status_color = 'green' if new_status == StoryStatus.implemented else 'yellow'
                    click.echo(f"  \u2713 {story_id}: {click.style(new_status.value, fg=status_color)} \u2014 {summary[:120]}")

                    iter_cost = result.cost_usd
                    iter_input = result.input_tokens
                    iter_output = result.output_tokens
                    iter_cache_read = result.cache_read_input_tokens
                    iter_cache_create = result.cache_creation_input_tokens

                    # Review and compound steps use run_with_retry (sequential)
                    # which creates its own ESC monitor. Stop the group monitor
                    # to avoid two monitors competing for stdin.
                    needs_sequential = (
                        (new_status == StoryStatus.implemented and review)
                        or (new_status == StoryStatus.implemented and compound)
                    )
                    if needs_sequential:
                        process_group.stop_monitor()

                    # Review step
                    if new_status == StoryStatus.implemented and review:
                        review_result = _run_review(
                            state, story,
                            model=model,
                            system_prompt=system_prompt,
                            verbose=verbose,
                            dangerously_skip_permissions=dangerously_skip_permissions,
                            max_budget_usd=max_budget_usd,
                        )
                        if review_result is not None:
                            iter_cost += review_result.cost_usd
                            iter_input += review_result.input_tokens
                            iter_output += review_result.output_tokens
                            iter_cache_read += review_result.cache_read_input_tokens
                            iter_cache_create += review_result.cache_creation_input_tokens
                            if not review_result.approved:
                                new_status = StoryStatus.rework
                                state.mark_story_status(story_id, StoryStatus.rework, summary="Review rejected")

                    # Compound learning
                    if new_status == StoryStatus.implemented and compound:
                        compound_result = _run_compound_capture(
                            state, story,
                            model=model,
                            system_prompt=system_prompt,
                            verbose=verbose,
                            dangerously_skip_permissions=dangerously_skip_permissions,
                            max_budget_usd=max_budget_usd,
                        )
                        iter_cost += compound_result.cost_usd
                        iter_input += compound_result.input_tokens
                        iter_output += compound_result.output_tokens
                        iter_cache_read += compound_result.cache_read_input_tokens
                        iter_cache_create += compound_result.cache_creation_input_tokens

                    if needs_sequential:
                        process_group.start_monitor()

                    state.log_iteration(IterationResult(
                        iteration=ps.current_iteration, phase="implement", mode="implement",
                        success=True, impl_status=new_status.value,
                        cost_usd=iter_cost, story_id=story_id,
                        input_tokens=iter_input, output_tokens=iter_output,
                        cache_read_input_tokens=iter_cache_read,
                        cache_creation_input_tokens=iter_cache_create,
                    ))
                    ps.total_cost_usd += iter_cost

                    if ps.consecutive_errors >= 5:
                        ps.completed = True
                        ps.completion_reason = "consecutive_errors"
                        # Kill remaining and drain futures
                        process_group.kill_all()
                        for f in list(futures):
                            try:
                                f.result(timeout=10)
                            except Exception:
                                pass
                        futures.clear()
                        for sid in in_flight:
                            state.mark_story_status(sid, StoryStatus.pending, summary="Stopped due to consecutive errors")
                        in_flight.clear()
                        break

                if ps.completed:
                    break

                time.sleep(cooldown)

    finally:
        process_group.stop_monitor()

    if aborted:
        ps.completed = True
        ps.completion_reason = "user_aborted"

    state.save_phase_state(ps)
    status_msg = ps.completion_reason or "in_progress"
    color = 'green' if ps.completion_reason == "all_stories_done" else 'yellow'
    click.echo(click.style(f"\n  Phase 'implement' (parallel={parallel}): {status_msg}", fg=color))
    click.echo(f"  Total cost: ${ps.total_cost_usd:.4f}")
    return ps
