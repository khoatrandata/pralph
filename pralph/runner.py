from __future__ import annotations

import json
import os
import random
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass

try:
    import termios
    import tty

    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False


@dataclass
class ClaudeResult:
    success: bool
    result: str = ""
    cost_usd: float = 0.0
    session_id: str = ""
    error: str = ""
    is_rate_limit: bool = False
    interrupted: bool = False


# Tools per phase
PLAN_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch,Edit,Write"
STORIES_TOOLS_EXTRACT = "Read,Glob,Grep"
STORIES_TOOLS_RESEARCH = "Read,Glob,Grep,WebSearch,WebFetch"
IMPLEMENT_TOOLS = "Read,Write,Edit,Bash,Glob,Grep"
REVIEW_TOOLS = "Read,Glob,Grep,Bash"
ADD_TOOLS = "Read,Glob,Grep"
IDEATE_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch"
REFINE_TOOLS = "Read,Glob,Grep"

# ── Elapsed timer ─────────────────────────────────────────────────────

_timer_lock = threading.Lock()
_timer_active = False


_timer_origin: list[float] = [0.0]  # mutable so timer thread sees resets


def _start_elapsed_timer(start_time: float) -> threading.Event:
    """Start a background thread that displays elapsed time on stderr."""
    global _timer_active
    _timer_active = False
    _timer_origin[0] = start_time
    stop = threading.Event()

    def _timer_fn():
        global _timer_active
        while not stop.is_set():
            with _timer_lock:
                elapsed = int(time.monotonic() - _timer_origin[0])
                m, s = divmod(elapsed, 60)
                sys.stderr.write(f"\r  \u23f3 running {m}:{s:02d}")
                sys.stderr.flush()
                _timer_active = True
            stop.wait(1.0)

    t = threading.Thread(target=_timer_fn, daemon=True)
    t.start()
    return stop


def _clear_timer_line():
    """Clear the in-place timer line before printing an event."""
    global _timer_active
    if _timer_active:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        _timer_active = False


def _reset_timer():
    """Reset the timer origin so it counts from now (call inside _timer_lock)."""
    _timer_origin[0] = time.monotonic()


def _stop_elapsed_timer(stop: threading.Event):
    """Stop the timer thread and clear the timer line."""
    stop.set()
    with _timer_lock:
        _clear_timer_line()


def run_claude(
    prompt: str,
    *,
    model: str = "sonnet",
    allowed_tools: str = "",
    system_prompt: str = "",
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
    timeout: int = 600,
    verbose: bool = False,
    project_dir: str | None = None,
) -> ClaudeResult:
    """Invoke claude -p as a subprocess with streaming output.

    Monitors stdin for ESC key — on press, kills the subprocess and presents
    an interrupt menu (take over interactively / skip / abort).
    """
    session_id = str(uuid.uuid4())
    cmd = [
        "claude", "-p", "--verbose", "--model", model,
        "--output-format", "stream-json", "--session-id", session_id,
    ]

    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])

    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    if dangerously_skip_permissions:
        cmd.append("--dangerously-skip-permissions")

    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])

    cwd = project_dir or None

    if verbose:
        _print_debug(cmd, prompt)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
    except FileNotFoundError:
        return ClaudeResult(
            success=False,
            error="claude CLI not found — install it: https://docs.anthropic.com/en/docs/claude-code",
        )

    # Send prompt on stdin, then close
    assert proc.stdin is not None
    proc.stdin.write(prompt)
    proc.stdin.close()

    # Read streaming NDJSON lines from stdout, printing progress as we go
    assert proc.stdout is not None
    assert proc.stderr is not None

    # Start ESC key monitor
    monitor_state = _start_esc_monitor(proc)

    # Start elapsed time counter
    start_time = time.monotonic()
    timer_stop = _start_elapsed_timer(start_time)

    final_result: dict | None = None
    deadline = time.monotonic() + timeout
    error_result: ClaudeResult | None = None

    try:
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                with _timer_lock:
                    _clear_timer_line()
                error_result = ClaudeResult(
                    success=False, error="timeout", session_id=session_id,
                )
                break

            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if verbose:
                    with _timer_lock:
                        _clear_timer_line()
                        print(f"  [stream] unparseable: {line[:120]}", file=sys.stderr)
                        _reset_timer()
                continue

            etype = event.get("type", "")

            # The final result event has the envelope we need
            if etype == "result":
                final_result = event
                # Print the final result text
                result_text = event.get("result", "")
                if result_text:
                    with _timer_lock:
                        _clear_timer_line()
                        print(f"\n{result_text}", file=sys.stderr)
                        _reset_timer()
                continue

            # Print progress for the user
            with _timer_lock:
                _clear_timer_line()
                _print_event(event, verbose)
                _reset_timer()

    except Exception as e:
        proc.kill()
        proc.wait()
        error_result = ClaudeResult(
            success=False, error=f"stream read error: {e}", session_id=session_id,
        )

    finally:
        _stop_elapsed_timer(timer_stop)
        _stop_esc_monitor(monitor_state)

    if error_result:
        return error_result

    proc.wait()

    # Check if interrupted by ESC
    if monitor_state and monitor_state[0].is_set():
        return _handle_interrupt(session_id, cwd)

    # Check stderr for errors
    stderr = proc.stderr.read().strip()

    if proc.returncode != 0 and final_result is None:
        is_rate = "rate" in stderr.lower() or "overloaded" in stderr.lower()
        return ClaudeResult(
            success=False,
            error=stderr or f"exit code {proc.returncode}",
            is_rate_limit=is_rate,
            session_id=session_id,
        )

    if final_result is None:
        return ClaudeResult(
            success=False, error="no result event in stream", session_id=session_id,
        )

    result = _parse_result_event(final_result)
    if not result.session_id:
        result.session_id = session_id
    return result


# ── ESC interrupt support ─────────────────────────────────────────────


def _start_esc_monitor(
    proc: subprocess.Popen,
) -> tuple[threading.Event, threading.Event, list] | None:
    """Start monitoring stdin for ESC key. Returns (interrupted, stop, old_settings) or None."""
    if not _HAS_TERMIOS:
        return None
    try:
        stdin_fd = sys.stdin.fileno()
    except (ValueError, AttributeError):
        return None
    if not os.isatty(stdin_fd):
        return None

    interrupted = threading.Event()
    stop = threading.Event()
    old_settings = termios.tcgetattr(stdin_fd)
    tty.setcbreak(stdin_fd)

    def monitor() -> None:
        try:
            while not stop.is_set():
                ready, _, _ = select.select([stdin_fd], [], [], 0.2)
                if stop.is_set():
                    return
                if ready:
                    ch = os.read(stdin_fd, 1)
                    if ch == b"\x1b":
                        interrupted.set()
                        try:
                            proc.send_signal(signal.SIGINT)
                        except OSError:
                            pass
                        return
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=monitor, daemon=True)
    t.start()
    return interrupted, stop, old_settings


def _stop_esc_monitor(
    monitor_state: tuple[threading.Event, threading.Event, list] | None,
) -> None:
    """Stop ESC monitor and restore terminal settings."""
    if monitor_state is None:
        return
    _, stop, old_settings = monitor_state
    stop.set()
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
    except (OSError, ValueError):
        pass


def _handle_interrupt(session_id: str, project_dir: str | None) -> ClaudeResult:
    """Show interrupt menu and handle user choice."""
    import click

    # Flush any stale input from the ESC detection
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError):
        pass

    click.echo()
    click.echo(click.style("  ⏸  Interrupted", fg="yellow", bold=True))
    click.echo()
    click.echo("  [1] Take over — resume session interactively")
    click.echo("  [2] Skip — continue to next iteration")
    click.echo("  [3] Abort — stop the loop")
    click.echo()
    choice = click.prompt(
        "  Choice", type=click.Choice(["1", "2", "3"]), default="1",
    )

    if choice == "1":
        click.echo(click.style("\n  Entering interactive mode...\n", fg="cyan", bold=True))
        resume_interactive(session_id, project_dir)
        return _post_takeover_menu(session_id)
    elif choice == "2":
        return ClaudeResult(
            success=False, error="interrupted",
            session_id=session_id, interrupted=True,
        )
    else:
        return ClaudeResult(
            success=False, error="aborted",
            session_id=session_id, interrupted=True,
        )


def _post_takeover_menu(session_id: str) -> ClaudeResult:
    """Ask user about story status after interactive takeover."""
    import click

    click.echo()
    click.echo(click.style("  Interactive session ended", fg="cyan", bold=True))
    click.echo()
    click.echo("  [1] Mark as implemented — story complete")
    click.echo("  [2] Continue loop — story resets to pending")
    click.echo("  [3] Abort loop")
    click.echo()
    choice = click.prompt(
        "  Choice", type=click.Choice(["1", "2", "3"]), default="2",
    )

    if choice == "1":
        return ClaudeResult(
            success=True,
            result=json.dumps({
                "status": "implemented",
                "summary": "Completed via interactive takeover",
            }),
            session_id=session_id,
            interrupted=True,
        )
    elif choice == "2":
        return ClaudeResult(
            success=False, error="interrupted",
            session_id=session_id, interrupted=True,
        )
    else:
        return ClaudeResult(
            success=False, error="aborted",
            session_id=session_id, interrupted=True,
        )


def resume_interactive(
    session_id: str,
    project_dir: str | None = None,
) -> int:
    """Resume a claude session interactively, inheriting the terminal."""
    cmd = ["claude", "--resume", session_id]
    return subprocess.call(cmd, cwd=project_dir)


def _parse_result_event(data: dict) -> ClaudeResult:
    """Parse the final 'result' event from the stream."""
    subtype = data.get("subtype", "")
    is_success = subtype in ("success", "error_max_turns")

    return ClaudeResult(
        success=is_success,
        result=data.get("result", ""),
        cost_usd=data.get("cost_usd", 0.0),
        session_id=data.get("session_id", ""),
        error="" if is_success else data.get("error", subtype or "unknown"),
        is_rate_limit="rate" in data.get("error", "").lower(),
    )


def _print_event(event: dict, verbose: bool) -> None:
    """Print a streaming event as compact progress."""
    import click as _click

    etype = event.get("type", "")

    if etype == "assistant":
        # message is the full API message object with content blocks
        msg = event.get("message", {})
        if isinstance(msg, dict):
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        _click.echo(text, err=True)
                elif isinstance(block, dict) and block.get("type") == "tool_use":
                    tool = block.get("name", "?")
                    inp = block.get("input", {})
                    hint = _tool_hint(inp)
                    _click.echo(_click.style(f"  🔧 {tool}", fg='cyan') + _click.style(hint, dim=True), err=True)

    elif etype == "tool_use":
        tool = event.get("tool", event.get("name", "?"))
        hint = _tool_hint(event.get("input", {}))
        _click.echo(_click.style(f"  🔧 {tool}", fg='cyan') + _click.style(hint, dim=True), err=True)

    elif etype == "tool_result":
        content = str(event.get("content", ""))
        if verbose:
            _click.echo(_click.style(f"  📋 {content}", dim=True), err=True)
        elif content:
            # Show a brief snippet by default
            first_line = content.split("\n")[0][:200]
            _click.echo(_click.style(f"  📋 {first_line}", dim=True), err=True)

    elif etype in ("system", "rate_limit_event"):
        pass  # skip noisy system events

    elif verbose:
        _click.echo(_click.style(f"  [{etype}] {str(event)[:120]}", dim=True), err=True)


def _tool_hint(tool_input: dict) -> str:
    """Extract a brief hint from tool input for display."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "pattern", "file_path", "query", "url", "old_string"):
        if key in tool_input:
            val = str(tool_input[key])[:80]
            return f" → {val}"
    return ""


def run_with_retry(
    prompt: str,
    *,
    max_retries: int = 4,
    verbose: bool = False,
    **kwargs,
) -> ClaudeResult:
    """Run claude with exponential backoff on rate limits."""
    delays = [30, 60, 120, 240]
    last_result = None

    for attempt in range(max_retries + 1):
        result = run_claude(prompt, verbose=verbose, **kwargs)
        last_result = result

        if result.success:
            return result

        if result.interrupted:
            return result

        if result.error == "timeout" and attempt == 0:
            original_timeout = kwargs.get("timeout", 600)
            kwargs["timeout"] = original_timeout * 2
            print(f"  [retry] timeout — retrying with {kwargs['timeout']}s", file=sys.stderr)
            continue

        if result.is_rate_limit and attempt < max_retries:
            delay = delays[min(attempt, len(delays) - 1)]
            jitter = random.uniform(0, delay * 0.2)
            wait = delay + jitter
            print(f"  [retry] rate limited — waiting {wait:.0f}s (attempt {attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
            continue

        # Non-retryable error
        break

    return last_result  # type: ignore[return-value]


def _print_debug(cmd: list[str], prompt: str) -> None:
    safe_cmd = " ".join(cmd)
    print(f"  [claude] {safe_cmd}", file=sys.stderr)
    print(f"  [prompt] {prompt}", file=sys.stderr)
