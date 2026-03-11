"""Tests for ESC monitor /dev/tty fallback when stdin is piped."""

from __future__ import annotations

import termios
import threading
from unittest.mock import MagicMock, patch

import pytest

from pralph.runner import _start_esc_monitor, _stop_esc_monitor


@pytest.fixture()
def mock_proc():
    proc = MagicMock()
    proc.send_signal = MagicMock()
    return proc


class TestStartEscMonitorPipedStdin:
    """When stdin is not a TTY, _start_esc_monitor should open /dev/tty."""

    def test_uses_dev_tty_when_stdin_is_pipe(self, mock_proc):
        mock_tty_file = MagicMock()
        mock_tty_file.fileno.return_value = 42

        mock_stdin = MagicMock()
        mock_stdin.fileno.return_value = 0

        with (
            patch("pralph.runner.sys.stdin", mock_stdin),
            patch("pralph.runner.os.isatty", return_value=False),
            patch("builtins.open", return_value=mock_tty_file) as mock_open,
            patch("pralph.runner.termios.tcgetattr", return_value=[1, 2, 3]),
            patch("pralph.runner.tty.setcbreak") as mock_setcbreak,
            patch("pralph.runner.select.select", return_value=([], [], [])),
        ):
            result = _start_esc_monitor(mock_proc)

            assert result is not None
            interrupted, stop, old_settings, tty_file = result

            mock_open.assert_called_once_with("/dev/tty", "rb", buffering=0)
            mock_setcbreak.assert_called_once_with(42)
            assert tty_file is mock_tty_file
            assert old_settings == [1, 2, 3]

            # Clean up monitor thread
            stop.set()

    def test_returns_none_when_no_tty_available(self, mock_proc):
        mock_stdin = MagicMock()
        mock_stdin.fileno.return_value = 0

        with (
            patch("pralph.runner.sys.stdin", mock_stdin),
            patch("pralph.runner.os.isatty", return_value=False),
            patch("builtins.open", side_effect=OSError("No TTY")),
        ):
            result = _start_esc_monitor(mock_proc)
            assert result is None

    def test_normal_tty_path_unchanged(self, mock_proc):
        mock_stdin = MagicMock()
        mock_stdin.fileno.return_value = 0

        with (
            patch("pralph.runner.sys.stdin", mock_stdin),
            patch("pralph.runner.os.isatty", return_value=True),
            patch("pralph.runner.termios.tcgetattr", return_value=[1, 2, 3]),
            patch("pralph.runner.tty.setcbreak") as mock_setcbreak,
            patch("pralph.runner.select.select", return_value=([], [], [])),
        ):
            result = _start_esc_monitor(mock_proc)

            assert result is not None
            interrupted, stop, old_settings, tty_file = result

            mock_setcbreak.assert_called_once_with(0)
            assert tty_file is None

            stop.set()


class TestStopEscMonitorCleanup:
    """_stop_esc_monitor should restore settings on the correct fd."""

    def test_restores_settings_on_tty_fd(self):
        mock_tty_file = MagicMock()
        mock_tty_file.fileno.return_value = 42

        stop = threading.Event()
        monitor_state = (threading.Event(), stop, [1, 2, 3], mock_tty_file)

        with patch("pralph.runner.termios.tcsetattr") as mock_tcsetattr:
            _stop_esc_monitor(monitor_state)

        assert stop.is_set()
        mock_tcsetattr.assert_called_once_with(42, termios.TCSADRAIN, [1, 2, 3])

    def test_restores_settings_on_stdin_when_no_tty_file(self):
        mock_stdin = MagicMock()
        mock_stdin.fileno.return_value = 0

        stop = threading.Event()
        monitor_state = (threading.Event(), stop, [1, 2, 3], None)

        with (
            patch("pralph.runner.sys.stdin", mock_stdin),
            patch("pralph.runner.termios.tcsetattr") as mock_tcsetattr,
        ):
            _stop_esc_monitor(monitor_state)

        assert stop.is_set()
        mock_tcsetattr.assert_called_once_with(0, termios.TCSADRAIN, [1, 2, 3])

    def test_handles_none_state_gracefully(self):
        _stop_esc_monitor(None)  # Should not raise
