"""Parallel implementation — run multiple stories concurrently with dependency ordering."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import click

from pralph.assembler import assemble_implement_prompt
from pralph.compound import CompoundResult, run_compound_capture
from pralph.models import IterationResult, PhaseState, Story, StoryStatus
from pralph.parser import parse_implement_output
from pralph.review import ReviewResult, run_review
from pralph.runner import ClaudeResult, make_session_id, run_with_retry_parallel
from pralph.terminal import ProcessGroup, handle_parallel_interrupt
from pralph.state import StateManager

# Foundation categories to prioritize in implementation
FOUNDATION_CATEGORIES = frozenset({
    "FND", "DBM", "SEC", "ARC", "ADM", "DAT", "DEP", "SYS", "INF",
    "INFRA", "ARCH", "DB", "AUTH", "SETUP", "FOUNDATION",
})


def _token_kwargs(cr: ClaudeResult) -> dict:
    return {
        "input_tokens": cr.input_tokens,
        "output_tokens": cr.output_tokens,
        "cache_read_input_tokens": cr.cache_read_input_tokens,
        "cache_creation_input_tokens": cr.cache_creation_input_tokens,
    }


def sort_stories(stories: list[Story]) -> list[Story]:
    """Sort stories: foundation categories first -> priority -> dependency order."""

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


_DONE_STATUSES = frozenset({
    StoryStatus.implemented, StoryStatus.skipped,
    StoryStatus.duplicate, StoryStatus.external,
    StoryStatus.error,
})


def get_ready_stories(
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
    candidates = rework + sort_stories(pending)

    ready: list[Story] = []
    for s in candidates:
        if len(ready) >= max_count:
            break
        deps_met = all(dep_id in done_ids for dep_id in s.dependencies)
        if deps_met:
            ready.append(s)

    return ready


def run_parallel_implement(
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
    cost_lock = threading.Lock()
    aborted = False

    def _implement_one_story(story: Story) -> tuple[Story, ClaudeResult, dict]:
        """Worker function: implement a single story. Returns (story, claude_result, parsed)."""
        prompt = assemble_implement_prompt(state, story, phase_state=ps, user_prompt=user_prompt)
        sid = make_session_id(state.project_id, "implement", story.id, story.title)
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
            session_id=sid,
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
                    ready = get_ready_stories(state, in_flight, slots)
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
                    remaining = get_ready_stories(state, in_flight, 1)
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

                    with cost_lock:
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
                        review_result = run_review(
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
                        compound_result = run_compound_capture(
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
                    with cost_lock:
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
