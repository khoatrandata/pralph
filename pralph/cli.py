from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from pralph import __version__
from pralph.loop import run_add, run_compound, run_ideate_loop, run_implement_loop, run_plan_loop, run_refine, run_stories_loop, run_webgen_loop
from pralph.viewer import run_viewer
from pralph.models import PhaseState, Story, StoryStatus
from pralph.state import ProjectNotInitializedError, StateManager
from pralph import db


def _read_stdin() -> str | None:
    """Read from stdin if it's piped (not a TTY). Returns None if interactive."""
    if not sys.stdin.isatty():
        return sys.stdin.read().strip() or None
    return None


def _resolve_prompt(flag_value: str | None, interactive_label: str, file_value: str | None = None) -> str:
    """Resolve prompt: flag > file > stdin > interactive prompt."""
    if flag_value:
        return flag_value
    if file_value:
        from pathlib import Path
        p = Path(file_value)
        if not p.exists():
            raise click.BadParameter(f"File not found: {file_value}", param_hint="'--prompt-file'")
        return p.read_text().strip()
    stdin = _read_stdin()
    if stdin:
        return stdin
    return click.prompt(interactive_label)


def _get_state(ctx: click.Context) -> StateManager:
    """Create a StateManager, exiting with a message if project is not initialized."""
    try:
        return StateManager(ctx.obj["project_dir"])
    except ProjectNotInitializedError as e:
        click.echo(click.style(str(e), fg="red"))
        raise SystemExit(1)


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
        ("Tools", ["compound", "reset-errors", "viewer", "query"]),
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
@click.option("--name", default=None, help="Project name (required on first run, e.g. 'myapp')")
@click.option("--prompt", default=None, help="Guidance for design doc creation")
@click.option("--prompt-file", default=None, type=click.Path(), help="Read prompt from a file")
@click.option("--reset", is_flag=True, help="Reset phase state and start fresh")
@click.pass_context
def plan(ctx, name, prompt, prompt_file, reset):
    """Phase 1: Create/refine a design document."""
    project_dir = ctx.obj["project_dir"]
    config_path = os.path.join(project_dir, ".pralph", "project.json")

    # On first run, require --name or prompt for it
    if not os.path.exists(config_path) and not name:
        name = click.prompt("Project name (used as ID across sessions)")

    state = StateManager(project_dir, project_name=name)
    if reset:
        _reset_phase(state, "plan")
    prompt = _resolve_prompt(prompt, "Design prompt", file_value=prompt_file)
    click.echo(f"pralph plan — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {state.project_id}")
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
    state = _get_state(ctx)
    if reset:
        _reset_phase(state, "stories")
    click.echo(f"pralph stories — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {state.project_id}")
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
    state = _get_state(ctx)
    if reset:
        _reset_phase(state, "webgen")
    click.echo(f"pralph webgen — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {state.project_id}")
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
    state = _get_state(ctx)
    click.echo(f"pralph add")
    click.echo(f"  project: {state.project_id}")
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
    state = _get_state(ctx)
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
    click.echo(f"  project: {state.project_id}")
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

    state = _get_state(ctx)
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
    click.echo(f"  project: {state.project_id}")
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
@click.option("--prompt-file", default=None, type=click.Path(), help="Read prompt from a file")
@click.option("--parallel", default=1, type=click.IntRange(min=1), help="Max concurrent stories (default: 1 = sequential)")
@click.option("--reset", is_flag=True, help="Reset phase state and start fresh")
@click.pass_context
def implement(ctx, story_id, phase1, review, compound, prompt, prompt_file, parallel, reset):
    """Phase 3: Implement stories from backlog."""
    state = _get_state(ctx)
    if reset:
        _reset_phase(state, "implement")
    if not prompt and prompt_file:
        from pathlib import Path
        p = Path(prompt_file)
        if not p.exists():
            raise click.BadParameter(f"File not found: {prompt_file}", param_hint="'--prompt-file'")
        prompt = p.read_text().strip()
    prompt = prompt or _read_stdin() or ""
    click.echo(f"pralph implement — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {state.project_id}")
    click.echo(f"  model: {ctx.obj['model']}")
    click.echo(f"  review: {'on' if review else 'off'}")
    click.echo(f"  compound: {'on' if compound else 'off'}")
    if parallel > 1:
        click.echo(f"  parallel: {parallel}")
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
        parallel=parallel,
    )


@main.command()
@click.option("--story-id", default=None, help="Story ID to capture learnings from")
@click.option("--prompt", default=None, help="Description of what was done")
@click.pass_context
def compound(ctx, story_id, prompt):
    """Capture learnings from recent work (compound learning)."""
    prompt = _resolve_prompt(prompt, "Description of work done")
    state = _get_state(ctx)
    click.echo(f"pralph compound")
    click.echo(f"  project: {state.project_id}")
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
    state = _get_state(ctx)

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

    # Clear error fields in the implement phase state
    ps = state.load_phase_state("implement")
    if ps.consecutive_errors > 0 or ps.last_error or ps.completion_reason in ("consecutive_errors", "error"):
        ps.consecutive_errors = 0
        ps.last_error = ""
        if ps.completion_reason in ("consecutive_errors", "error"):
            ps.completed = False
            ps.completion_reason = ""
        state.save_phase_state(ps)
        click.echo(f"  Cleared 'implement' phase error state")

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
    state = _get_state(ctx)
    stories = state.load_stories()
    if not stories:
        click.echo("No stories found. Run 'pralph stories' first.")
        return
    click.echo(f"  project: {state.project_id}")
    click.echo(f"  stories: {len(stories)}")
    run_viewer(state, port=port, open_browser=not no_open)


# ── Built-in queries for pralph query ────────────────────────────────

_BUILTIN_QUERIES = {
    "progress": (
        "Story progress by status",
        "SELECT status, COUNT(*) as count FROM stories WHERE project_id = ? GROUP BY status ORDER BY count DESC",
    ),
    "cost": (
        "Cost breakdown by phase",
        """SELECT phase,
                  COUNT(*) as iterations,
                  ROUND(SUM(cost_usd), 4) as total_cost,
                  SUM(input_tokens) as input_tokens,
                  SUM(output_tokens) as output_tokens
           FROM run_log WHERE project_id = ?
           GROUP BY phase ORDER BY total_cost DESC""",
    ),
    "stories": (
        "All stories",
        "SELECT id, title, status, priority, category, complexity FROM stories WHERE project_id = ? ORDER BY priority, id",
    ),
    "cost-per-story": (
        "Cost per story",
        """SELECT story_id, COUNT(*) as iterations,
                  ROUND(SUM(cost_usd), 4) as total_cost,
                  SUM(input_tokens) as input_tokens,
                  SUM(output_tokens) as output_tokens
           FROM run_log WHERE project_id = ? AND story_id != ''
           GROUP BY story_id ORDER BY total_cost DESC""",
    ),
    "errors": (
        "Recent errors",
        """SELECT iteration, phase, story_id, error, ROUND(duration, 1) as duration_s
           FROM run_log WHERE project_id = ? AND success = false AND error != ''
           ORDER BY logged_at DESC LIMIT 20""",
    ),
    "timeline": (
        "Implementation timeline",
        """SELECT story_id, phase, success, ROUND(cost_usd, 4) as cost, ROUND(duration, 1) as duration_s, logged_at
           FROM run_log WHERE project_id = ? AND story_id != ''
           ORDER BY logged_at""",
    ),
    "projects": (
        "All registered projects",
        "SELECT project_id, name, created_at FROM projects ORDER BY created_at DESC",
    ),
}


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    from datetime import timedelta
    td = timedelta(seconds=int(seconds))
    parts = []
    hours, remainder = divmod(td.seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if td.days:
        hours += td.days * 24
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs:
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"


def _format_cost(cost: float) -> str:
    return f"${cost:.2f}"


def _read_project_id(project_dir: str) -> str:
    """Read project_id from .pralph/project.json without opening a write connection."""
    import json
    config = Path(project_dir) / ".pralph" / "project.json"
    if not config.exists():
        raise click.ClickException(
            f"Project not initialized. Run 'pralph plan --name <project-name>' first.\n"
            f"  directory: {project_dir}"
        )
    data = json.loads(config.read_text())
    pid = data.get("project_id", "")
    if not pid:
        raise click.ClickException(f"project_id not set in {config}")
    return pid


def _gather_report_data(project_id: str) -> dict:
    """Gather all data needed for the progress report from DuckDB (read-only)."""
    conn = db.get_readonly_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM phase_state WHERE project_id = ? ORDER BY phase", [project_id]
        )
        cols = [d[0] for d in rows.description]
        phase_states = [dict(zip(cols, r)) for r in rows.fetchall()]

        current_phase = {}
        for ps in phase_states:
            if not ps.get("completed", False):
                current_phase = ps
                break
        if not current_phase and phase_states:
            current_phase = phase_states[-1]

        rows = conn.execute(
            "SELECT id, title, status, priority, category, complexity FROM stories WHERE project_id = ? ORDER BY priority, id",
            [project_id],
        )
        cols = [d[0] for d in rows.description]
        stories = {r[0]: dict(zip(cols, r)) for r in rows.fetchall()}

        rows = conn.execute(
            "SELECT status, COUNT(*) FROM stories WHERE project_id = ? GROUP BY status", [project_id]
        )
        status_counts = {r[0]: r[1] for r in rows.fetchall()}

        rows = conn.execute(
            """SELECT story_id,
                      COUNT(*) as iterations,
                      COALESCE(SUM(cost_usd), 0) as cost_usd,
                      COALESCE(SUM(duration), 0) as duration,
                      LIST(impl_status) as statuses
               FROM run_log
               WHERE project_id = ? AND story_id != '' AND mode = 'implement'
               GROUP BY story_id
               ORDER BY cost_usd DESC""",
            [project_id],
        )
        story_costs = {}
        for r in rows.fetchall():
            statuses = r[4] if r[4] else []
            story_costs[r[0]] = {
                "iterations": r[1],
                "cost_usd": r[2],
                "duration": r[3],
                "statuses": [s for s in statuses if s],
            }

        rows = conn.execute(
            "SELECT phase, COALESCE(SUM(cost_usd), 0) FROM run_log WHERE project_id = ? GROUP BY phase",
            [project_id],
        )
        phase_costs = {r[0]: r[1] for r in rows.fetchall()}

        row = conn.execute(
            "SELECT COALESCE(SUM(duration), 0) FROM run_log WHERE project_id = ?", [project_id]
        ).fetchone()
        total_duration = row[0] if row else 0.0

        active_story = current_phase.get("active_story_id", "") or None

        row = conn.execute(
            "SELECT phase, mode, story_id, impl_status FROM run_log WHERE project_id = ? ORDER BY logged_at DESC LIMIT 1",
            [project_id],
        ).fetchone()
        last_entry = None
        if row:
            last_entry = {"phase": row[0], "mode": row[1], "story_id": row[2], "impl_status": row[3]}
    finally:
        conn.close()

    return {
        "phase_states": phase_states,
        "current_phase": current_phase,
        "stories": stories,
        "story_costs": story_costs,
        "phase_costs": phase_costs,
        "status_counts": status_counts,
        "total_duration": total_duration,
        "active_story": active_story,
        "last_entry": last_entry,
    }


def _print_report(data: dict) -> None:
    """Print the combined progress report."""
    ps = data["current_phase"]
    if not ps:
        click.echo("No phase state found. Has pralph been run in this project?")
        return

    click.echo("=" * 60)
    click.echo("  PRALPH PROGRESS REPORT")
    click.echo("=" * 60)

    click.echo()
    for phase_data in data["phase_states"]:
        phase = phase_data.get("phase", "?")
        iteration = phase_data.get("current_iteration", 0)
        completed = phase_data.get("completed", False)
        cost = phase_data.get("total_cost_usd", 0.0)
        status_str = "COMPLETED" if completed else "running"
        if completed:
            reason = phase_data.get("completion_reason", "")
            if reason:
                status_str += f" ({reason})"
        click.echo(f"  {phase:<12} iter={iteration:<4} cost={_format_cost(cost):<10} {status_str}")

    last_summary = ps.get("last_summary", "")
    last_error = ps.get("last_error", "")
    if last_summary:
        click.echo(f"\n  Last result: {last_summary[:200]}")
    if last_error:
        click.echo(f"  Last error:  {last_error[:200]}")

    click.echo()
    click.echo("-" * 60)
    click.echo("  STORY SUMMARY")
    click.echo("-" * 60)
    counts = data["status_counts"]
    total_stories = sum(counts.values())
    click.echo(f"  Total stories: {total_stories}")
    click.echo()
    priority_order = ["implemented", "in_progress", "rework", "pending"]
    seen = set()
    for status in priority_order:
        if status in counts:
            click.echo(f"    {status:<20} {counts[status]:>3}")
            seen.add(status)
    for status, count in sorted(counts.items()):
        if status not in seen:
            click.echo(f"    {status:<20} {count:>3}")

    story_costs = data["story_costs"]
    if story_costs:
        click.echo()
        click.echo("-" * 60)
        click.echo("  COST PER STORY")
        click.echo("-" * 60)
        click.echo(f"  {'Story':<12} {'Description':<40} {'Status':<15} {'Cost':>8} {'Duration':>10} {'Iters':>5}")
        click.echo(f"  {chr(9472) * 12} {chr(9472) * 40} {chr(9472) * 15} {chr(9472) * 8} {chr(9472) * 10} {chr(9472) * 5}")

        for story_id, sc in story_costs.items():
            story_data = data["stories"].get(story_id, {})
            status = story_data.get("status", "unknown")
            title = story_data.get("title", "")
            if len(title) > 38:
                title = title[:35] + "..."
            final_status = sc["statuses"][-1] if sc["statuses"] else status
            click.echo(
                f"  {story_id:<12} {title:<40} {final_status:<15} {_format_cost(sc['cost_usd']):>8} "
                f"{_format_duration(sc['duration']):>10} {sc['iterations']:>5}"
            )

        impl_total = sum(sc["cost_usd"] for sc in story_costs.values())
        impl_duration = sum(sc["duration"] for sc in story_costs.values())
        click.echo(f"  {chr(9472) * 12} {chr(9472) * 40} {chr(9472) * 15} {chr(9472) * 8} {chr(9472) * 10} {chr(9472) * 5}")
        click.echo(
            f"  {'TOTAL':<12} {'':<40} {'':<15} {_format_cost(impl_total):>8} "
            f"{_format_duration(impl_duration):>10} {sum(sc['iterations'] for sc in story_costs.values()):>5}"
        )

    phase_costs = data["phase_costs"]
    if phase_costs:
        click.echo()
        click.echo("-" * 60)
        click.echo("  COST BY PHASE")
        click.echo("-" * 60)
        phase_order = ["plan", "stories", "webgen", "implement"]
        seen = set()
        for p in phase_order:
            if p in phase_costs:
                click.echo(f"    {p:<20} {_format_cost(phase_costs[p]):>10}")
                seen.add(p)
        for p, cost in sorted(phase_costs.items()):
            if p not in seen:
                click.echo(f"    {p:<20} {_format_cost(cost):>10}")
        grand_total = sum(phase_costs.values())
        click.echo(f"    {chr(9472) * 20} {chr(9472) * 10}")
        click.echo(f"    {'GRAND TOTAL':<20} {_format_cost(grand_total):>10}")

    click.echo()
    click.echo("-" * 60)
    click.echo("  CURRENTLY ACTIVE")
    click.echo("-" * 60)
    active = data["active_story"]
    if active:
        story_data = data["stories"].get(active, {})
        title = story_data.get("title", "")
        category = story_data.get("category", "")
        sc = data["story_costs"].get(active, {})
        cost_so_far = sc.get("cost_usd", 0.0) if sc else 0.0
        iters = sc.get("iterations", 0) if sc else 0
        click.echo(f"  Story:    {active}")
        if title:
            click.echo(f"  Title:    {title}")
        if category:
            click.echo(f"  Category: {category}")
        if cost_so_far > 0:
            click.echo(f"  Cost:     {_format_cost(cost_so_far)} ({iters} iterations)")
    elif data["current_phase"].get("completed"):
        click.echo("  All work completed.")
    else:
        last = data["last_entry"]
        if last:
            click.echo(f"  Phase: {last.get('phase', '?')}, Mode: {last.get('mode', '?')}")
        else:
            click.echo("  No activity recorded yet.")

    click.echo()
    click.echo("=" * 60)


def _build_report_json(data: dict) -> str:
    """Build the progress report as JSON."""
    import json as _json
    story_details = []
    for story_id, sc in data["story_costs"].items():
        story_data = data["stories"].get(story_id, {})
        story_details.append({
            "story_id": story_id,
            "title": story_data.get("title", ""),
            "status": story_data.get("status", "unknown"),
            "last_impl_status": sc["statuses"][-1] if sc["statuses"] else "",
            "cost_usd": round(sc["cost_usd"], 2),
            "duration_seconds": round(sc["duration"], 1),
            "iterations": sc["iterations"],
        })
    report = {
        "phase_states": data["phase_states"],
        "current_phase": data["current_phase"],
        "story_summary": data["status_counts"],
        "total_stories": sum(data["status_counts"].values()),
        "cost_by_phase": {k: round(v, 2) for k, v in data["phase_costs"].items()},
        "grand_total_cost": round(sum(data["phase_costs"].values()), 2),
        "total_duration_seconds": round(data["total_duration"], 1),
        "story_costs": story_details,
        "active_story": data["active_story"],
    }
    return _json.dumps(report, indent=2, default=str)


def _format_table(columns: list[str], rows: list[tuple]) -> str:
    """Format query results as an aligned text table."""
    if not rows:
        return "(no results)"
    str_rows = [[str(v) if v is not None else "" for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in str_rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    separator = "  ".join("-" * widths[i] for i in range(len(columns)))
    lines = [header, separator]
    for row in str_rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))))
    return "\n".join(lines)


def _format_csv(columns: list[str], rows: list[tuple]) -> str:
    """Format query results as CSV."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _format_json(columns: list[str], rows: list[tuple]) -> str:
    """Format query results as JSON."""
    import json
    data = [dict(zip(columns, row)) for row in rows]
    return json.dumps(data, indent=2, default=str)


@main.command("query")
@click.argument("sql", required=False)
@click.option("--progress", is_flag=True, help="Story progress by status")
@click.option("--cost", is_flag=True, help="Cost breakdown by phase")
@click.option("--stories", "show_stories", is_flag=True, help="List all stories with status")
@click.option("--cost-per-story", is_flag=True, help="Cost per story")
@click.option("--errors", is_flag=True, help="Recent errors")
@click.option("--timeline", is_flag=True, help="Implementation timeline")
@click.option("--projects", is_flag=True, help="All registered projects")
@click.option("--report", is_flag=True, help="Full progress report (phases, stories, costs, active work)")
@click.option("--watch", type=int, default=0, metavar="SECONDS", help="Auto-refresh every N seconds (use with --report)")
@click.option("--all-projects", is_flag=True, help="Include all projects in custom SQL")
@click.option("--format", "fmt", type=click.Choice(["table", "csv", "json"]), default="table", help="Output format")
@click.pass_context
def query_cmd(ctx, sql, progress, cost, show_stories, cost_per_story, errors, timeline, projects, report, watch, all_projects, fmt):
    """Query project data stored in DuckDB.

    Run built-in queries with flags (--progress, --cost, --stories, etc.)
    or pass arbitrary SQL as an argument.

    \b
    Examples:
      pralph query --progress
      pralph query --report
      pralph query --report --watch 10
      pralph query --cost --format json
      pralph query "SELECT * FROM stories WHERE priority = 1"
      pralph query --projects
    """
    import time

    # Handle --report mode (read-only — safe while pralph is running)
    if report:
        try:
            project_id = _read_project_id(ctx.obj["project_dir"])
        except (click.ClickException, FileNotFoundError) as e:
            click.echo(click.style(str(e), fg="red"))
            return

        try:
            while True:
                data = _gather_report_data(project_id)
                if watch:
                    click.echo("\033[2J\033[H", nl=False)
                if fmt == "json":
                    click.echo(_build_report_json(data))
                else:
                    _print_report(data)
                if watch <= 0:
                    break
                time.sleep(watch)
        except KeyboardInterrupt:
            click.echo()
        return

    # Determine which query to run
    builtin_flags = {
        "progress": progress,
        "cost": cost,
        "stories": show_stories,
        "cost-per-story": cost_per_story,
        "errors": errors,
        "timeline": timeline,
        "projects": projects,
    }

    selected = [name for name, flag in builtin_flags.items() if flag]

    if not selected and not sql:
        # Default: show progress
        selected = ["progress"]

    # Only resolve project_id if we need it (project-scoped queries)
    needs_project = any(name != "projects" for name in selected)
    project_id = ""
    if needs_project:
        try:
            project_id = _read_project_id(ctx.obj["project_dir"])
        except (click.ClickException, FileNotFoundError) as e:
            click.echo(click.style(str(e), fg="red"))
            return

    def _output(columns: list[str], rows: list[tuple]) -> None:
        if fmt == "table":
            click.echo(_format_table(columns, rows))
        elif fmt == "csv":
            click.echo(_format_csv(columns, rows))
        else:
            click.echo(_format_json(columns, rows))

    # Run built-in queries
    for name in selected:
        label, builtin_sql = _BUILTIN_QUERIES[name]
        click.echo(click.style(f"\n  {label}", bold=True))
        click.echo()
        if name == "projects":
            columns, rows = db.execute_query(builtin_sql)
        else:
            columns, rows = db.execute_query(builtin_sql, [project_id])
        _output(columns, rows)

    # Run custom SQL
    if sql:
        click.echo()
        try:
            columns, rows = db.execute_query(sql)
            _output(columns, rows)
        except Exception as e:
            click.echo(click.style(f"Query error: {e}", fg="red"))
            if not all_projects and project_id:
                click.echo(click.style(
                    f"\n  Hint: filter by project with WHERE project_id = '{project_id}'",
                    dim=True,
                ))
    click.echo()
