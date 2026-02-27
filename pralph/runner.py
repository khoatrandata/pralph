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
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


# Tools per phase
PLAN_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch,Edit,Write"
STORIES_TOOLS_EXTRACT = "Read,Glob,Grep"
STORIES_TOOLS_RESEARCH = "Read,Glob,Grep,WebSearch,WebFetch"
IMPLEMENT_TOOLS = "Read,Write,Edit,Bash,Glob,Grep"
REVIEW_TOOLS = "Read,Glob,Grep,Bash"
COMPOUND_TOOLS = "Read,Glob,Grep,Bash"
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
                sys.stderr.write(f"\r  \u23f3 running {m}:{s:02d}  \033[2m(ESC to interrupt)\033[0m")
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
    resume_session_id: str | None = None,
) -> ClaudeResult:
    """Invoke claude -p as a subprocess with streaming output.

    Monitors stdin for ESC key — on press, stops the subprocess (SIGSTOP) and
    presents an interrupt menu (continue / take over / skip / abort).
    """
    if resume_session_id:
        session_id = resume_session_id
        cmd = [
            "claude", "--resume", resume_session_id,
            "--verbose", "--output-format", "stream-json",
        ]
        if dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
    else:
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
    if not resume_session_id:
        proc.stdin.write(prompt)
    proc.stdin.close()

    # Read streaming NDJSON lines from stdout, printing progress as we go
    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_fd = proc.stdout.fileno()

    # Start ESC key monitor
    monitor_state = _start_esc_monitor(proc)

    # Start elapsed time counter
    start_time = time.monotonic()
    timer_stop = _start_elapsed_timer(start_time)

    final_result: dict | None = None
    deadline = time.monotonic() + timeout
    error_result: ClaudeResult | None = None
    buf = ""

    try:
        while True:
            # Check if interrupted by ESC (process is SIGSTOP'd)
            if monitor_state and monitor_state[0].is_set():
                _stop_elapsed_timer(timer_stop)
                _stop_esc_monitor(monitor_state)

                choice = _handle_interrupt(session_id, cwd)

                if choice == "continue":
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except OSError:
                        pass
                    timer_stop = _start_elapsed_timer(time.monotonic())
                    monitor_state = _start_esc_monitor(proc)
                    continue
                elif choice == "takeover":
                    import click as _click
                    proc.kill()
                    proc.wait()
                    _click.echo(_click.style("\n  Entering interactive mode...\n", fg="cyan", bold=True))
                    resume_interactive(session_id, cwd)
                    post = _post_takeover_menu(session_id)
                    if post == "resume":
                        return run_claude(
                            prompt,
                            resume_session_id=session_id,
                            dangerously_skip_permissions=dangerously_skip_permissions,
                            timeout=timeout,
                            verbose=verbose,
                            project_dir=project_dir,
                        )
                    elif post == "implemented":
                        return ClaudeResult(
                            success=True,
                            result=json.dumps({
                                "status": "implemented",
                                "summary": "Completed via interactive takeover",
                            }),
                            session_id=session_id,
                            interrupted=True,
                        )
                    elif post == "continue":
                        return ClaudeResult(
                            success=False, error="interrupted",
                            session_id=session_id, interrupted=True,
                        )
                    else:  # abort
                        return ClaudeResult(
                            success=False, error="aborted",
                            session_id=session_id, interrupted=True,
                        )
                elif choice == "skip":
                    proc.kill()
                    proc.wait()
                    return ClaudeResult(
                        success=False, error="interrupted",
                        session_id=session_id, interrupted=True,
                    )
                else:  # abort
                    proc.kill()
                    proc.wait()
                    return ClaudeResult(
                        success=False, error="aborted",
                        session_id=session_id, interrupted=True,
                    )

            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                with _timer_lock:
                    _clear_timer_line()
                error_result = ClaudeResult(
                    success=False, error="timeout", session_id=session_id,
                )
                break

            # Wait for data with timeout so we can check the interrupt flag
            ready, _, _ = select.select([stdout_fd], [], [], 0.3)

            if not ready:
                if proc.poll() is not None:
                    break  # process ended
                continue

            chunk = os.read(stdout_fd, 8192)
            if not chunk:
                break  # EOF

            buf += chunk.decode("utf-8", errors="replace")

            while "\n" in buf:
                line, buf = buf.split("\n", 1)
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

                if etype == "result":
                    final_result = event
                    result_text = event.get("result", "")
                    if result_text:
                        with _timer_lock:
                            _clear_timer_line()
                            print(f"\n{result_text}", file=sys.stderr)
                            _reset_timer()
                    continue

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
                            proc.send_signal(signal.SIGSTOP)
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


def _handle_interrupt(session_id: str, project_dir: str | None) -> str:
    """Show interrupt menu and return choice: 'continue', 'takeover', 'skip', or 'abort'."""
    import click

    # Flush any stale input from the ESC detection
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError):
        pass

    click.echo()
    click.echo(click.style("  ⏸  Interrupted", fg="yellow", bold=True))
    click.echo()
    click.echo("  [1] Continue  — resume where it left off")
    click.echo("  [2] Take over — open interactive session")
    click.echo("  [3] Skip      — continue to next iteration")
    click.echo("  [4] Abort     — stop the loop")
    click.echo()
    choice = click.prompt(
        "  Choice", type=click.Choice(["1", "2", "3", "4"]), default="1",
    )

    if choice == "1":
        return "continue"
    elif choice == "2":
        return "takeover"
    elif choice == "3":
        return "skip"
    else:
        return "abort"


def _post_takeover_menu(session_id: str) -> str:
    """Ask user about story status after interactive takeover.

    Returns: 'implemented', 'resume', 'continue', or 'abort'.
    """
    import click

    click.echo()
    click.echo(click.style("  Interactive session ended", fg="cyan", bold=True))
    click.echo()
    click.echo("  [1] Resume              — continue automated session")
    click.echo("  [2] Mark as implemented — story complete")
    click.echo("  [3] Continue loop       — story resets to pending")
    click.echo("  [4] Abort loop")
    click.echo()
    choice = click.prompt(
        "  Choice", type=click.Choice(["1", "2", "3", "4"]), default="1",
    )

    if choice == "1":
        return "resume"
    elif choice == "2":
        return "implemented"
    elif choice == "3":
        return "continue"
    else:
        return "abort"


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

    usage = data.get("usage", {})

    return ClaudeResult(
        success=is_success,
        result=data.get("result", ""),
        cost_usd=data.get("total_cost_usd", data.get("cost_usd", 0.0)),
        session_id=data.get("session_id", ""),
        error="" if is_success else data.get("error", subtype or "unknown"),
        is_rate_limit="rate" in data.get("error", "").lower(),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
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


# ── Parallel process support ─────────────────────────────────────────


class ProcessGroup:
    """Manages multiple concurrent claude subprocesses for parallel implementation.

    Tracks running processes by story ID, provides a single ESC monitor that
    SIGSTOPs all registered processes, and an interrupt menu for the group.
    """

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._interrupted = threading.Event()
        self._monitor_stop: threading.Event | None = None
        self._old_settings: list | None = None
        self.print_lock = threading.Lock()
        """Lock for thread-safe stderr/stdout writes from worker threads."""

    @property
    def is_interrupted(self) -> bool:
        return self._interrupted.is_set()

    def register(self, story_id: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[story_id] = proc

    def unregister(self, story_id: str) -> None:
        with self._lock:
            self._procs.pop(story_id, None)

    def start_monitor(self) -> None:
        """Start a single ESC monitor for all processes in the group."""
        if not _HAS_TERMIOS:
            return
        try:
            stdin_fd = sys.stdin.fileno()
        except (ValueError, AttributeError):
            return
        if not os.isatty(stdin_fd):
            return

        self._interrupted.clear()
        self._monitor_stop = threading.Event()
        self._old_settings = termios.tcgetattr(stdin_fd)
        tty.setcbreak(stdin_fd)

        stop = self._monitor_stop

        def monitor() -> None:
            try:
                while not stop.is_set():
                    ready, _, _ = select.select([stdin_fd], [], [], 0.2)
                    if stop.is_set():
                        return
                    if ready:
                        ch = os.read(stdin_fd, 1)
                        if ch == b"\x1b":
                            self._interrupted.set()
                            self._stop_all_procs()
                            return
            except (OSError, ValueError):
                pass

        t = threading.Thread(target=monitor, daemon=True)
        t.start()

    def stop_monitor(self) -> None:
        """Stop the ESC monitor and restore terminal settings."""
        if self._monitor_stop is not None:
            self._monitor_stop.set()
            self._monitor_stop = None
        if self._old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            except (OSError, ValueError):
                pass
            self._old_settings = None

    def _stop_all_procs(self) -> None:
        """SIGSTOP all registered processes."""
        with self._lock:
            for proc in self._procs.values():
                try:
                    proc.send_signal(signal.SIGSTOP)
                except OSError:
                    pass

    def resume_all(self) -> None:
        """SIGCONT all registered processes and clear interrupted state."""
        self._interrupted.clear()
        with self._lock:
            for proc in self._procs.values():
                try:
                    proc.send_signal(signal.SIGCONT)
                except OSError:
                    pass

    def kill_all(self) -> None:
        """Kill all registered processes."""
        with self._lock:
            for proc in self._procs.values():
                try:
                    proc.kill()
                except OSError:
                    pass
            for proc in self._procs.values():
                try:
                    proc.wait()
                except OSError:
                    pass
            self._procs.clear()


def handle_parallel_interrupt() -> str:
    """Show interrupt menu for parallel mode. Returns 'continue' or 'abort'."""
    import click

    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError):
        pass

    click.echo()
    click.echo(click.style("  \u23f8  Interrupted (parallel mode)", fg="yellow", bold=True))
    click.echo()
    click.echo("  [1] Continue all — resume all processes")
    click.echo("  [2] Abort all    — stop the loop")
    click.echo()
    choice = click.prompt(
        "  Choice", type=click.Choice(["1", "2"]), default="1",
    )

    return "continue" if choice == "1" else "abort"


def run_claude_parallel(
    prompt: str,
    *,
    story_id: str,
    process_group: ProcessGroup,
    model: str = "sonnet",
    allowed_tools: str = "",
    system_prompt: str = "",
    dangerously_skip_permissions: bool = False,
    max_budget_usd: float | None = None,
    timeout: int = 600,
    verbose: bool = False,
    project_dir: str | None = None,
) -> ClaudeResult:
    """Invoke claude -p as a subprocess for parallel mode.

    Registers/unregisters with ProcessGroup instead of managing its own ESC monitor.
    Prefixes output with [story_id]. No timer management (handled centrally).
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
    prefix = f"[{story_id}]"

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

    process_group.register(story_id, proc)

    assert proc.stdin is not None
    proc.stdin.write(prompt)
    proc.stdin.close()

    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_fd = proc.stdout.fileno()
    final_result: dict | None = None
    deadline = time.monotonic() + timeout
    error_result: ClaudeResult | None = None
    buf = ""

    try:
        while True:
            # Check if group was interrupted — process is already SIGSTOP'd
            if process_group.is_interrupted:
                # Wait for the group interrupt to be resolved (resume or abort)
                # The main thread handles the menu and calls resume_all or kill_all
                while process_group.is_interrupted:
                    time.sleep(0.3)
                    # If we were killed, check if process ended
                    if proc.poll() is not None:
                        break
                if proc.poll() is not None and final_result is None:
                    # We were killed via abort
                    process_group.unregister(story_id)
                    return ClaudeResult(
                        success=False, error="aborted",
                        session_id=session_id, interrupted=True,
                    )
                # Resumed — extend deadline
                deadline = time.monotonic() + timeout
                continue

            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                error_result = ClaudeResult(
                    success=False, error="timeout", session_id=session_id,
                )
                break

            ready, _, _ = select.select([stdout_fd], [], [], 0.3)

            if not ready:
                if proc.poll() is not None:
                    break
                continue

            chunk = os.read(stdout_fd, 8192)
            if not chunk:
                break

            buf += chunk.decode("utf-8", errors="replace")

            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    if verbose:
                        with process_group.print_lock:
                            print(f"  {prefix} [stream] unparseable: {line[:120]}", file=sys.stderr)
                    continue

                etype = event.get("type", "")

                if etype == "result":
                    final_result = event
                    result_text = event.get("result", "")
                    if result_text:
                        with process_group.print_lock:
                            print(f"\n  {prefix} {result_text}", file=sys.stderr)
                    continue

                # Print tool events with story prefix
                if etype == "tool_use":
                    tool = event.get("tool", event.get("name", "?"))
                    hint = _tool_hint(event.get("input", {}))
                    with process_group.print_lock:
                        print(f"  {prefix} \U0001f527 {tool}{hint}", file=sys.stderr)
                elif verbose:
                    with process_group.print_lock:
                        print(f"  {prefix} [{etype}] {str(event)[:120]}", file=sys.stderr)

    except Exception as e:
        proc.kill()
        proc.wait()
        error_result = ClaudeResult(
            success=False, error=f"stream read error: {e}", session_id=session_id,
        )
    finally:
        process_group.unregister(story_id)

    if error_result:
        return error_result

    proc.wait()

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


def run_with_retry_parallel(
    prompt: str,
    *,
    story_id: str,
    process_group: ProcessGroup,
    max_retries: int = 4,
    verbose: bool = False,
    **kwargs,
) -> ClaudeResult:
    """Run claude in parallel mode with exponential backoff on rate limits."""
    delays = [30, 60, 120, 240]
    last_result = None

    for attempt in range(max_retries + 1):
        result = run_claude_parallel(
            prompt,
            story_id=story_id,
            process_group=process_group,
            verbose=verbose,
            **kwargs,
        )
        last_result = result

        if result.success:
            return result

        if result.interrupted:
            return result

        if result.error == "timeout" and attempt == 0:
            original_timeout = kwargs.get("timeout", 600)
            kwargs["timeout"] = original_timeout * 2
            with process_group.print_lock:
                print(f"  [{story_id}] [retry] timeout — retrying with {kwargs['timeout']}s", file=sys.stderr)
            continue

        if result.is_rate_limit and attempt < max_retries:
            delay = delays[min(attempt, len(delays) - 1)]
            jitter = random.uniform(0, delay * 0.2)
            wait = delay + jitter
            with process_group.print_lock:
                print(f"  [{story_id}] [retry] rate limited — waiting {wait:.0f}s (attempt {attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
            continue

        break

    return last_result  # type: ignore[return-value]
