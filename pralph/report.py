"""Shared reporting logic used by both the CLI and the viewer."""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from pathlib import Path


BUILTIN_QUERIES = {
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
        """SELECT iteration, phase, story_id, session_id, error, ROUND(duration, 1) as duration_s
           FROM run_log WHERE project_id = ? AND success = false AND error != ''
           ORDER BY logged_at DESC LIMIT 20""",
    ),
    "timeline": (
        "Implementation timeline",
        """SELECT story_id, phase, success, session_id, ROUND(cost_usd, 4) as cost, ROUND(duration, 1) as duration_s, logged_at
           FROM run_log WHERE project_id = ? AND story_id != ''
           ORDER BY logged_at""",
    ),
    "sessions": (
        "Session history",
        """SELECT session_id, phase, story_id, COUNT(*) as iterations,
                  ROUND(SUM(cost_usd), 4) as total_cost,
                  MIN(logged_at) as started, MAX(logged_at) as ended
           FROM run_log WHERE project_id = ? AND session_id != ''
           GROUP BY session_id, phase, story_id
           ORDER BY started""",
    ),
    "projects": (
        "All registered projects",
        "SELECT project_id, name, created_at FROM projects ORDER BY created_at DESC",
    ),
}


def read_project_id(project_dir: str) -> str:
    """Read project_id from .pralph/project.json without opening a write connection."""
    config = Path(project_dir) / ".pralph" / "project.json"
    if not config.exists():
        raise FileNotFoundError(
            f"Project not initialized. Run 'pralph plan --name <project-name>' first.\n"
            f"  directory: {project_dir}"
        )
    data = json.loads(config.read_text())
    pid = data.get("project_id", "")
    if not pid:
        raise ValueError(f"project_id not set in {config}")
    return pid


def read_storage_backend(project_dir: str) -> str:
    """Read storage backend from .pralph/project.json."""
    config = Path(project_dir) / ".pralph" / "project.json"
    if config.exists():
        try:
            data = json.loads(config.read_text())
            if "storage" in data:
                return data["storage"]
            # Existing project without storage key — was using DuckDB
            return "duckdb"
        except (json.JSONDecodeError, OSError):
            pass
    return "jsonl"


def run_builtin_query(name: str, state) -> tuple[list[str], list[tuple]]:
    """Run a named built-in query against a StateManager, returning (columns, rows).

    Works with both JSONL and DuckDB backends.
    """
    if name == "progress":
        stories = state.load_stories()
        counts: dict[str, int] = defaultdict(int)
        for s in stories:
            counts[s.status.value] += 1
        columns = ["status", "count"]
        rows = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return columns, rows

    if name == "cost":
        entries = state.load_run_log()
        agg: dict[str, dict] = defaultdict(lambda: {"iterations": 0, "total_cost": 0.0, "input_tokens": 0, "output_tokens": 0})
        for e in entries:
            phase = e.get("phase", "")
            if not phase:
                continue
            a = agg[phase]
            a["iterations"] += 1
            a["total_cost"] += e.get("cost_usd", 0.0)
            a["input_tokens"] += e.get("input_tokens", 0)
            a["output_tokens"] += e.get("output_tokens", 0)
        columns = ["phase", "iterations", "total_cost", "input_tokens", "output_tokens"]
        rows = [(p, a["iterations"], round(a["total_cost"], 4), a["input_tokens"], a["output_tokens"])
                for p, a in sorted(agg.items(), key=lambda x: x[1]["total_cost"], reverse=True)]
        return columns, rows

    if name == "stories":
        stories = state.load_stories()
        columns = ["id", "title", "status", "priority", "category", "complexity"]
        rows = [(s.id, s.title, s.status.value, s.priority, s.category, s.complexity)
                for s in sorted(stories, key=lambda s: (s.priority, s.id))]
        return columns, rows

    if name == "cost-per-story":
        entries = state.load_run_log()
        agg = defaultdict(lambda: {"iterations": 0, "total_cost": 0.0, "input_tokens": 0, "output_tokens": 0})
        for e in entries:
            sid = e.get("story_id", "")
            if not sid:
                continue
            a = agg[sid]
            a["iterations"] += 1
            a["total_cost"] += e.get("cost_usd", 0.0)
            a["input_tokens"] += e.get("input_tokens", 0)
            a["output_tokens"] += e.get("output_tokens", 0)
        columns = ["story_id", "iterations", "total_cost", "input_tokens", "output_tokens"]
        rows = [(sid, a["iterations"], round(a["total_cost"], 4), a["input_tokens"], a["output_tokens"])
                for sid, a in sorted(agg.items(), key=lambda x: x[1]["total_cost"], reverse=True)]
        return columns, rows

    if name == "errors":
        entries = state.load_run_log()
        error_entries = [e for e in entries if not e.get("success", True) and e.get("error", "")]
        columns = ["iteration", "phase", "story_id", "session_id", "error", "duration_s"]
        rows = [(e.get("iteration", 0), e.get("phase", ""), e.get("story_id", ""),
                 e.get("session_id", ""), e.get("error", ""), round(e.get("duration", 0.0), 1))
                for e in reversed(error_entries[-20:])]
        return columns, rows

    if name == "timeline":
        entries = state.load_run_log()
        story_entries = [e for e in entries if e.get("story_id", "")]
        columns = ["story_id", "phase", "success", "session_id", "cost", "duration_s", "logged_at"]
        rows = [(e.get("story_id", ""), e.get("phase", ""), e.get("success", False),
                 e.get("session_id", ""), round(e.get("cost_usd", 0.0), 4),
                 round(e.get("duration", 0.0), 1), e.get("logged_at", ""))
                for e in story_entries]
        return columns, rows

    if name == "sessions":
        entries = state.load_run_log()
        agg: dict[str, dict] = {}
        for e in entries:
            sid = e.get("session_id", "")
            if not sid:
                continue
            if sid not in agg:
                agg[sid] = {"phase": e.get("phase", ""), "story_id": e.get("story_id", ""),
                            "iterations": 0, "total_cost": 0.0,
                            "started": e.get("logged_at", ""), "ended": e.get("logged_at", "")}
            a = agg[sid]
            a["iterations"] += 1
            a["total_cost"] += e.get("cost_usd", 0.0)
            a["ended"] = e.get("logged_at", "")
        columns = ["session_id", "phase", "story_id", "iterations", "total_cost", "started", "ended"]
        rows = [(sid, a["phase"], a["story_id"], a["iterations"], round(a["total_cost"], 4),
                 a["started"], a["ended"]) for sid, a in agg.items()]
        return columns, rows

    if name == "projects":
        columns = ["project_id", "name", "created_at"]
        try:
            from pralph import db
            conn = db.get_readonly_connection()
            try:
                result = conn.execute("SELECT project_id, name, created_at FROM projects ORDER BY created_at DESC")
                rows = [tuple(r) for r in result.fetchall()]
            finally:
                conn.close()
        except Exception:
            rows = [(state.project_id, state.project_id, "")]
        return columns, rows

    return [], []


def gather_report_data(state) -> dict:
    """Gather all data needed for the progress report.

    Accepts a StateManager instance (either JSONL or DuckDB backend).
    Returns a dict consumable by both the CLI printer and the viewer API.
    """
    phase_states = state.load_all_phase_states()

    current_phase = {}
    for ps in phase_states:
        if not ps.get("completed", False):
            current_phase = ps
            break
    if not current_phase and phase_states:
        current_phase = phase_states[-1]

    stories_list = state.load_stories()
    stories = {s.id: {"id": s.id, "title": s.title, "status": s.status.value,
                       "priority": s.priority, "category": s.category, "complexity": s.complexity}
               for s in stories_list}

    status_counts: dict[str, int] = defaultdict(int)
    for s in stories_list:
        status_counts[s.status.value] += 1

    run_log = state.load_run_log()

    # Aggregate story costs from run_log
    story_agg: dict[str, dict] = defaultdict(lambda: {"iterations": 0, "cost_usd": 0.0, "duration": 0.0, "statuses": []})
    for entry in run_log:
        sid = entry.get("story_id", "")
        if not sid or entry.get("mode") != "implement":
            continue
        agg = story_agg[sid]
        agg["iterations"] += 1
        agg["cost_usd"] += entry.get("cost_usd", 0.0)
        agg["duration"] += entry.get("duration", 0.0)
        impl_status = entry.get("impl_status", "")
        if impl_status:
            agg["statuses"].append(impl_status)
    story_costs = dict(story_agg)

    # Phase costs
    phase_costs: dict[str, float] = defaultdict(float)
    total_duration = 0.0
    for entry in run_log:
        phase = entry.get("phase", "")
        if phase:
            phase_costs[phase] += entry.get("cost_usd", 0.0)
        total_duration += entry.get("duration", 0.0)
    phase_costs = dict(phase_costs)

    active_story = current_phase.get("active_story_id", "") or None

    # Last run_log entry
    last_entry = None
    if run_log:
        last = run_log[-1]
        last_entry = {"phase": last.get("phase", ""), "mode": last.get("mode", ""),
                      "story_id": last.get("story_id", ""), "impl_status": last.get("impl_status", "")}

    # Cost projection
    implemented = status_counts.get("implemented", 0)
    pending = status_counts.get("pending", 0) + status_counts.get("rework", 0)
    avg_cost = sum(sc["cost_usd"] for sc in story_costs.values()) / max(implemented, 1) if story_costs else 0
    avg_duration = sum(sc["duration"] for sc in story_costs.values()) / max(implemented, 1) if story_costs else 0

    return {
        "phase_states": phase_states,
        "current_phase": current_phase,
        "stories": stories,
        "story_costs": story_costs,
        "phase_costs": phase_costs,
        "status_counts": dict(status_counts),
        "total_duration": total_duration,
        "active_story": active_story,
        "last_entry": last_entry,
        "grand_total_cost": round(sum(phase_costs.values()), 4),
        "projection": {
            "implemented": implemented,
            "remaining": pending,
            "avg_cost_per_story": round(avg_cost, 4),
            "avg_duration_per_story": round(avg_duration, 1),
            "estimated_remaining_cost": round(avg_cost * pending, 2),
            "estimated_remaining_duration": round(avg_duration * pending, 1),
        },
    }


# -- formatting helpers --

def format_duration(seconds: float) -> str:
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


def format_cost(cost: float) -> str:
    return f"${cost:.2f}"


def format_table(columns: list[str], rows: list[tuple]) -> str:
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


def format_csv(columns: list[str], rows: list[tuple]) -> str:
    """Format query results as CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def format_json(columns: list[str], rows: list[tuple]) -> str:
    """Format query results as JSON."""
    data = [dict(zip(columns, row)) for row in rows]
    return json.dumps(data, indent=2, default=str)


# -- CLI report printers --

def print_report(data: dict) -> None:
    """Print the combined progress report to stdout via click."""
    import click

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
        click.echo(f"  {phase:<12} iter={iteration:<4} cost={format_cost(cost):<10} {status_str}")

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
                f"  {story_id:<12} {title:<40} {final_status:<15} {format_cost(sc['cost_usd']):>8} "
                f"{format_duration(sc['duration']):>10} {sc['iterations']:>5}"
            )

        impl_total = sum(sc["cost_usd"] for sc in story_costs.values())
        impl_duration = sum(sc["duration"] for sc in story_costs.values())
        click.echo(f"  {chr(9472) * 12} {chr(9472) * 40} {chr(9472) * 15} {chr(9472) * 8} {chr(9472) * 10} {chr(9472) * 5}")
        click.echo(
            f"  {'TOTAL':<12} {'':<40} {'':<15} {format_cost(impl_total):>8} "
            f"{format_duration(impl_duration):>10} {sum(sc['iterations'] for sc in story_costs.values()):>5}"
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
                click.echo(f"    {p:<20} {format_cost(phase_costs[p]):>10}")
                seen.add(p)
        for p, cost in sorted(phase_costs.items()):
            if p not in seen:
                click.echo(f"    {p:<20} {format_cost(cost):>10}")
        grand_total = sum(phase_costs.values())
        click.echo(f"    {chr(9472) * 20} {chr(9472) * 10}")
        click.echo(f"    {'GRAND TOTAL':<20} {format_cost(grand_total):>10}")

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
            click.echo(f"  Cost:     {format_cost(cost_so_far)} ({iters} iterations)")
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


def build_report_json(data: dict) -> str:
    """Build the progress report as JSON."""
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
    return json.dumps(report, indent=2, default=str)
