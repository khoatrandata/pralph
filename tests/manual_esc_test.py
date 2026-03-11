#!/usr/bin/env python3
"""Manual test for ESC monitor with piped and interactive stdin.

Usage:
    python tests/manual_esc_test.py             # interactive — ESC should work
    echo "hello" | python tests/manual_esc_test.py  # piped stdin — ESC should also work
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time

# Add project root to path
sys.path.insert(0, ".")

from pralph.runner import _handle_interrupt, _start_esc_monitor, _stop_esc_monitor


def main() -> None:
    print(f"stdin is a TTY: {sys.stdin.isatty()}")
    print("Starting a long-running subprocess...")
    print("Press ESC to trigger interrupt menu.\n")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\nfor i in range(60):\n    print(f'Working... {i}', flush=True)\n    time.sleep(1)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    monitor_state = _start_esc_monitor(proc)
    if monitor_state is None:
        print("WARNING: ESC monitor could not be started!")
        print("This means the fix is not working for piped stdin.")
        proc.kill()
        proc.wait()
        sys.exit(1)

    print("ESC monitor started successfully.")
    tty_file = monitor_state[3]
    if tty_file is not None:
        print(f"Using /dev/tty (fd={tty_file.fileno()}) for keyboard input.")
    else:
        print("Using stdin for keyboard input.")
    print()

    interrupted = monitor_state[0]

    try:
        import select as _select

        assert proc.stdout is not None
        while proc.poll() is None:
            if interrupted.is_set():
                _stop_esc_monitor(monitor_state)
                choice = _handle_interrupt("test-session", None, tty_file=tty_file)
                if tty_file is not None:
                    try:
                        tty_file.close()
                    except OSError:
                        pass

                if choice == "continue":
                    print("\nResuming...\n")
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except OSError:
                        pass
                    monitor_state = _start_esc_monitor(proc)
                    if monitor_state:
                        interrupted = monitor_state[0]
                        tty_file = monitor_state[3]
                    continue
                else:
                    print(f"\nChoice: {choice}")
                    proc.kill()
                    proc.wait()
                    return

            # Non-blocking read so we can check interrupted between iterations
            ready, _, _ = _select.select([proc.stdout], [], [], 0.2)
            if ready:
                line = proc.stdout.readline()
                if line:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
    finally:
        _stop_esc_monitor(monitor_state)
        if monitor_state is not None and monitor_state[3] is not None:
            try:
                monitor_state[3].close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    print("\nSubprocess finished.")


if __name__ == "__main__":
    main()
