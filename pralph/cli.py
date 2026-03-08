from __future__ import annotations

import os
import sys

import click

from pralph import __version__
from pralph.loop import run_add, run_compound, run_ideate_loop, run_implement_loop, run_plan_loop, run_refine, run_stories_loop, run_webgen_loop
from pralph.viewer import run_viewer
from pralph.models import PhaseState, Story, StoryStatus
from pralph.state import StateManager


def _read_stdin() -> str | None:
    """Read from stdin if it's piped (not a TTY). Returns None if interactive."""
    if not sys.stdin.isatty():
        return sys.stdin.read().strip() or None
    return None


def _resolve_prompt(flag_value: str | None, interactive_label: str) -> str:
    """Resolve prompt: flag > stdin > interactive prompt."""
    if flag_value:
        return flag_value
    stdin = _read_stdin()
    if stdin:
        return stdin
    return click.prompt(interactive_label)


def _reset_phase(state: StateManager, phase: str) -> None:
    """Reset a phase's state so it runs from scratch."""
    state.save_phase_state(PhaseState(phase=phase))
    click.echo(f"  Reset phase '{phase}'")


def _get_extra_tools(ctx: click.Context, state: StateManager) -> str:
    """Merge CLI --extra-tools with project-level .pralph/extra-tools.txt."""
    cli_tools = ctx.obj["extra_tools_cli"]
    project_tools = state.read_extra_tools()
    parts = [p for p in [project_tools, cli_tools] if p]
    return ",".join(parts)


class OrderedGroup(click.Group):
    """Click group that shows commands in defined order with section headers."""

    SECTIONS = [
        ("Workflow", ["plan", "stories", "webgen", "implement"]),
        ("Replan", ["add", "ideate", "refine"]),
        ("Tools", ["compound", "reset-errors", "viewer"]),
    ]

    def list_commands(self, ctx):
        return list(self.commands)

    def format_commands(self, ctx, formatter):
        for section_name, cmd_names in self.SECTIONS:
            rows = []
            for name in cmd_names:
                cmd = self.commands.get(name)
                if cmd is None:
                    continue
                help_text = cmd.get_short_help_str(limit=formatter.width)
                rows.append((name, help_text))
            if rows:
                with formatter.section(f"{section_name} Commands"):
                    formatter.write_dl(rows)


@click.group(cls=OrderedGroup, invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--model", default="opus", help="Model alias or full name")
@click.option("--max-iterations", default=50, type=int, help="Max loop iterations")
@click.option("--max-budget-usd", default=None, type=float, help="Max $ per claude invocation")
@click.option("--cooldown", default=5, type=int, help="Seconds between iterations")
@click.option("--verbose", is_flag=True, help="Show full claude output")
@click.option("--project-dir", default=None, type=click.Path(exists=True), help="Target project dir [default: cwd]")
@click.option("--dangerously-skip-permissions", is_flag=True, help="Pass permission bypass to claude")
@click.option("--extra-tools", default=None, help="Additional tools to allow (comma-separated, e.g. mcp__db__query,mcp__api__call)")
@click.version_option(version=__version__)
@click.pass_context
def main(ctx, model, max_iterations, max_budget_usd, cooldown, verbose, project_dir, dangerously_skip_permissions, extra_tools):
    """pralph — Planned Ralph: multi-phase development workflow powered by Claude Code."""
    ctx.ensure_object(dict)
    ctx.obj["model"] = model
    ctx.obj["max_iterations"] = max_iterations
    ctx.obj["max_budget_usd"] = max_budget_usd
    ctx.obj["cooldown"] = cooldown
    ctx.obj["verbose"] = verbose
    ctx.obj["project_dir"] = project_dir or os.getcwd()
    ctx.obj["dangerously_skip_permissions"] = dangerously_skip_permissions
    ctx.obj["extra_tools_cli"] = extra_tools or ""

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("--prompt", default=None, help="Guidance for design doc creation")
@click.option("--reset", is_flag=True, help="Reset phase state and start fresh")
@click.pass_context
def plan(ctx, prompt, reset):
    """Phase 1: Create/refine a design document."""
    state = StateManager(ctx.obj["project_dir"])
    if reset:
        _reset_phase(state, "plan")
    prompt = _resolve_prompt(prompt, "Design prompt")
    click.echo(f"pralph plan — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")

    run_plan_loop(
        state,
        model=ctx.obj["model"],
        max_iterations=ctx.obj["max_iterations"],
        cooldown=ctx.obj["cooldown"],
        user_prompt=prompt,
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )


@main.command()
@click.option("--extract-weight", default=80, type=int, help="Extract vs research weight (0-100)")
@click.option("--reset", is_flag=True, help="Reset phase state and start fresh")
@click.pass_context
def stories(ctx, extract_weight, reset):
    """Phase 2: Extract user stories from design doc."""
    state = StateManager(ctx.obj["project_dir"])
    if reset:
        _reset_phase(state, "stories")
    click.echo(f"pralph stories — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")
    click.echo(f"  extract_weight: {extract_weight}%")

    run_stories_loop(
        state,
        model=ctx.obj["model"],
        max_iterations=ctx.obj["max_iterations"],
        cooldown=ctx.obj["cooldown"],
        extract_weight=extract_weight,
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )


@main.command()
@click.option("--reset", is_flag=True, help="Reset phase state and start fresh")
@click.pass_context
def webgen(ctx, reset):
    """Phase 2b: Discover missing requirements via web research."""
    state = StateManager(ctx.obj["project_dir"])
    if reset:
        _reset_phase(state, "webgen")
    click.echo(f"pralph webgen — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")

    run_webgen_loop(
        state,
        model=ctx.obj["model"],
        max_iterations=ctx.obj["max_iterations"],
        cooldown=ctx.obj["cooldown"],
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )


@main.command()
@click.option("--prompt", default=None, help="Brief idea to turn into a story (prompted if omitted)")
@click.option("--next", "is_next", is_flag=True, help="Priority 1 — implement next")
@click.option("--anytime", is_flag=True, default=False, help="Claude picks priority (default)")
@click.pass_context
def add(ctx, prompt, is_next, anytime):
    """Add a single story from an idea."""
    prompt = _resolve_prompt(prompt, "Idea")
    state = StateManager(ctx.obj["project_dir"])
    click.echo(f"pralph add")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")
    click.echo(f"  priority: {'next (1)' if is_next else 'claude picks'}")
    click.echo(f"  idea: {prompt}")

    story = run_add(
        state,
        idea=prompt,
        is_next=is_next,
        model=ctx.obj["model"],
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )

    if story:
        click.echo()
        click.echo(click.style("  Story created:", fg='green', bold=True))
        click.echo(f"    ID:         {click.style(story.id, fg='blue')}")
        click.echo(f"    Title:      {story.title}")
        click.echo(f"    Priority:   {story.priority}")
        click.echo(f"    Category:   {story.category}")
        click.echo(f"    Complexity: {story.complexity}")
        click.echo(f"    Deps:       {', '.join(story.dependencies) or 'none'}")
        click.echo(f"    Criteria:   {len(story.acceptance_criteria)} items")
        for ac in story.acceptance_criteria:
            click.echo(f"      - {ac}")
    else:
        click.echo(click.style("\n  Failed to create story", fg='red'))


@main.command()
@click.argument("ideas_args", nargs=-1)
@click.option("--ideas-file", default=None, type=click.Path(), help="Path to ideas file [default: .pralph/ideas.md]")
@click.option("--prompt", default=None, help="Ideas as inline text")
@click.option("--reset", is_flag=True, help="Reset phase state and start fresh")
@click.pass_context
def ideate(ctx, ideas_args, ideas_file, prompt, reset):
    """Process a batch of ideas into stories.

    Ideas can be passed as arguments: pralph ideate "add dark mode" "CSV export"
    """
    state = StateManager(ctx.obj["project_dir"])
    if reset:
        _reset_phase(state, "ideate")

    # Resolve ideas: args > --prompt > --ideas-file > default ideas.md > stdin > interactive
    if ideas_args:
        ideas_text = "\n".join(f"- {a}" for a in ideas_args)
    elif prompt:
        ideas_text = prompt
    elif ideas_file:
        from pathlib import Path
        p = Path(ideas_file)
        if not p.exists():
            click.echo(f"Error: Ideas file not found: {ideas_file}")
            return
        ideas_text = p.read_text()
    elif state.ideas_path.exists():
        ideas_text = state.ideas_path.read_text()
    else:
        stdin = _read_stdin()
        if stdin:
            ideas_text = stdin
        else:
            click.echo("Enter ideas (one per line, blank line to finish):")
            lines = []
            while True:
                line = click.prompt("", default="", prompt_suffix="  ", show_default=False)
                if not line:
                    break
                lines.append(line)
            if not lines:
                click.echo("Error: No ideas provided")
                return
            ideas_text = "\n".join(lines)

    ideas_text = ideas_text.strip()
    if not ideas_text:
        click.echo("Error: Ideas text is empty")
        return

    click.echo(f"pralph ideate — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")
    click.echo(f"  ideas: {len(ideas_text)} chars")

    run_ideate_loop(
        state,
        ideas_text=ideas_text,
        model=ctx.obj["model"],
        max_iterations=ctx.obj["max_iterations"],
        cooldown=ctx.obj["cooldown"],
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )


@main.command()
@click.argument("instruction", required=False)
@click.option("--prompt", default=None, help="Refinement instruction")
@click.option("--story", "-s", "story_ids", multiple=True, help="Story ID(s) to refine")
@click.option("--pattern", "-p", "id_pattern", default=None, help="Glob pattern to match story IDs (e.g. 'I18N-*')")
@click.pass_context
def refine(ctx, instruction, prompt, story_ids, id_pattern):
    """Refine existing stories: split, merge, or rewrite."""
    import fnmatch

    state = StateManager(ctx.obj["project_dir"])
    all_stories = state.load_stories()
    stories_by_id = {s.id: s for s in all_stories}

    # Resolve target stories from --story IDs + --pattern glob
    selected: list[Story] = []
    seen: set[str] = set()

    for sid in story_ids:
        if sid in stories_by_id:
            if sid not in seen:
                selected.append(stories_by_id[sid])
                seen.add(sid)
        else:
            click.echo(click.style(f"  Warning: story '{sid}' not found, skipping", fg='yellow'))

    if id_pattern:
        for s in all_stories:
            if fnmatch.fnmatch(s.id, id_pattern) and s.id not in seen:
                selected.append(s)
                seen.add(s.id)

    if not selected:
        click.echo(click.style("Error: no stories matched. Use -s STORY_ID or -p 'PATTERN-*'", fg='red'))
        return

    # Warn about non-actionable statuses
    for s in selected:
        if s.status not in (StoryStatus.pending, StoryStatus.rework, StoryStatus.error):
            click.echo(click.style(f"  Warning: {s.id} has status '{s.status.value}'", fg='yellow'))

    # Resolve instruction: positional arg > --prompt > stdin > interactive
    if not instruction:
        if prompt:
            instruction = prompt
        else:
            instruction = _read_stdin() or click.prompt("Refinement instruction")
    if not instruction.strip():
        click.echo(click.style("Error: instruction is empty", fg='red'))
        return

    click.echo(f"pralph refine")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")
    click.echo(f"  stories: {', '.join(s.id for s in selected)}")
    click.echo(f"  instruction: {instruction}")

    new_stories = run_refine(
        state,
        instruction=instruction,
        original_stories=selected,
        model=ctx.obj["model"],
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )

    if new_stories:
        click.echo()
        click.echo(click.style(f"  {len(new_stories)} replacement stories created:", fg='green', bold=True))
        for s in new_stories:
            click.echo(f"    {click.style(s.id, fg='blue')}: {s.title}")
        click.echo()
        click.echo(click.style(f"  {len(selected)} original stories marked as skipped:", fg='yellow'))
        for s in selected:
            click.echo(f"    {click.style(s.id, dim=True)}: {s.title}")
    else:
        click.echo(click.style("\n  Failed to refine stories", fg='red'))


@main.command()
@click.option("--story-id", default=None, help="Implement a specific story")
@click.option("--phase1/--no-phase1", default=True, help="Architecture-first grouping")
@click.option("--review/--no-review", default=True, help="Run reviewer after each implementation")
@click.option("--compound/--no-compound", default=False, help="Capture learnings after each story (compound learning)")
@click.option("--prompt", default=None, help="Guidance for implementation (e.g. 'use FastAPI', 'use MCP for DB access')")
@click.option("--reset", is_flag=True, help="Reset phase state and start fresh")
@click.pass_context
def implement(ctx, story_id, phase1, review, compound, prompt, reset):
    """Phase 3: Implement stories from backlog."""
    state = StateManager(ctx.obj["project_dir"])
    if reset:
        _reset_phase(state, "implement")
    prompt = prompt or _read_stdin() or ""
    click.echo(f"pralph implement — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")
    click.echo(f"  review: {'on' if review else 'off'}")
    click.echo(f"  compound: {'on' if compound else 'off'}")
    if story_id:
        click.echo(f"  story: {story_id}")

    run_implement_loop(
        state,
        model=ctx.obj["model"],
        max_iterations=ctx.obj["max_iterations"],
        cooldown=ctx.obj["cooldown"],
        story_id=story_id,
        phase1=phase1,
        review=review,
        compound=compound,
        user_prompt=prompt,
        extra_tools=_get_extra_tools(ctx, state),
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )


@main.command()
@click.option("--story-id", default=None, help="Story ID to capture learnings from")
@click.option("--prompt", default=None, help="Description of what was done")
@click.pass_context
def compound(ctx, story_id, prompt):
    """Capture learnings from recent work (compound learning)."""
    prompt = _resolve_prompt(prompt, "Description of work done")
    state = StateManager(ctx.obj["project_dir"])
    click.echo(f"pralph compound")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")
    if story_id:
        click.echo(f"  story: {story_id}")
    if prompt:
        click.echo(f"  description: {prompt[:80]}")

    cost = run_compound(
        state,
        story_id=story_id,
        description=prompt,
        model=ctx.obj["model"],
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )

    click.echo(f"\n  Cost: ${cost:.4f}")


@main.command("reset-errors")
@click.pass_context
def reset_errors(ctx):
    """Reset error stories to pending and clear error state."""
    state = StateManager(ctx.obj["project_dir"])

    # Show error details before resetting
    error_stories = [s for s in state.load_stories() if s.status == StoryStatus.error]
    if error_stories:
        click.echo(click.style(f"  {len(error_stories)} stories in error state:", fg='red'))
        for s in error_stories:
            reason = s.metadata.get("error_reason", "(no reason captured)")
            error_at = s.metadata.get("error_at", "")
            click.echo(f"    {click.style(s.id, fg='blue')}: {s.title}")
            click.echo(f"      Reason: {reason[:200]}")
            if error_at:
                click.echo(f"      Error at: {error_at}")

    # Reset error stories back to pending
    reset_stories = state.reset_error_stories()

    # Clear error fields in whichever phase is currently stored
    if state.phase_state_path.exists():
        try:
            import json
            data = json.loads(state.phase_state_path.read_text())
            phase = data.get("phase", "implement")
        except (json.JSONDecodeError, KeyError):
            phase = "implement"
        ps = state.load_phase_state(phase)
        if ps.consecutive_errors > 0 or ps.last_error or ps.completion_reason in ("consecutive_errors", "error"):
            ps.consecutive_errors = 0
            ps.last_error = ""
            if ps.completion_reason in ("consecutive_errors", "error"):
                ps.completed = False
                ps.completion_reason = ""
            state.save_phase_state(ps)
            click.echo(f"  Cleared '{phase}' phase error state")

    if reset_stories:
        click.echo(click.style(f"  Reset {len(reset_stories)} stories to pending", fg='green'))
    else:
        click.echo("  No error stories found")


@main.command()
@click.option("--port", default=8411, type=int, help="Port to serve on")
@click.option("--no-open", is_flag=True, help="Don't auto-open browser")
@click.pass_context
def viewer(ctx, port, no_open):
    """Browse and review user stories in a web UI."""
    state = StateManager(ctx.obj["project_dir"])
    stories = state.load_stories()
    if not stories:
        click.echo("No stories found. Run 'pralph stories' first.")
        return
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  stories: {len(stories)}")
    run_viewer(state, port=port, open_browser=not no_open)
