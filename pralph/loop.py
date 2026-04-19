from __future__ import annotations

import json
import random
import time
from datetime import datetime
from typing import Callable

import click

from pralph.assembler import (
    assemble_add_prompt,
    assemble_ideate_prompt,
    assemble_implement_prompt,
    assemble_justloop_prompt,
    assemble_phase1_analyze_prompt,
    assemble_plan_prompt,
    assemble_refine_prompt,
    assemble_stories_prompt,
    build_guardrails_system_prompt,
)
from pralph.compound import run_compound_capture
from pralph.models import IterationResult, PhaseState, Story, StoryStatus
from pralph.parallel import FOUNDATION_CATEGORIES, sort_stories, run_parallel_implement
from pralph.parser import (
    detect_completion_signal,
    detect_ideation_complete,
    detect_loop_complete,
    extract_json_from_text,
    parse_implement_output,
    parse_plan_output,
    parse_stories_output,
)
from pralph.review import run_review
from pralph.runner import (
    ADD_TOOLS,
    IDEATE_TOOLS,
    IMPLEMENT_TOOLS,
    JUSTLOOP_TOOLS,
    PLAN_TOOLS,
    REFINE_TOOLS,
    STORIES_TOOLS_EXTRACT,
    STORIES_TOOLS_RESEARCH,
    ClaudeResult,
    make_session_id,
    run_with_retry,
)
from pralph.terminal import resume_interactive
from pralph.state import StateManager


def _token_kwargs(cr: ClaudeResult) -> dict:
    """Extract token and session fields from a ClaudeResult as keyword arguments for IterationResult."""
    d: dict = {
        "input_tokens": cr.input_tokens,
        "output_tokens": cr.output_tokens,
        "cache_read_input_tokens": cr.cache_read_input_tokens,
        "cache_creation_input_tokens": cr.cache_creation_input_tokens,
    }
    if cr.session_id:
        d["session_id"] = cr.session_id
    return d


def _stamp_session(stories: list[Story], session_id: str) -> None:
    """Tag stories with the session_id that created them."""
    if not session_id:
        return
    for s in stories:
        s.metadata["session_id"] = session_id


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


def _suggest_compact(state: StateManager) -> None:
    """Print a hint to run compact-index if solutions exist."""
    if state.has_solutions() or state.has_global_solutions():
        click.echo(click.style("\n  Tip:", dim=True) + click.style(" run ", dim=True)
                   + click.style("pralph compact-index", fg='cyan')
                   + click.style(" to deduplicate & prune solution indexes", dim=True))


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
                if not result.session_id:
                    result.session_id = ps.active_session_id
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
    DONE_REASONS = {"generation_complete", "planning_complete", "all_stories_done", "single_story_done", "loop_complete"}

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
        if not result.session_id and ps.active_session_id:
            result.session_id = ps.active_session_id

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
            _suggest_compact(state)
            return ps

        state.save_phase_state(ps)

        if i < start_iter + max_iterations - 1:
            time.sleep(cooldown)

    ps.completed = True
    ps.completion_reason = "max_iterations"
    state.save_phase_state(ps)
    click.echo(click.style(f"\n  Phase '{phase}' complete: max iterations reached", fg='yellow'))
    _suggest_compact(state)
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

        sid = make_session_id(state.project_id, "plan")
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

        sid = make_session_id(state.project_id, "stories")
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
            _stamp_session(new_stories, result.session_id)
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
            _stamp_session(new_stories, result.session_id)
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

    _stamp_session([story], result.session_id)
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
    _stamp_session(stories, result.session_id)
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

        sid = make_session_id(state.project_id, "ideate")
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
            _stamp_session(new_stories, result.session_id)
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
            _stamp_session(new_stories, result.session_id)
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
        click.echo("Error: No stories found. Run 'pralph stories' before 'webgen'.")
        return PhaseState(phase="webgen", completed=True, completion_reason="no_stories")

    system_prompt = build_guardrails_system_prompt("stories", state)

    def iteration_fn(i: int, ps: PhaseState) -> IterationResult:
        prompt = assemble_stories_prompt(state, mode="webgen", phase_state=ps)

        sid = make_session_id(state.project_id, "webgen")
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
            _stamp_session(new_stories, result.session_id)
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
            _stamp_session(new_stories, result.session_id)
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


def _resolve_story_ids(
    state: StateManager,
    story_ids: list[str],
    with_deps: bool,
) -> list[str]:
    """Resolve story IDs, optionally including upstream dependencies.

    Returns IDs in dependency order (deps first), filtered to only
    pending/rework stories.
    """
    all_stories = state.load_stories()
    by_id = {s.id: s for s in all_stories}

    # Validate requested IDs exist
    missing = [sid for sid in story_ids if sid not in by_id]
    if missing:
        raise click.ClickException(f"Stories not found: {', '.join(missing)}")

    target_ids = set(story_ids)

    if with_deps:
        # Walk upstream: for each target, collect all transitive dependencies
        def collect_deps(sid: str, visited: set[str]) -> None:
            if sid in visited:
                return
            visited.add(sid)
            story = by_id.get(sid)
            if story:
                for dep_id in story.dependencies:
                    collect_deps(dep_id, visited)

        all_needed: set[str] = set()
        for sid in story_ids:
            collect_deps(sid, all_needed)
        target_ids = all_needed

    # Filter to only actionable stories (pending/rework)
    done_statuses = {StoryStatus.implemented, StoryStatus.skipped,
                     StoryStatus.duplicate, StoryStatus.external}
    actionable = [
        by_id[sid] for sid in target_ids
        if sid in by_id and by_id[sid].status not in done_statuses
    ]

    # Sort in dependency order
    ordered = sort_stories(actionable)
    return [s.id for s in ordered]


def run_implement_loop(
    state: StateManager,
    *,
    model: str = "sonnet",
    max_iterations: int = 50,
    cooldown: int = 5,
    story_ids: list[str] | None = None,
    with_deps: bool = False,
    phase1: bool = True,
    review: bool = True,
    compound: bool = False,
    save_global: bool = False,
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

    # If specific stories requested, resolve and implement them in order
    if story_ids:
        resolved = _resolve_story_ids(state, story_ids, with_deps)
        if not resolved:
            click.echo("  All requested stories (and dependencies) are already done.")
            return PhaseState(phase="implement", completed=True, completion_reason="all_stories_done")
        if with_deps and len(resolved) > len(story_ids):
            dep_ids = [sid for sid in resolved if sid not in set(story_ids)]
            click.echo(click.style(f"  Including {len(dep_ids)} dependencies:", fg='cyan'))
            for sid in dep_ids:
                click.echo(f"    {click.style(sid, fg='blue')}")
        click.echo(f"  Implementation order: {', '.join(resolved)}")
        last_ps = PhaseState(phase="implement")
        for sid in resolved:
            last_ps = _implement_single(
                state, sid, model=model, system_prompt=system_prompt,
                tools=tools, user_prompt=user_prompt, review=review,
                compound=compound, save_global=save_global, verbose=verbose,
                dangerously_skip_permissions=dangerously_skip_permissions,
                max_budget_usd=max_budget_usd,
            )
            # Stop on user abort/interrupt or error
            if last_ps.completion_reason in ("user_aborted", "user_interrupted", "error"):
                break
        last_ps.completion_reason = last_ps.completion_reason or "all_stories_done"
        return last_ps

    # Parallel mode: run up to N stories concurrently
    if parallel > 1:
        return run_parallel_implement(
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
            return rework + ordered + sort_stories(remaining)
        return rework + sort_stories(pending)

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
            if isinstance(reasoning, dict):
                for sid, reason in reasoning.items():
                    click.echo(f"    {click.style(sid, fg='blue')}: {reason[:80]}")
            elif reasoning:
                click.echo(f"    {str(reasoning)[:120]}")

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

        sid = make_session_id(state.project_id, "implement", story.id, story.title)
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
            review_result = run_review(
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
            compound_result = run_compound_capture(
                state, story,
                model=model,
                system_prompt=system_prompt,
                verbose=verbose,
                dangerously_skip_permissions=dangerously_skip_permissions,
                max_budget_usd=max_budget_usd,
                save_global=save_global,
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
            review_result = run_review(
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
    save_global: bool = False,
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
        review_result = run_review(
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
        run_compound_capture(
            state, story,
            model=model,
            system_prompt=system_prompt,
            verbose=verbose,
            dangerously_skip_permissions=dangerously_skip_permissions,
            max_budget_usd=max_budget_usd,
            save_global=save_global,
        )

    _suggest_compact(state)
    return PhaseState(phase="implement", completed=True, completion_reason="single_story_done")


# ── Justloop ─────────────────────────────────────────────────────────


def run_justloop(
    state: StateManager,
    *,
    user_prompt: str,
    model: str = "sonnet",
    max_iterations: int = 50,
    cooldown: int = 5,
    extra_tools: str = "",
    verbose: bool = False,
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
) -> PhaseState:
    """Run a simple prompt loop until completion."""
    import uuid

    system_prompt = build_guardrails_system_prompt("implement", state)
    tools = JUSTLOOP_TOOLS
    if extra_tools:
        tools = f"{tools},{extra_tools}"

    def iteration_fn(i: int, ps: PhaseState) -> IterationResult:
        prompt = assemble_justloop_prompt(state, user_prompt=user_prompt, phase_state=ps)

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
            timeout=1800,
            verbose=verbose,
            project_dir=str(state.project_dir),
            session_id=sid,
        )

        _clear_session_tracking(ps)

        if not result.success:
            return IterationResult(
                iteration=i, phase="justloop", mode="execute",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )

        return IterationResult(
            iteration=i, phase="justloop", mode="execute",
            success=True, raw_output=result.result,
            cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def resume_fn(session_id: str, ps: PhaseState) -> IterationResult:
        result = run_with_retry(
            "Continue where you left off.",
            resume_session_id=session_id,
            timeout=1800,
            project_dir=str(state.project_dir),
            dangerously_skip_permissions=dangerously_skip_permissions,
            verbose=verbose,
        )
        if not result.success:
            return IterationResult(
                iteration=ps.current_iteration, phase="justloop", mode="resume",
                success=False, error=result.error, cost_usd=result.cost_usd,
                **_token_kwargs(result),
            )
        return IterationResult(
            iteration=ps.current_iteration, phase="justloop", mode="resume",
            success=True, raw_output=result.result,
            cost_usd=result.cost_usd,
            **_token_kwargs(result),
        )

    def completion_fn(result: IterationResult, ps: PhaseState) -> bool:
        if result.raw_output and detect_loop_complete(result.raw_output):
            ps.completion_reason = "loop_complete"
            return True
        if ps.consecutive_errors >= 5:
            ps.completion_reason = "consecutive_errors"
            return True
        return False

    return _run_loop("justloop", state, max_iterations, cooldown, iteration_fn, completion_fn, resume_fn, verbose, dangerously_skip_permissions)
