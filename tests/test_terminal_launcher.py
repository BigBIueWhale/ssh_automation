"""
Comprehensive SSH terminal launcher test suite.

Fully mocked — no real processes are spawned, no SSH connections are made.
Tests cover argument building, platform-specific terminal commands, launch
logic, auto-reconnect, exception hierarchy, and typeguard enforcement.

Run with full visibility:
    pytest tests/test_terminal_launcher.py -v -s
"""

from __future__ import annotations

import platform
import subprocess
import sys
from typing import List
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Dependency gate — report clearly if anything is missing
# ---------------------------------------------------------------------------
_MISSING: List[str] = []

try:
    import paramiko  # noqa: F401
except ImportError:
    _MISSING.append("paramiko")

try:
    from typeguard import typechecked  # noqa: F401
except ImportError:
    _MISSING.append("typeguard")

# typeguard 4.x raises TypeCheckError (extends Exception, not TypeError).
# typeguard 2.x raises plain TypeError.  Accept either in enforcement tests.
try:
    from typeguard import TypeCheckError
    _TYPEGUARD_ERRORS = (TypeError, TypeCheckError)
except ImportError:
    _TYPEGUARD_ERRORS = (TypeError,)

if _MISSING:
    print(
        "\n"
        "=" * 72 + "\n"
        "  MISSING REQUIRED LIBRARIES\n"
        "=" * 72 + "\n"
        f"  The following packages are not installed: {', '.join(_MISSING)}\n"
        f"  Install them with:  pip install {' '.join(_MISSING)}\n"
        "=" * 72 + "\n",
        file=sys.stderr,
    )
    pytest.skip(
        f"Required libraries missing: {', '.join(_MISSING)}",
        allow_module_level=True,
    )

from linux_ssh_tools.connection import SSHConnectionManager
from linux_ssh_tools.terminal import SSHTerminalLauncher
from linux_ssh_tools.exceptions import (
    TerminalLaunchError,
    SSHConnectionError,
    LinuxSSHToolsError,
)

# ---------------------------------------------------------------------------
# Report environment
# ---------------------------------------------------------------------------
print(
    "\n"
    "+" * 72 + "\n"
    f"  Platform : {platform.system()} {platform.release()}\n"
    f"  Python   : {sys.version.split()[0]}\n"
    "+" * 72
)


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _report(label: str, detail: str = "") -> None:
    """Uniform test-level print."""
    if detail:
        print(f"  [{label}] {detail}")
    else:
        print(f"  [{label}]")


# ═══════════════════════════════════════════════════════════════════════════
#  FIXTURES
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def launcher():
    """Create an SSHTerminalLauncher backed by a real SSHConnectionManager.

    _clear_ssh_fingerprints is patched to a no-op so the fixture never
    touches the filesystem.
    """
    with patch.object(SSHConnectionManager, "_clear_ssh_fingerprints"):
        mgr = SSHConnectionManager(
            hostname="192.168.1.100",
            username="testuser",
            password="testpass",
            port=22,
        )
    return SSHTerminalLauncher(mgr)


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _build_ssh_args
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildSSHArgs:
    """Verify the SSH argument list builder."""

    def test_basic_args_no_command(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Basic args without initial_command")
        args = launcher._build_ssh_args()

        _report("RESULT", f"args = {args}")
        assert "ssh" in args
        assert "testuser@192.168.1.100" in args
        assert "-p" in args
        assert "22" in args
        assert "StrictHostKeyChecking=no" in args
        # UserKnownHostsFile should reference some null file
        assert any("UserKnownHostsFile=" in a for a in args)
        # No -t when there is no initial_command
        assert "-t" not in args
        _report("PASS", "Basic SSH args are correct")

    def test_args_with_initial_command(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Args with initial_command='top'")
        args = launcher._build_ssh_args(initial_command="top")

        _report("RESULT", f"args = {args}")
        assert "-t" in args
        assert args[-1] == "top ; exec ${SHELL:-/bin/bash}"
        _report("PASS", "initial_command appended with -t and exec shell")

    def test_args_with_command_containing_arguments(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Args with initial_command='ls -la /tmp'")
        args = launcher._build_ssh_args(initial_command="ls -la /tmp")

        _report("RESULT", f"args = {args}")
        assert "-t" in args
        assert args[-1] == "ls -la /tmp ; exec ${SHELL:-/bin/bash}"
        _report("PASS", "Command with arguments is a single trailing arg")

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_null_file_linux(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Null file on Linux should be /dev/null")
        args = launcher._build_ssh_args()

        null_args = [a for a in args if "UserKnownHostsFile=" in a]
        _report("RESULT", f"null args = {null_args}")
        assert any("/dev/null" in a for a in null_args)
        _report("PASS", "/dev/null used on Linux")

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_null_file_windows(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Null file on Windows should be NUL")
        args = launcher._build_ssh_args()

        null_args = [a for a in args if "UserKnownHostsFile=" in a]
        _report("RESULT", f"null args = {null_args}")
        assert any("NUL" in a for a in null_args)
        _report("PASS", "NUL used on Windows")

    def test_custom_port(self) -> None:
        _report("TEST", "Custom port 2222")
        with patch.object(SSHConnectionManager, "_clear_ssh_fingerprints"):
            mgr = SSHConnectionManager(
                hostname="192.168.1.100",
                username="testuser",
                password="testpass",
                port=2222,
            )
        lnch = SSHTerminalLauncher(mgr)
        args = lnch._build_ssh_args()

        idx = args.index("-p")
        _report("RESULT", f"-p followed by {args[idx + 1]}")
        assert args[idx + 1] == "2222"
        _report("PASS", "Custom port reflected in args")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _get_windows_terminal_command
# ═══════════════════════════════════════════════════════════════════════════

class TestWindowsTerminalCommand:
    """Verify Windows Terminal (wt) command generation."""

    def test_basic_windows_terminal(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Basic Windows Terminal command")
        result = launcher._get_windows_terminal_command()

        _report("RESULT", f"cmd = {result}")
        assert result[:5] == ["wt", "-w", "0", "--title", "SSH: 192.168.1.100"]
        assert "--" in result
        _report("PASS", "wt command structure is correct")

    def test_custom_title(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Windows Terminal with custom title")
        result = launcher._get_windows_terminal_command(window_title="My Session")

        _report("RESULT", f"cmd = {result}")
        title_idx = result.index("--title")
        assert result[title_idx + 1] == "My Session"
        _report("PASS", "Custom title in correct position")

    def test_with_initial_command(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Windows Terminal with initial_command='htop'")
        result = launcher._get_windows_terminal_command(initial_command="htop")

        _report("RESULT", f"cmd = {result}")
        assert "-t" in result
        _report("PASS", "-t present when initial_command given")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _get_cmd_terminal_command
# ═══════════════════════════════════════════════════════════════════════════

class TestCmdTerminalCommand:
    """Verify CMD (cmd /k) command generation."""

    def test_basic_cmd_command(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Basic CMD terminal command")
        result = launcher._get_cmd_terminal_command()

        _report("RESULT", f"cmd = {result!r}")
        assert isinstance(result, str)
        assert result.startswith("cmd /k title")
        assert "ssh" in result
        _report("PASS", "cmd /k string structure is correct")

    def test_metachar_escaping(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "CMD metachar escaping for '&'")
        result = launcher._get_cmd_terminal_command(window_title="Test & Title")

        _report("RESULT", f"cmd = {result!r}")
        # The implementation escapes ^ after &, so ^& becomes ^^&
        assert "Test ^^& Title" in result
        _report("PASS", "& escaped (with caret re-escaping)")

    def test_all_metachar_escaping(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "All cmd.exe metacharacters escaped")
        result = launcher._get_cmd_terminal_command(window_title="a&b|c<d>e^f(g)")

        _report("RESULT", f"cmd = {result!r}")
        # ^ is escaped after &|<>, so introduced carets also get doubled
        assert "a^^&b^^|c^^<d^^>e^^f^(g^)" in result
        _report("PASS", "All metacharacters properly escaped")

    def test_with_initial_command(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "CMD with initial_command containing args")
        result = launcher._get_cmd_terminal_command(initial_command="ls -la /tmp")

        _report("RESULT", f"cmd = {result!r}")
        assert "-t" in result
        _report("PASS", "-t present in cmd string when initial_command given")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _get_linux_terminal_command
# ═══════════════════════════════════════════════════════════════════════════

class TestLinuxTerminalCommand:
    """Verify Linux terminal emulator command generation."""

    def _mock_which(self, available: List[str]):
        """Return a side_effect function for shutil.which."""
        def _which(name: str):
            return f"/usr/bin/{name}" if name in available else None
        return _which

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_gnome_terminal(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "gnome-terminal detected")
        mock_which.side_effect = self._mock_which(["gnome-terminal"])

        result = launcher._get_linux_terminal_command()
        _report("RESULT", f"cmd = {result}")
        assert result[0] == "gnome-terminal"
        assert "--title" in result
        assert "--" in result
        _report("PASS", "gnome-terminal command structure correct")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_xterm(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "xterm detected")
        mock_which.side_effect = self._mock_which(["xterm"])

        result = launcher._get_linux_terminal_command()
        _report("RESULT", f"cmd = {result}")
        assert result[0] == "xterm"
        assert "-T" in result
        assert "-e" in result
        _report("PASS", "xterm command structure correct")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_x_terminal_emulator(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "x-terminal-emulator detected")
        mock_which.side_effect = self._mock_which(["x-terminal-emulator"])

        result = launcher._get_linux_terminal_command()
        _report("RESULT", f"cmd = {result}")
        assert result[0] == "x-terminal-emulator"
        assert "-e" in result
        _report("PASS", "x-terminal-emulator command structure correct")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_x_terminal_emulator_takes_priority(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "x-terminal-emulator takes priority over gnome-terminal")
        mock_which.side_effect = self._mock_which(["x-terminal-emulator", "gnome-terminal"])

        result = launcher._get_linux_terminal_command()
        _report("RESULT", f"cmd = {result}")
        assert result[0] == "x-terminal-emulator"
        _report("PASS", "x-terminal-emulator wins when both available")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_fallback_no_terminal(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "No terminal emulator found — fallback to bare ssh")
        mock_which.return_value = None

        result = launcher._get_linux_terminal_command()
        _report("RESULT", f"cmd = {result}")
        assert result[0] == "ssh"
        _report("PASS", "Falls back to bare ssh args")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_custom_title_gnome(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Custom title with gnome-terminal")
        mock_which.side_effect = self._mock_which(["gnome-terminal"])

        result = launcher._get_linux_terminal_command(window_title="Custom Title")
        _report("RESULT", f"cmd = {result}")
        title_idx = result.index("--title")
        assert result[title_idx + 1] == "Custom Title"
        _report("PASS", "Custom title appears after --title")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_with_initial_command_and_args(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "gnome-terminal with initial_command='vim /etc/hosts'")
        mock_which.side_effect = self._mock_which(["gnome-terminal"])

        result = launcher._get_linux_terminal_command(initial_command="vim /etc/hosts")
        _report("RESULT", f"cmd = {result}")
        assert "-t" in result
        assert result[-1] == "vim /etc/hosts ; exec ${SHELL:-/bin/bash}"
        _report("PASS", "Command with args embedded correctly")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — launch_terminal
# ═══════════════════════════════════════════════════════════════════════════

class TestLaunchTerminal:
    """Verify launch_terminal dispatches correctly per platform."""

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_launch_linux_success(self, mock_which, mock_popen, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Launch on Linux with gnome-terminal")
        mock_which.side_effect = lambda name: "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None
        mock_popen.return_value = MagicMock()

        launcher.launch_terminal(context="test linux launch")

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        _report("RESULT", f"Popen called with cmd = {cmd}")
        assert cmd[0] == "gnome-terminal"
        # No creationflags on Linux
        assert "creationflags" not in call_args[1]
        _report("PASS", "Linux launch dispatches to gnome-terminal")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_launch_windows_terminal(self, mock_which, mock_popen, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Launch on Windows with wt available")
        mock_which.return_value = "C:\\wt.exe"
        mock_popen.return_value = MagicMock()

        # CREATE_NEW_CONSOLE may not exist on Linux; ensure it's available
        with patch.object(subprocess, "CREATE_NEW_CONSOLE", 0x10, create=True):
            launcher.launch_terminal(context="test windows launch")

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        _report("RESULT", f"Popen called with cmd = {cmd}")
        assert cmd[0] == "wt"
        assert call_args[1].get("creationflags") == 0x10
        _report("PASS", "Windows launch dispatches to wt with CREATE_NEW_CONSOLE")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_launch_windows_cmd_fallback(self, mock_which, mock_popen, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Launch on Windows without wt — falls back to cmd")
        mock_which.return_value = None
        mock_popen.return_value = MagicMock()

        with patch.object(subprocess, "CREATE_NEW_CONSOLE", 0x10, create=True):
            launcher.launch_terminal(context="test windows cmd fallback")

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        _report("RESULT", f"Popen called with cmd = {cmd!r}")
        assert isinstance(cmd, str)
        assert cmd.startswith("cmd /k")
        _report("PASS", "Windows fallback uses cmd /k")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_launch_windows_cmd_explicit(self, mock_which, mock_popen, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Launch on Windows with use_windows_terminal=False")
        mock_which.return_value = "C:\\wt.exe"  # wt exists but should be ignored
        mock_popen.return_value = MagicMock()

        with patch.object(subprocess, "CREATE_NEW_CONSOLE", 0x10, create=True):
            launcher.launch_terminal(context="test windows cmd explicit", use_windows_terminal=False)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        _report("RESULT", f"Popen called with cmd = {cmd!r}")
        assert isinstance(cmd, str)
        assert cmd.startswith("cmd /k")
        _report("PASS", "use_windows_terminal=False forces cmd")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_launch_wraps_exception(self, mock_which, mock_popen, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "OSError from Popen wraps into TerminalLaunchError")
        mock_which.side_effect = lambda name: "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None
        mock_popen.side_effect = OSError("No such file")

        with pytest.raises(TerminalLaunchError) as exc_info:
            launcher.launch_terminal(context="test exception wrap")

        _report("CAUGHT", f"TerminalLaunchError: {exc_info.value}")
        assert "192.168.1.100" in str(exc_info.value)
        assert exc_info.value.__cause__ is not None
        _report("PASS", "OSError wrapped in TerminalLaunchError with context")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_launch_linux_fallback_raw_ssh(self, mock_which, mock_popen, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "No terminal emulator found — Popen gets bare ssh args")
        mock_which.return_value = None
        mock_popen.return_value = MagicMock()

        launcher.launch_terminal(context="test linux fallback")

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        _report("RESULT", f"Popen called with cmd = {cmd}")
        assert cmd[0] == "ssh"
        _report("PASS", "Falls back to bare ssh on Linux")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — launch_with_auto_reconnect
# ═══════════════════════════════════════════════════════════════════════════

class TestLaunchWithAutoReconnect:
    """Verify retry logic in launch_with_auto_reconnect."""

    @patch("linux_ssh_tools.terminal.time.sleep")
    def test_success_first_attempt(self, mock_sleep, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Succeeds on first attempt")
        with patch.object(launcher, "launch_terminal", return_value=MagicMock()) as mock_lt:
            launcher.launch_with_auto_reconnect(context="test first attempt")

        mock_lt.assert_called_once()
        mock_sleep.assert_not_called()
        _report("PASS", "launch_terminal called once, no sleep")

    @patch("linux_ssh_tools.terminal.time.sleep")
    def test_retry_then_success(self, mock_sleep, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Fails twice then succeeds on third attempt")
        effects = [
            TerminalLaunchError("fail 1"),
            TerminalLaunchError("fail 2"),
            MagicMock(),
        ]
        with patch.object(launcher, "launch_terminal", side_effect=effects) as mock_lt:
            launcher.launch_with_auto_reconnect(context="test retry success", max_attempts=3)

        assert mock_lt.call_count == 3
        assert mock_sleep.call_count == 2
        _report("PASS", "3 calls, 2 sleeps, succeeded on third")

    @patch("linux_ssh_tools.terminal.time.sleep")
    def test_all_attempts_fail(self, mock_sleep, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "All attempts fail — raises TerminalLaunchError")
        with patch.object(
            launcher, "launch_terminal",
            side_effect=TerminalLaunchError("always fails"),
        ) as mock_lt:
            with pytest.raises(TerminalLaunchError):
                launcher.launch_with_auto_reconnect(context="test all fail", max_attempts=4)

        assert mock_lt.call_count == 4
        assert mock_sleep.call_count == 3  # max_attempts - 1
        _report("PASS", "All 4 attempts failed, 3 sleeps, exception raised")

    @patch("linux_ssh_tools.terminal.time.sleep")
    def test_retry_delay_respected(self, mock_sleep, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Custom retry_delay=5.0 is passed to sleep")
        with patch.object(
            launcher, "launch_terminal",
            side_effect=[TerminalLaunchError("fail"), MagicMock()],
        ):
            launcher.launch_with_auto_reconnect(
                context="test delay", max_attempts=2, retry_delay=5.0,
            )

        mock_sleep.assert_called_once_with(5.0)
        _report("PASS", "sleep called with 5.0")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════

class TestTerminalExceptionHierarchy:
    """Verify TerminalLaunchError class relationships."""

    def test_terminal_launch_error_under_base(self) -> None:
        _report("TEST", "TerminalLaunchError is subclass of LinuxSSHToolsError")
        assert issubclass(TerminalLaunchError, LinuxSSHToolsError)
        _report("PASS", "TerminalLaunchError descends from LinuxSSHToolsError")

    def test_terminal_launch_error_not_under_connection_error(self) -> None:
        _report("TEST", "TerminalLaunchError is NOT subclass of SSHConnectionError")
        assert not issubclass(TerminalLaunchError, SSHConnectionError)
        _report("PASS", "TerminalLaunchError is a sibling, not child of SSHConnectionError")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Typeguard enforcement
# ═══════════════════════════════════════════════════════════════════════════

class TestTypeguardEnforcement:
    """Verify typeguard rejects wrong types for SSHTerminalLauncher."""

    def test_rejects_wrong_manager_type(self) -> None:
        _report("TEST", "SSHTerminalLauncher('not_a_manager') should raise")
        with pytest.raises(_TYPEGUARD_ERRORS):
            SSHTerminalLauncher("not_a_manager")  # type: ignore[arg-type]
        _report("PASS", "TypeError/TypeCheckError raised for string argument")

    def test_rejects_none_manager_type(self) -> None:
        _report("TEST", "SSHTerminalLauncher(None) should raise")
        with pytest.raises(_TYPEGUARD_ERRORS):
            SSHTerminalLauncher(None)  # type: ignore[arg-type]
        _report("PASS", "TypeError/TypeCheckError raised for None argument")


# ---------------------------------------------------------------------------
#  Entry point for running outside pytest
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
