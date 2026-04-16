"""Terminal interaction — ESC interrupt monitors, elapsed timer, interactive resume, and process groups."""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading

try:
    import termios
    import tty

    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False


# ── Elapsed timer ─────────────────────────────────────────────────────

import time

_timer_lock = threading.Lock()
_timer_active = False
_timer_origin: list[float] = [0.0]  # mutable so timer thread sees resets


def start_elapsed_timer(start_time: float) -> threading.Event:
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


def clear_timer_line():
    """Clear the in-place timer line before printing an event."""
    global _timer_active
    if _timer_active:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        _timer_active = False


def reset_timer():
    """Reset the timer origin so it counts from now (call inside timer_lock)."""
    _timer_origin[0] = time.monotonic()


def stop_elapsed_timer(stop: threading.Event):
    """Stop the timer thread and clear the timer line."""
    stop.set()
    with _timer_lock:
        clear_timer_line()


# Expose lock for external callers that need synchronized output
timer_lock = _timer_lock


# ── ESC interrupt support ─────────────────────────────────────────────


def start_esc_monitor(
    proc: subprocess.Popen,
) -> tuple[threading.Event, threading.Event, list, "io.BufferedReader | None"] | None:
    """Start monitoring stdin for ESC key.

    Returns (interrupted, stop, old_settings, tty_file) or None.
    tty_file is non-None when /dev/tty was opened because stdin is not a TTY.
    """
    if not HAS_TERMIOS:
        return None

    tty_file = None
    try:
        stdin_fd = sys.stdin.fileno()
    except (ValueError, AttributeError):
        return None

    if os.isatty(stdin_fd):
        input_fd = stdin_fd
    else:
        try:
            tty_file = open("/dev/tty", "rb", buffering=0)  # noqa: SIM115
            input_fd = tty_file.fileno()
        except OSError:
            return None

    interrupted = threading.Event()
    stop = threading.Event()
    old_settings = termios.tcgetattr(input_fd)
    tty.setcbreak(input_fd)

    def monitor() -> None:
        try:
            while not stop.is_set():
                ready, _, _ = select.select([input_fd], [], [], 0.2)
                if stop.is_set():
                    return
                if ready:
                    ch = os.read(input_fd, 1)
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
    return interrupted, stop, old_settings, tty_file


def stop_esc_monitor(
    monitor_state: tuple[threading.Event, threading.Event, list, "io.BufferedReader | None"] | None,
) -> None:
    """Stop ESC monitor and restore terminal settings."""
    if monitor_state is None:
        return
    _, stop, old_settings, tty_file = monitor_state
    stop.set()
    try:
        if tty_file is not None:
            termios.tcsetattr(tty_file.fileno(), termios.TCSADRAIN, old_settings)
        else:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
    except (OSError, ValueError):
        pass


# ── Interrupt menus ───────────────────────────────────────────────────


def handle_interrupt(
    session_id: str,
    project_dir: str | None,
    tty_file: "io.BufferedReader | None" = None,
    verbose: bool = False,
) -> tuple[str, bool]:
    """Show interrupt menu and return (choice, verbose).

    Choice is one of: 'continue', 'takeover', 'skip', or 'abort'.
    verbose is the (possibly toggled) verbose state.
    """
    import click

    # Flush any stale input from the ESC detection
    try:
        flush_fd = tty_file.fileno() if tty_file is not None else sys.stdin.fileno()
        termios.tcflush(flush_fd, termios.TCIFLUSH)
    except (OSError, ValueError):
        pass

    # When stdin is piped, redirect stdin to /dev/tty so click.prompt reads
    # from the terminal instead of the exhausted pipe.
    restore_stdin = None
    tty_stdin = None
    if not sys.stdin.isatty():
        try:
            tty_stdin = open("/dev/tty", "r")  # noqa: SIM115
            restore_stdin = sys.stdin
            sys.stdin = tty_stdin
        except OSError:
            if tty_stdin is not None:
                tty_stdin.close()
            tty_stdin = None

    verbose_label = "Off" if verbose else "On"

    click.echo()
    click.echo(click.style("  ⏸  Interrupted", fg="yellow", bold=True))
    click.echo()
    click.echo("  [1] Continue  — resume where it left off")
    click.echo("  [2] Take over — open interactive session")
    click.echo("  [3] Skip      — continue to next iteration")
    click.echo("  [4] Abort     — stop the loop")
    click.echo()
    click.echo(f"  [5] Toggle verbose ({verbose_label})")
    click.echo()
    try:
        choice = click.prompt(
            "  Choice", type=click.Choice(["1", "2", "3", "4", "5"]), default="1",
        )
    finally:
        if restore_stdin is not None:
            sys.stdin = restore_stdin
        if tty_stdin is not None:
            try:
                tty_stdin.close()
            except OSError:
                pass

    if choice == "5":
        verbose = not verbose
        state = "on" if verbose else "off"
        click.echo(click.style(f"  Verbose {state}", fg="cyan"))
        return "continue", verbose
    elif choice == "1":
        return "continue", verbose
    elif choice == "2":
        return "takeover", verbose
    elif choice == "3":
        return "skip", verbose
    else:
        return "abort", verbose


def post_takeover_menu(session_id: str) -> str:
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
    dangerously_skip_permissions: bool = False,
) -> int:
    """Resume a claude session interactively, inheriting the terminal."""
    cmd = ["claude", "--resume", session_id]
    if dangerously_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    return subprocess.call(cmd, cwd=project_dir)


# ── Parallel process group ────────────────────────────────────────────


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
        if not HAS_TERMIOS:
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
