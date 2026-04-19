from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from pralph import __version__
from pralph.compound import run_compound
from pralph.loop import run_add, run_ideate_loop, run_implement_loop, run_justloop, run_plan_loop, run_refine, run_stories_loop, run_webgen_loop
from pralph.viewer import run_viewer
from pralph.models import PhaseState, Story, StoryStatus
from pralph.state import ProjectNotInitializedError, StateManager
from pralph.report import (
    BUILTIN_QUERIES, gather_report_data, read_project_id, read_storage_backend,
    run_builtin_query,
    format_cost, format_csv, format_duration, format_json, format_table,
    print_report, build_report_json,
)


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


def _get_state(ctx: click.Context, *, readonly: bool = False) -> StateManager:
    """Create a StateManager, exiting with a message if project is not initialized."""
    try:
        return StateManager(ctx.obj["project_dir"], readonly=readonly, domains=ctx.obj.get("domains"))
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
        ("Replan", ["add", "ideate", "refine", "edit"]),
        ("Tools", ["justloop", "compound", "compact-index", "reset-errors", "viewer", "query"]),
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
@click.option("--domain", multiple=True, help="Override detected domains (repeatable, e.g. --domain rust --domain docker)")
@click.version_option(version=__version__)
@click.pass_context
def main(ctx, model, max_iterations, max_budget_usd, cooldown, verbose, project_dir, dangerously_skip_permissions, extra_tools, domain):
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
    ctx.obj["domains"] = list(domain) if domain else None

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

    # On first run, require --name or derive from directory name or prompt for it
    if not os.path.exists(config_path) and not name:
        # Default to directory name if stdin is not a TTY (non-interactive)
        if not sys.stdin.isatty():
            name = os.path.basename(project_dir)
            click.echo(f"  Auto-derived project name: {name}")
        else:
            name = click.prompt("Project name (used as ID across sessions)")

    state = StateManager(project_dir, project_name=name, domains=ctx.obj.get("domains"))
    if reset:
        _reset_phase(state, "plan")
    # If .pralph/plan-prompt.md exists, don't require a CLI prompt
    if not prompt and not prompt_file and state.read_phase_prompt("plan"):
        prompt = ""  # assembler will use plan-prompt.md
    else:
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
@click.option("--prompt-file", default=None, type=click.Path(), help="Read prompt from a file")
@click.option("--next", "is_next", is_flag=True, help="Priority 1 — implement next")
@click.option("--anytime", is_flag=True, default=False, help="Claude picks priority (default)")
@click.pass_context
def add(ctx, prompt, prompt_file, is_next, anytime):
    """Add a single story from an idea."""
    prompt = _resolve_prompt(prompt, "Idea", file_value=prompt_file)
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
@click.option("--prompt-file", default=None, type=click.Path(), help="Read prompt from a file")
@click.option("--story", "-s", "story_ids", multiple=True, help="Story ID(s) to refine")
@click.option("--pattern", "-p", "id_pattern", default=None, help="Glob pattern to match story IDs (e.g. 'I18N-*')")
@click.pass_context
def refine(ctx, instruction, prompt, prompt_file, story_ids, id_pattern):
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

    # Resolve instruction: positional arg > --prompt > --prompt-file > stdin > interactive
    if not instruction:
        instruction = _resolve_prompt(prompt, "Refinement instruction", file_value=prompt_file)
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
@click.argument("story_id")
@click.option("--title", default=None, help="New title")
@click.option("--content", default=None, help="New content/description")
@click.option("--priority", type=int, default=None, help="New priority (1=highest)")
@click.option("--category", default=None, help="New category")
@click.option("--complexity", default=None, help="New complexity (e.g. trivial, simple, medium, complex, large, epic)")
@click.option("--status", type=click.Choice([s.value for s in StoryStatus]), default=None, help="New status")
@click.option("--add-dep", multiple=True, help="Add dependency (story ID)")
@click.option("--remove-dep", multiple=True, help="Remove dependency (story ID)")
@click.option("--set-deps", default=None, help="Replace all dependencies (comma-separated IDs, or 'none')")
@click.option("--add-criteria", multiple=True, help="Add acceptance criterion")
@click.option("--remove-criteria", type=int, multiple=True, help="Remove acceptance criterion by index (0-based)")
@click.option("--set-criteria", default=None, help="Replace all criteria (semicolon-separated)")
@click.option("--delete", "do_delete", is_flag=True, help="Delete the story entirely")
@click.option("--show", is_flag=True, help="Show story details (no changes)")
@click.pass_context
def edit(ctx, story_id, title, content, priority, category, complexity, status,
         add_dep, remove_dep, set_deps, add_criteria, remove_criteria, set_criteria,
         do_delete, show):
    """Edit a story's fields directly (no AI involved).

    \b
    Examples:
      pralph edit STORY-001 --title "New title"
      pralph edit STORY-001 --priority 1 --status pending
      pralph edit STORY-001 --add-dep STORY-002 --add-criteria "Must handle errors"
      pralph edit STORY-001 --set-deps "STORY-002,STORY-003"
      pralph edit STORY-001 --set-criteria "Criterion 1;Criterion 2;Criterion 3"
      pralph edit STORY-001 --delete
      pralph edit STORY-001 --show
    """
    state = _get_state(ctx)
    stories = state.load_stories()
    stories_by_id = {s.id: s for s in stories}

    if story_id not in stories_by_id:
        click.echo(click.style(f"Error: story '{story_id}' not found", fg="red"))
        available = sorted(stories_by_id.keys())
        if available:
            click.echo(f"  Available: {', '.join(available[:20])}")
            if len(available) > 20:
                click.echo(f"  ... and {len(available) - 20} more")
        raise SystemExit(1)

    story = stories_by_id[story_id]

    # Show mode
    if show:
        click.echo(f"\n  {click.style(story.id, fg='blue', bold=True)}: {story.title}")
        click.echo(f"  Status:     {story.status.value}")
        click.echo(f"  Priority:   {story.priority}")
        click.echo(f"  Category:   {story.category}")
        click.echo(f"  Complexity: {story.complexity}")
        click.echo(f"  Source:     {story.source}")
        click.echo(f"  Deps:       {', '.join(story.dependencies) or 'none'}")
        click.echo(f"  Criteria:   {len(story.acceptance_criteria)} items")
        for i, ac in enumerate(story.acceptance_criteria):
            click.echo(f"    [{i}] {ac}")
        if story.content:
            click.echo(f"  Content:")
            for line in story.content.split("\n")[:10]:
                click.echo(f"    {line}")
            if story.content.count("\n") > 10:
                click.echo(f"    ... ({story.content.count(chr(10)) + 1} lines total)")
        click.echo()
        return

    # Delete mode
    if do_delete:
        confirm = click.confirm(f"  Delete story {story_id} ({story.title})?")
        if not confirm:
            click.echo("  Cancelled")
            return
        if state.delete_story(story_id):
            click.echo(click.style(f"  Deleted {story_id}", fg="green"))
        else:
            click.echo(click.style(f"  Failed to delete {story_id}", fg="red"))
        return

    # Collect changes
    changes = []

    if title is not None:
        story.title = title
        changes.append(f"title -> {title}")

    if content is not None:
        story.content = content
        changes.append(f"content -> ({len(content)} chars)")

    if priority is not None:
        story.priority = priority
        changes.append(f"priority -> {priority}")

    if category is not None:
        story.category = category
        changes.append(f"category -> {category}")

    if complexity is not None:
        story.complexity = complexity
        changes.append(f"complexity -> {complexity}")

    if status is not None:
        story.status = StoryStatus(status)
        changes.append(f"status -> {status}")

    # Dependencies
    if set_deps is not None:
        if set_deps.lower() == "none":
            story.dependencies = []
        else:
            story.dependencies = [d.strip() for d in set_deps.split(",") if d.strip()]
        changes.append(f"dependencies -> {story.dependencies}")
    else:
        if add_dep:
            for dep in add_dep:
                if dep not in story.dependencies:
                    story.dependencies.append(dep)
            changes.append(f"added deps: {list(add_dep)}")
        if remove_dep:
            story.dependencies = [d for d in story.dependencies if d not in remove_dep]
            changes.append(f"removed deps: {list(remove_dep)}")

    # Acceptance criteria
    if set_criteria is not None:
        story.acceptance_criteria = [c.strip() for c in set_criteria.split(";") if c.strip()]
        changes.append(f"criteria -> {len(story.acceptance_criteria)} items")
    else:
        if add_criteria:
            story.acceptance_criteria.extend(add_criteria)
            changes.append(f"added {len(add_criteria)} criteria")
        if remove_criteria:
            indices = sorted(set(remove_criteria), reverse=True)
            removed = 0
            for idx in indices:
                if 0 <= idx < len(story.acceptance_criteria):
                    story.acceptance_criteria.pop(idx)
                    removed += 1
            changes.append(f"removed {removed} criteria")

    if not changes:
        click.echo("  No changes specified. Use --show to view, or pass options like --title, --priority, etc.")
        click.echo("  Run 'pralph edit --help' for all options.")
        return

    state.update_story(story)
    click.echo(click.style(f"  Updated {story_id}:", fg="green"))
    for change in changes:
        click.echo(f"    {change}")


@main.command()
@click.option("--story-id", default=None, help="Implement specific stories (comma-separated IDs)")
@click.option("--with-deps", is_flag=True, help="Also implement unfinished upstream dependencies of --story-id")
@click.option("--phase1/--no-phase1", default=True, help="Architecture-first grouping")
@click.option("--review/--no-review", default=True, help="Run reviewer after each implementation")
@click.option("--compound/--no-compound", default=False, help="Capture learnings after each story (compound learning)")
@click.option("--prompt", default=None, help="Guidance for implementation (e.g. 'use FastAPI', 'use MCP for DB access')")
@click.option("--prompt-file", default=None, type=click.Path(), help="Read prompt from a file")
@click.option("--parallel", default=1, type=click.IntRange(min=1), help="Max concurrent stories (default: 1 = sequential)")
@click.option("--reset", is_flag=True, help="Reset phase state and start fresh")
@click.pass_context
def implement(ctx, story_id, with_deps, phase1, review, compound, prompt, prompt_file, parallel, reset):
    """Phase 3: Implement stories from backlog."""
    state = _get_state(ctx)
    if reset:
        _reset_phase(state, "implement")
    if with_deps and not story_id:
        raise click.UsageError("--with-deps requires --story-id")
    if not prompt and prompt_file:
        from pathlib import Path
        p = Path(prompt_file)
        if not p.exists():
            raise click.BadParameter(f"File not found: {prompt_file}", param_hint="'--prompt-file'")
        prompt = p.read_text().strip()
    prompt = prompt or _read_stdin() or ""
    # Parse comma-separated story IDs
    story_ids = [s.strip() for s in story_id.split(",") if s.strip()] if story_id else None

    save_global = state.global_compound
    click.echo(f"pralph implement — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {state.project_id}")
    click.echo(f"  model: {ctx.obj['model']}")
    click.echo(f"  review: {'on' if review else 'off'}")
    click.echo(f"  compound: {'on' if compound else 'off'}")
    if compound and save_global:
        domains = state.detect_domains()
        click.echo(f"  global: on (domains: {', '.join(domains) or 'none detected'})")
    if parallel > 1:
        click.echo(f"  parallel: {parallel}")
    if story_ids:
        click.echo(f"  stories: {', '.join(story_ids)}")
        if with_deps:
            click.echo(f"  with-deps: on")

    run_implement_loop(
        state,
        model=ctx.obj["model"],
        max_iterations=ctx.obj["max_iterations"],
        cooldown=ctx.obj["cooldown"],
        story_ids=story_ids,
        with_deps=with_deps,
        phase1=phase1,
        review=review,
        compound=compound,
        save_global=save_global,
        user_prompt=prompt,
        extra_tools=_get_extra_tools(ctx, state),
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
        parallel=parallel,
    )


@main.command()
@click.argument("prompt_args", nargs=-1)
@click.option("--prompt", default=None, help="Task prompt (prompted if omitted)")
@click.pass_context
def justloop(ctx, prompt_args, prompt):
    """Run a prompt in a loop until complete."""
    project_dir = ctx.obj["project_dir"]
    config_path = os.path.join(project_dir, ".pralph", "project.json")
    if not os.path.exists(config_path):
        name = os.path.basename(project_dir)
    else:
        name = None
    state = StateManager(project_dir, project_name=name, domains=ctx.obj.get("domains"))

    # Resolve prompt: positional args > --prompt > stdin > interactive
    if prompt_args:
        user_prompt = " ".join(prompt_args)
    elif prompt:
        user_prompt = prompt
    else:
        user_prompt = _resolve_prompt(None, "Task prompt")

    # Always start fresh — justloop is a standalone tool, not a resumable phase
    _reset_phase(state, "justloop")

    click.echo(f"pralph justloop — max {ctx.obj['max_iterations']} iterations")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {ctx.obj['model']}")
    click.echo(f"  prompt: {user_prompt}")

    run_justloop(
        state,
        user_prompt=user_prompt,
        model=ctx.obj["model"],
        max_iterations=ctx.obj["max_iterations"],
        cooldown=ctx.obj["cooldown"],
        extra_tools=_get_extra_tools(ctx, state),
        verbose=ctx.obj["verbose"],
        dangerously_skip_permissions=ctx.obj["dangerously_skip_permissions"],
        max_budget_usd=ctx.obj["max_budget_usd"],
    )


@main.command()
@click.option("--story-id", default=None, help="Story ID to capture learnings from")
@click.option("--prompt", default=None, help="Description of what was done")
@click.option("--prompt-file", default=None, type=click.Path(), help="Read prompt from a file")
@click.pass_context
def compound(ctx, story_id, prompt, prompt_file):
    """Capture learnings from recent work (compound learning)."""
    prompt = _resolve_prompt(prompt, "Description of work done", file_value=prompt_file)
    state = _get_state(ctx)
    save_global = state.global_compound
    click.echo(f"pralph compound")
    click.echo(f"  project: {state.project_id}")
    click.echo(f"  model: {ctx.obj['model']}")
    if save_global:
        domains = state.detect_domains()
        click.echo(f"  global: on (domains: {', '.join(domains) or 'none detected'})")
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
        save_global=save_global,
    )

    click.echo(f"\n  Cost: ${cost:.4f}")


@main.command("export-solutions")
@click.option("--output", "-o", default=None, type=click.Path(), help="Write to file (default: stdout)")
@click.option("--category", "-c", default=None, help="Filter by category")
@click.option("--format", "fmt", default="markdown", type=click.Choice(["markdown", "json"]), help="Output format")
@click.pass_context
def export_solutions(ctx, output, category, fmt):
    """Export compound learning solutions for reuse across projects."""
    import json as json_mod

    state = _get_state(ctx, readonly=True)
    entries = state.load_solutions_index()
    if not entries:
        click.echo("No solutions found. Run 'pralph implement --compound' or 'pralph compound' first.")
        return

    if category:
        entries = [e for e in entries if e.get("category", "").lower() == category.lower()]
        if not entries:
            click.echo(f"No solutions found in category '{category}'.")
            return

    if fmt == "json":
        items = []
        for entry in entries:
            content = state.read_solution(entry.get("filename", ""))
            items.append({**entry, "content": content})
        text = json_mod.dumps(items, indent=2)
    else:
        parts = []
        for entry in entries:
            content = state.read_solution(entry.get("filename", ""))
            title = entry.get("title", "Untitled")
            cat = entry.get("category", "")
            tags = ", ".join(entry.get("tags", []))
            header = f"# [{cat}] {title}"
            if tags:
                header += f"\n\nTags: {tags}"
            if content:
                parts.append(f"{header}\n\n{content}")
            else:
                parts.append(header)
        text = "\n\n---\n\n".join(parts) + "\n"

    if output:
        Path(output).write_text(text)
        click.echo(f"Exported {len(entries)} solution(s) to {output}")
    else:
        click.echo(text)


@main.command("compact-index")
@click.option("--global-only", is_flag=True, help="Only compact global indexes")
@click.option("--local-only", is_flag=True, help="Only compact project-local index")
@click.pass_context
def compact_index(ctx, global_only, local_only):
    """Compact solution indexes: merge duplicates via Haiku and prune orphans."""
    state = StateManager(ctx.obj["project_dir"], domains=ctx.obj.get("domains"))
    model = "haiku"
    verbose = ctx.obj.get("verbose", False)
    dangerously_skip_permissions = ctx.obj.get("dangerously_skip_permissions", False)
    click.echo(f"pralph compact-index")
    click.echo(f"  project: {ctx.obj['project_dir']}")
    click.echo(f"  model: {model}")

    compact_kwargs = dict(
        model=model,
        verbose=verbose,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )

    total_merged = 0
    total_removed = 0
    total_cost = 0.0

    # Local index
    if not global_only:
        if state.solutions_index_path.exists():
            stats = state.compact_local_index(**compact_kwargs)
            total_merged += stats["merged"]
            total_removed += stats["removed"]
            total_cost += stats.get("cost", 0.0)
            changed = stats["merged"] + stats["removed"]
            if changed:
                click.echo(click.style(f"  local: {stats['original']} → {stats['kept']}", fg='green')
                           + f" ({stats['merged']} merged, {stats['removed']} removed)")
            else:
                click.echo(f"  local: {stats['kept']} entries (clean)")
        else:
            click.echo("  local: no index")

    # Global indexes
    if not local_only:
        domains = state.detect_domains()
        if domains:
            results = state.compact_global_indexes(**compact_kwargs)
            if results:
                for stats in results:
                    domain = stats["domain"]
                    total_merged += stats["merged"]
                    total_removed += stats["removed"]
                    total_cost += stats.get("cost", 0.0)
                    changed = stats["merged"] + stats["removed"]
                    if changed:
                        click.echo(click.style(f"  global/{domain}: {stats['original']} → {stats['kept']}", fg='green')
                                   + f" ({stats['merged']} merged, {stats['removed']} removed)")
                    else:
                        click.echo(f"  global/{domain}: {stats['kept']} entries (clean)")
            else:
                click.echo("  global: no indexes found")
        else:
            click.echo("  global: no domains detected")

    total = total_merged + total_removed
    if total:
        click.echo(click.style(f"\n  Compacted: {total_merged} merged, {total_removed} removed", fg='green', bold=True))
    else:
        click.echo(click.style("\n  All indexes clean", fg='green'))
    if total_cost > 0:
        click.echo(f"  Cost: ${total_cost:.4f}")


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

    # Clear error fields in all phase states that have errors
    for phase_name in ("plan", "stories", "webgen", "ideate", "implement", "justloop"):
        ps = state.load_phase_state(phase_name)
        if ps.consecutive_errors > 0 or ps.last_error or ps.completion_reason in ("consecutive_errors", "error"):
            ps.consecutive_errors = 0
            ps.last_error = ""
            if ps.completion_reason in ("consecutive_errors", "error"):
                ps.completed = False
                ps.completion_reason = ""
            state.save_phase_state(ps)
            click.echo(f"  Cleared '{phase_name}' phase error state")

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
    """Query project data.

    Run built-in queries with flags (--progress, --cost, --stories, etc.)
    or pass arbitrary SQL as an argument (DuckDB backend only).

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

    def _output(columns: list[str], rows: list[tuple]) -> None:
        if fmt == "table":
            click.echo(format_table(columns, rows))
        elif fmt == "csv":
            click.echo(format_csv(columns, rows))
        else:
            click.echo(format_json(columns, rows))

    # Handle --report mode — works with both backends
    if report:
        try:
            state = StateManager(ctx.obj["project_dir"], readonly=True, domains=ctx.obj.get("domains"))
        except ProjectNotInitializedError as e:
            click.echo(click.style(str(e), fg="red"))
            return

        try:
            while True:
                state.refresh_readonly()
                data = gather_report_data(state)
                if watch and fmt != "json":
                    click.echo("\033[2J\033[H", nl=False)
                if fmt == "json":
                    click.echo(build_report_json(data))
                else:
                    print_report(data)
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
        selected = ["progress"]

    # Run built-in queries via StateManager (works with both backends)
    if selected:
        try:
            state = StateManager(ctx.obj["project_dir"], readonly=True, domains=ctx.obj.get("domains"))
        except ProjectNotInitializedError as e:
            click.echo(click.style(str(e), fg="red"))
            return

        for name in selected:
            label = BUILTIN_QUERIES[name][0]
            click.echo(click.style(f"\n  {label}", bold=True))
            click.echo()
            columns, rows = run_builtin_query(name, state)
            _output(columns, rows)

    # Run custom SQL (DuckDB only)
    if sql:
        backend = read_storage_backend(ctx.obj["project_dir"])
        if backend != "duckdb":
            click.echo(click.style(
                "Custom SQL requires the DuckDB backend.\n"
                "Set \"storage\": \"duckdb\" in .pralph/project.json to enable SQL queries.",
                fg="yellow",
            ))
            return

        from pralph import db
        click.echo()
        try:
            columns, rows = db.execute_query(sql)
            _output(columns, rows)
        except Exception as e:
            click.echo(click.style(f"Query error: {e}", fg="red"))
            project_id = ""
            try:
                project_id = read_project_id(ctx.obj["project_dir"])
            except (FileNotFoundError, ValueError):
                pass
            if not all_projects and project_id:
                click.echo(click.style(
                    f"\n  Hint: filter by project with WHERE project_id = '{project_id}'",
                    dim=True,
                ))
    click.echo()
