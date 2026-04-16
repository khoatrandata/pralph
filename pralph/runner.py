"""Subprocess driver for invoking claude -p with streaming NDJSON output."""
from __future__ import annotations

import json
import os
import random
import select
import signal
import subprocess
import sys
import time
import re
import uuid
from dataclasses import dataclass

from pralph.terminal import (
    ProcessGroup,
    clear_timer_line,
    handle_interrupt,
    handle_parallel_interrupt,
    post_takeover_menu,
    reset_timer,
    resume_interactive,
    start_elapsed_timer,
    start_esc_monitor,
    stop_elapsed_timer,
    stop_esc_monitor,
    timer_lock,
)


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
JUSTLOOP_TOOLS = "Read,Write,Edit,Bash,Glob,Grep"

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def make_session_id(project_id: str, phase: str, story_id: str = "", title: str = "") -> str:
    """Build a valid UUID session ID for Claude Code.

    Uses uuid5 with a descriptive name so the ID is both a valid UUID and
    traceable back to the project/phase/story context via the namespace.
    A random suffix ensures uniqueness across iterations.
    """
    parts = [project_id, phase]
    if story_id:
        parts.append(story_id)
    if title:
        parts.append(title[:40])
    parts.append(uuid.uuid4().hex[:8])  # random suffix for uniqueness
    name = "-".join(parts)
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


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
    session_id: str | None = None,
) -> ClaudeResult:
    """Invoke claude -p as a subprocess with streaming output.

    Monitors stdin for ESC key — on press, stops the subprocess (SIGSTOP) and
    presents an interrupt menu (continue / take over / skip / abort).
    """
    if resume_session_id:
        session_id = resume_session_id
        cmd = [
            "claude", "-p", "--resume", resume_session_id,
            "--verbose", "--output-format", "stream-json",
        ]
        if dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
    else:
        session_id = session_id or str(uuid.uuid4())
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
    if prompt:
        proc.stdin.write(prompt)
    proc.stdin.close()

    # Read streaming NDJSON lines from stdout, printing progress as we go
    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_fd = proc.stdout.fileno()

    # Start ESC key monitor
    monitor_state = start_esc_monitor(proc)

    # Start elapsed time counter
    start_time = time.monotonic()
    timer_stop = start_elapsed_timer(start_time)

    final_result: dict | None = None
    all_assistant_text: list[str] = []  # accumulate all assistant text blocks
    deadline = time.monotonic() + timeout
    error_result: ClaudeResult | None = None
    buf = ""

    try:
        while True:
            # Check if interrupted by ESC (process is SIGSTOP'd)
            if monitor_state and monitor_state[0].is_set():
                stop_elapsed_timer(timer_stop)
                tty_file = monitor_state[3]
                stop_esc_monitor(monitor_state)

                choice, verbose = handle_interrupt(session_id, cwd, tty_file=tty_file, verbose=verbose)

                # Close /dev/tty if we opened it; a new one is opened if we continue
                if tty_file is not None:
                    try:
                        tty_file.close()
                    except OSError:
                        pass

                if choice == "continue":
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except OSError:
                        pass
                    timer_stop = start_elapsed_timer(time.monotonic())
                    monitor_state = start_esc_monitor(proc)
                    continue
                elif choice == "takeover":
                    import click as _click
                    proc.kill()
                    proc.wait()
                    _click.echo(_click.style("\n  Entering interactive mode...\n", fg="cyan", bold=True))
                    resume_interactive(session_id, cwd, dangerously_skip_permissions)
                    post = post_takeover_menu(session_id)
                    if post == "resume":
                        return run_claude(
                            "Continue where you left off.",
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
                with timer_lock:
                    clear_timer_line()
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
                        with timer_lock:
                            clear_timer_line()
                            print(f"  [stream] unparseable: {line[:120]}", file=sys.stderr)
                            reset_timer()
                    continue

                etype = event.get("type", "")

                if etype == "result":
                    final_result = event
                    result_text = event.get("result", "")
                    if result_text:
                        with timer_lock:
                            clear_timer_line()
                            print(f"\n{result_text}", file=sys.stderr)
                            reset_timer()
                    continue

                # Accumulate all assistant text for fallback parsing
                if etype == "assistant":
                    msg = event.get("message", {})
                    if isinstance(msg, dict):
                        for block in msg.get("content", []):
                            if isinstance(block, dict) and block.get("type") == "text":
                                t = block.get("text", "")
                                if t:
                                    all_assistant_text.append(t)

                with timer_lock:
                    clear_timer_line()
                    _print_event(event, verbose)
                    reset_timer()

    except Exception as e:
        proc.kill()
        proc.wait()
        error_result = ClaudeResult(
            success=False, error=f"stream read error: {e}", session_id=session_id,
        )

    finally:
        stop_elapsed_timer(timer_stop)
        stop_esc_monitor(monitor_state)
        if monitor_state is not None and monitor_state[3] is not None:
            try:
                monitor_state[3].close()
            except OSError:
                pass

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
    # If the result event had no text but we captured assistant text during
    # streaming, use the accumulated text so parsers can find structured output
    # that appeared in earlier assistant turns (e.g. before a tool call).
    if not result.result and all_assistant_text:
        result.result = "\n\n".join(all_assistant_text)
    return result


# ── Stream parsing helpers ────────────────────────────────────────────


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

    elif etype == "user":
        # Tool result coming back from a tool call
        output = ""
        tool_result = event.get("tool_use_result")
        if isinstance(tool_result, dict):
            stdout = tool_result.get("stdout", "")
            stderr = tool_result.get("stderr", "")
            output = stderr if stderr else stdout
        if not output:
            # Fallback: extract content from message.content tool_result blocks
            msg = event.get("message", {})
            if isinstance(msg, dict):
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        c = block.get("content", "")
                        if isinstance(c, str) and c:
                            output = c
                            break
        if output:
            if verbose:
                _click.echo(_click.style(f"  📋 {output}", dim=True), err=True)
            else:
                lines = output.strip().split("\n")
                preview = lines[0]
                if len(lines) > 1:
                    preview += _click.style(f" (+{len(lines)-1} lines)", dim=True)
                _click.echo(_click.style(f"  📋 {preview}", dim=True), err=True)

    elif etype == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        status = info.get("status", "unknown")
        if status != "allowed":
            limit_type = info.get("rateLimitType", "")
            resets_at = info.get("resetsAt", "")
            _click.echo(_click.style(f"  ⚠ rate limited ({limit_type}, resets {resets_at})", fg='yellow'), err=True)

    elif etype == "system":
        if verbose:
            subtype = event.get("subtype", "")
            model = event.get("model", "")
            cwd = event.get("cwd", "")
            version = event.get("claude_code_version", "")
            parts = [s for s in [subtype, model, cwd, version] if s]
            _click.echo(_click.style(f"  [system] {' | '.join(parts)}", dim=True), err=True)

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


# ── Retry wrappers ────────────────────────────────────────────────────


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
            kwargs["timeout"] = min(original_timeout * 2, 3600)
            kwargs.pop("session_id", None)  # avoid "already in use" on retry
            print(f"  [retry] timeout — retrying with {kwargs['timeout']}s", file=sys.stderr)
            continue

        if result.is_rate_limit and attempt < max_retries:
            delay = delays[min(attempt, len(delays) - 1)]
            jitter = random.uniform(0, delay * 0.2)
            wait = delay + jitter
            kwargs.pop("session_id", None)  # avoid "already in use" on retry
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


# ── Parallel subprocess execution ─────────────────────────────────────


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
    session_id: str | None = None,
) -> ClaudeResult:
    """Invoke claude -p as a subprocess for parallel mode.

    Registers/unregisters with ProcessGroup instead of managing its own ESC monitor.
    Prefixes output with [story_id]. No timer management (handled centrally).
    """
    session_id = session_id or uuid.uuid4().hex[:8]
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
            kwargs["timeout"] = min(original_timeout * 2, 3600)
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
