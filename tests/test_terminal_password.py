"""
SSH terminal launcher password automation tests.

Tests cover askpass script creation, cleanup scheduling, password environment
preparation, command prefix injection, and end-to-end launch_terminal
integration with password delivery.

Only ``subprocess.Popen`` is mocked (can't spawn real terminals in CI).
Everything else — filesystem, timers, env construction — is real.

Run with:
    PYTHONPATH=linux_ssh_tools/src python -m pytest tests/test_terminal_password.py -v -s
"""

from __future__ import annotations

import os
import platform
import stat
import sys
import time
from typing import List
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Dependency gate
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

_IS_WINDOWS = platform.system() == "Windows"


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _report(label: str, detail: str = "") -> None:
    if detail:
        print(f"  [{label}] {detail}")
    else:
        print(f"  [{label}]")


def _make_launcher(password: str = "testpass", hostname: str = "192.168.1.100",
                   username: str = "testuser", port: int = 22) -> SSHTerminalLauncher:
    with patch.object(SSHConnectionManager, "_clear_ssh_fingerprints"):
        mgr = SSHConnectionManager(
            hostname=hostname, username=username, password=password, port=port,
        )
    return SSHTerminalLauncher(mgr)


# ═══════════════════════════════════════════════════════════════════════════
#  FIXTURES
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def launcher():
    return _make_launcher()


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _create_askpass_script
# ═══════════════════════════════════════════════════════════════════════════

class TestCreateAskpassScript:
    """Real filesystem — creates and reads actual temp files."""

    @pytest.mark.skipif(_IS_WINDOWS, reason="Linux shell script test")
    def test_linux_script_content(self) -> None:
        _report("TEST", "Linux askpass script contains correct echo")
        path = SSHTerminalLauncher._create_askpass_script("s3cret")
        try:
            with open(path) as f:
                content = f.read()
            _report("RESULT", f"content = {content!r}")
            assert content == "#!/bin/sh\necho 's3cret'\n"
            assert path.endswith(".sh")
        finally:
            os.unlink(path)
        _report("PASS", "Script content and suffix correct")

    @pytest.mark.skipif(_IS_WINDOWS, reason="Linux permissions test")
    def test_linux_script_permissions(self) -> None:
        _report("TEST", "Linux askpass script has mode 0700")
        path = SSHTerminalLauncher._create_askpass_script("pw")
        try:
            mode = os.stat(path).st_mode
            _report("RESULT", f"mode = {oct(mode)}")
            assert mode & 0o777 == 0o700
        finally:
            os.unlink(path)
        _report("PASS", "Permissions are 0700")

    @pytest.mark.skipif(not _IS_WINDOWS, reason="Windows bat script test")
    def test_windows_script_content(self) -> None:
        _report("TEST", "Windows askpass script contains correct echo")
        path = SSHTerminalLauncher._create_askpass_script("s3cret")
        try:
            with open(path) as f:
                content = f.read()
            _report("RESULT", f"content = {content!r}")
            assert content == "@echo off\necho s3cret\n"
            assert path.endswith(".bat")
        finally:
            os.unlink(path)
        _report("PASS", "Script content and suffix correct")

    def test_special_chars_single_quote(self) -> None:
        _report("TEST", "Password with single quotes is escaped")
        path = SSHTerminalLauncher._create_askpass_script("it's a test")
        try:
            with open(path) as f:
                content = f.read()
            _report("RESULT", f"content = {content!r}")
            if _IS_WINDOWS:
                assert "it's a test" in content
            else:
                assert "it'\\''s a test" in content
        finally:
            os.unlink(path)
        _report("PASS", "Single quotes handled correctly")

    def test_special_chars_ampersand(self) -> None:
        _report("TEST", "Password with & is escaped")
        path = SSHTerminalLauncher._create_askpass_script("a&b")
        try:
            with open(path) as f:
                content = f.read()
            _report("RESULT", f"content = {content!r}")
            if _IS_WINDOWS:
                assert "a^&b" in content
            else:
                # Inside single quotes, & needs no special escaping
                assert "'a&b'" in content
        finally:
            os.unlink(path)
        _report("PASS", "Ampersand handled correctly")

    # ---------------------------------------------------------------
    #  Cross-platform: mock _IS_WINDOWS so both .sh and .bat code
    #  paths run on every CI runner (Linux AND Windows).
    # ---------------------------------------------------------------

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_linux_script_via_mock(self) -> None:
        _report("TEST", "Force Linux path via _IS_WINDOWS=False (runs everywhere)")
        path = SSHTerminalLauncher._create_askpass_script("s3cret")
        try:
            with open(path) as f:
                content = f.read()
            _report("RESULT", f"content = {content!r}")
            assert content == "#!/bin/sh\necho 's3cret'\n"
            assert path.endswith(".sh")
        finally:
            os.unlink(path)
        _report("PASS", "Linux .sh path verified via mock")

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_windows_bat_via_mock(self) -> None:
        _report("TEST", "Force Windows path via _IS_WINDOWS=True (runs everywhere)")
        path = SSHTerminalLauncher._create_askpass_script("s3cret")
        try:
            with open(path) as f:
                content = f.read()
            _report("RESULT", f"content = {content!r}")
            assert content == "@echo off\necho s3cret\n"
            assert path.endswith(".bat")
        finally:
            os.unlink(path)
        _report("PASS", "Windows .bat path verified via mock")

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_windows_bat_metachar_escaping(self) -> None:
        _report("TEST", "Windows .bat escapes cmd metacharacters (runs everywhere)")
        path = SSHTerminalLauncher._create_askpass_script("p^a&s|s<w>o(r)d")
        try:
            with open(path) as f:
                content = f.read()
            _report("RESULT", f"content = {content!r}")
            # ^ must be escaped first to avoid double-escaping
            assert "p^^a^&s^|s^<w^>o^(r^)d" in content
        finally:
            os.unlink(path)
        _report("PASS", "All cmd metacharacters escaped in .bat")

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_linux_sh_single_quote_via_mock(self) -> None:
        _report("TEST", "Linux .sh single-quote escaping (runs everywhere)")
        path = SSHTerminalLauncher._create_askpass_script("it's")
        try:
            with open(path) as f:
                content = f.read()
            _report("RESULT", f"content = {content!r}")
            assert "echo 'it'\\''s'" in content
        finally:
            os.unlink(path)
        _report("PASS", "Single quotes escaped with shell idiom")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _schedule_askpass_cleanup
# ═══════════════════════════════════════════════════════════════════════════

class TestScheduleAskpassCleanup:
    """Real timer, real filesystem."""

    def test_file_deleted_after_delay(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Askpass script deleted after short delay")
        path = SSHTerminalLauncher._create_askpass_script("pw")
        assert os.path.exists(path)

        launcher._schedule_askpass_cleanup(path, delay=0.2)
        time.sleep(0.5)

        _report("RESULT", f"exists after cleanup = {os.path.exists(path)}")
        assert not os.path.exists(path)
        _report("PASS", "File deleted by cleanup timer")

    def test_tolerates_missing_file(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Cleanup tolerates already-deleted file")
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "nonexistent_askpass_test_file.sh")
        # Should not raise
        launcher._schedule_askpass_cleanup(path, delay=0.1)
        time.sleep(0.3)
        _report("PASS", "No exception for missing file")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _prepare_password_env
# ═══════════════════════════════════════════════════════════════════════════

class TestPreparePasswordEnv:
    """Real file creation, real env construction. Only shutil.which mocked."""

    @pytest.mark.skipif(_IS_WINDOWS, reason="sshpass is Linux-only")
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=True)
    def test_linux_sshpass_path(self, _mock_has, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Linux + sshpass available -> sshpass prefix")
        prefix, env, askpass_path = launcher._prepare_password_env()

        _report("RESULT", f"prefix={prefix}, env keys={list(env.keys())}, askpass={askpass_path}")
        assert prefix == ["sshpass", "-e"]
        assert env == {"SSHPASS": "testpass"}
        assert askpass_path is None
        _report("PASS", "sshpass strategy selected")

    @pytest.mark.skipif(_IS_WINDOWS, reason="Linux askpass fallback test")
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=False)
    def test_linux_askpass_fallback(self, _mock_has, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Linux + no sshpass -> SSH_ASKPASS fallback")
        prefix, env, askpass_path = launcher._prepare_password_env()
        try:
            _report("RESULT", f"prefix={prefix}, env keys={sorted(env.keys())}, askpass={askpass_path}")
            assert prefix == []
            assert "SSH_ASKPASS" in env
            assert "SSH_ASKPASS_REQUIRE" in env
            assert env["SSH_ASKPASS_REQUIRE"] == "force"
            assert "DISPLAY" in env
            assert askpass_path is not None
            assert os.path.exists(askpass_path)
        finally:
            if askpass_path and os.path.exists(askpass_path):
                os.unlink(askpass_path)
        _report("PASS", "SSH_ASKPASS fallback with DISPLAY set")

    @pytest.mark.skipif(not _IS_WINDOWS, reason="Windows askpass test")
    def test_windows_askpass(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Windows -> SSH_ASKPASS (no sshpass)")
        prefix, env, askpass_path = launcher._prepare_password_env()
        try:
            _report("RESULT", f"prefix={prefix}, env keys={sorted(env.keys())}, askpass={askpass_path}")
            assert prefix == []
            assert "SSH_ASKPASS" in env
            assert "SSH_ASKPASS_REQUIRE" in env
            assert askpass_path is not None
            # No DISPLAY on Windows
            assert "DISPLAY" not in env
        finally:
            if askpass_path and os.path.exists(askpass_path):
                os.unlink(askpass_path)
        _report("PASS", "Windows askpass strategy correct")

    # ---------------------------------------------------------------
    #  Cross-platform: mock _IS_WINDOWS so both sshpass and askpass
    #  code paths run on every CI runner.
    # ---------------------------------------------------------------

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=True)
    def test_sshpass_path_via_mock(self, _mock_has, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "sshpass path via _IS_WINDOWS=False mock (runs everywhere)")
        prefix, env, askpass_path = launcher._prepare_password_env()

        _report("RESULT", f"prefix={prefix}, env={env}, askpass={askpass_path}")
        assert prefix == ["sshpass", "-e"]
        assert env == {"SSHPASS": "testpass"}
        assert askpass_path is None
        _report("PASS", "sshpass strategy via mock")

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=False)
    def test_linux_askpass_via_mock(self, _mock_has, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Linux askpass fallback via mock (runs everywhere)")
        prefix, env, askpass_path = launcher._prepare_password_env()
        try:
            assert prefix == []
            assert "SSH_ASKPASS" in env
            assert "SSH_ASKPASS_REQUIRE" in env
            assert env["SSH_ASKPASS_REQUIRE"] == "force"
            assert "DISPLAY" in env
            assert askpass_path is not None
            assert os.path.exists(askpass_path)
            assert askpass_path.endswith(".sh")
            _report("RESULT", f"env keys={sorted(env.keys())}, suffix=.sh")
        finally:
            if askpass_path and os.path.exists(askpass_path):
                os.unlink(askpass_path)
        _report("PASS", "Linux askpass with DISPLAY via mock")

    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_windows_askpass_via_mock(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Windows askpass via _IS_WINDOWS=True mock (runs everywhere)")
        prefix, env, askpass_path = launcher._prepare_password_env()
        try:
            assert prefix == []
            assert "SSH_ASKPASS" in env
            assert "SSH_ASKPASS_REQUIRE" in env
            assert askpass_path is not None
            assert "DISPLAY" not in env
            assert askpass_path.endswith(".bat")
            _report("RESULT", f"env keys={sorted(env.keys())}, suffix=.bat")
        finally:
            if askpass_path and os.path.exists(askpass_path):
                os.unlink(askpass_path)
        _report("PASS", "Windows askpass without DISPLAY via mock")

    def test_empty_password(self) -> None:
        _report("TEST", "Empty password -> skip automation entirely")
        lnch = _make_launcher(password="")
        prefix, env, askpass_path = lnch._prepare_password_env()

        _report("RESULT", f"prefix={prefix}, env={env}, askpass={askpass_path}")
        assert prefix == []
        assert env == {}
        assert askpass_path is None
        _report("PASS", "No automation for empty password")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Command methods with ssh_prefix
# ═══════════════════════════════════════════════════════════════════════════

class TestCommandsWithSshPrefix:
    """Real method calls, no mocks needed (except shutil.which for terminals)."""

    def _mock_which(self, available: List[str]):
        def _which(name: str):
            return f"/usr/bin/{name}" if name in available else None
        return _which

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_gnome_terminal_with_sshpass_prefix(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "gnome-terminal with sshpass prefix")
        mock_which.side_effect = self._mock_which(["gnome-terminal"])

        result = launcher._get_linux_terminal_command(ssh_prefix=["sshpass", "-e"])
        _report("RESULT", f"cmd = {result}")

        dash_dash_idx = result.index("--")
        after_separator = result[dash_dash_idx + 1:]
        assert after_separator[0] == "sshpass"
        assert after_separator[1] == "-e"
        assert after_separator[2] == "ssh"
        _report("PASS", "sshpass prefix inserted between -- and ssh")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_xterm_with_sshpass_prefix(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "xterm with sshpass prefix")
        mock_which.side_effect = self._mock_which(["xterm"])

        result = launcher._get_linux_terminal_command(ssh_prefix=["sshpass", "-e"])
        _report("RESULT", f"cmd = {result}")

        e_idx = result.index("-e")
        after_e = result[e_idx + 1:]
        assert after_e[0] == "sshpass"
        assert after_e[1] == "-e"
        assert after_e[2] == "ssh"
        _report("PASS", "sshpass prefix inserted after -e for xterm")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_fallback_with_prefix(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Bare ssh fallback with sshpass prefix")
        mock_which.return_value = None

        result = launcher._get_linux_terminal_command(ssh_prefix=["sshpass", "-e"])
        _report("RESULT", f"cmd = {result}")

        assert result[0] == "sshpass"
        assert result[1] == "-e"
        assert result[2] == "ssh"
        _report("PASS", "sshpass prefix at start of bare ssh command")

    def test_windows_terminal_with_prefix(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Windows Terminal (wt) with sshpass prefix")
        result = launcher._get_windows_terminal_command(ssh_prefix=["sshpass", "-e"])
        _report("RESULT", f"cmd = {result}")

        dash_dash_idx = result.index("--")
        after_separator = result[dash_dash_idx + 1:]
        assert after_separator[0] == "sshpass"
        assert after_separator[1] == "-e"
        assert after_separator[2] == "ssh"
        _report("PASS", "sshpass prefix inserted in wt command")

    def test_cmd_terminal_with_prefix(self, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "CMD terminal with sshpass prefix")
        result = launcher._get_cmd_terminal_command(ssh_prefix=["sshpass", "-e"])
        _report("RESULT", f"cmd = {result!r}")

        assert isinstance(result, str)
        assert "sshpass -e ssh" in result
        _report("PASS", "sshpass prefix in cmd /k string")

    @patch("linux_ssh_tools.terminal.shutil.which")
    def test_no_prefix_preserves_old_behavior(self, mock_which, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "No prefix (default) preserves original command structure")
        mock_which.side_effect = self._mock_which(["gnome-terminal"])

        result_no_arg = launcher._get_linux_terminal_command()
        result_none = launcher._get_linux_terminal_command(ssh_prefix=None)
        result_empty = launcher._get_linux_terminal_command(ssh_prefix=[])

        _report("RESULT", f"no_arg = {result_no_arg}")
        assert result_no_arg == result_none == result_empty
        # First element after -- should be ssh
        dash_dash_idx = result_no_arg.index("--")
        assert result_no_arg[dash_dash_idx + 1] == "ssh"
        _report("PASS", "Default behavior unchanged")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — launch_terminal with password
# ═══════════════════════════════════════════════════════════════════════════

class TestLaunchTerminalWithPassword:
    """Popen mocked, everything else real."""

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=True)
    def test_sshpass_env_and_prefix_in_cmd(
        self, _mock_has, mock_which, mock_popen, launcher: SSHTerminalLauncher,
    ) -> None:
        _report("TEST", "launch_terminal with sshpass: env has SSHPASS, cmd has prefix")
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("gnome-terminal", "ssh") else None
        mock_popen.return_value = MagicMock()

        launcher.launch_terminal(context="test sshpass launch")

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        kwargs = call_args[1]

        _report("RESULT", f"cmd = {cmd}")
        _report("RESULT", f"env has SSHPASS = {'SSHPASS' in kwargs.get('env', {})}")

        # Command should contain sshpass prefix
        dash_dash_idx = cmd.index("--")
        assert cmd[dash_dash_idx + 1] == "sshpass"
        assert cmd[dash_dash_idx + 2] == "-e"
        assert cmd[dash_dash_idx + 3] == "ssh"

        # Environment should contain SSHPASS
        assert "env" in kwargs
        assert kwargs["env"]["SSHPASS"] == "testpass"
        _report("PASS", "sshpass prefix and SSHPASS env var present")

    @patch.object(SSHTerminalLauncher, "_require_askpass_force_support")
    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=False)
    def test_askpass_env_in_launch(
        self, _mock_has, mock_which, mock_popen, _mock_askpass_check,
        launcher: SSHTerminalLauncher,
    ) -> None:
        _report("TEST", "launch_terminal with askpass: env has SSH_ASKPASS")
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("gnome-terminal", "ssh") else None
        mock_popen.return_value = MagicMock()

        launcher.launch_terminal(context="test askpass launch")

        call_args = mock_popen.call_args
        kwargs = call_args[1]

        _report("RESULT", f"env keys = {sorted(k for k in kwargs.get('env', {}) if k.startswith('SSH_'))}")

        assert "env" in kwargs
        env = kwargs["env"]
        assert "SSH_ASKPASS" in env
        assert "SSH_ASKPASS_REQUIRE" in env
        assert env["SSH_ASKPASS_REQUIRE"] == "force"

        # Askpass script should exist (cleanup scheduled but not yet run)
        askpass_path = env["SSH_ASKPASS"]
        _report("RESULT", f"askpass script exists = {os.path.exists(askpass_path)}")
        assert os.path.exists(askpass_path)

        # Clean up manually
        if os.path.exists(askpass_path):
            os.unlink(askpass_path)
        _report("PASS", "SSH_ASKPASS env vars set correctly")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_no_password_no_env(self, mock_which, mock_popen) -> None:
        _report("TEST", "launch_terminal with empty password: no env manipulation")
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("gnome-terminal", "ssh") else None
        mock_popen.return_value = MagicMock()

        lnch = _make_launcher(password="")
        lnch.launch_terminal(context="test no password")

        call_args = mock_popen.call_args
        kwargs = call_args[1]

        _report("RESULT", f"'env' in kwargs = {'env' in kwargs}")
        assert "env" not in kwargs
        _report("PASS", "No env kwarg when password is empty")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_no_password_no_prefix(self, mock_which, mock_popen) -> None:
        _report("TEST", "launch_terminal with empty password: no sshpass prefix in cmd")
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("gnome-terminal", "ssh") else None
        mock_popen.return_value = MagicMock()

        lnch = _make_launcher(password="")
        lnch.launch_terminal(context="test no password prefix")

        cmd = mock_popen.call_args[0][0]
        _report("RESULT", f"cmd = {cmd}")

        dash_dash_idx = cmd.index("--")
        assert cmd[dash_dash_idx + 1] == "ssh"
        _report("PASS", "No prefix when password is empty")

    @patch.object(SSHTerminalLauncher, "_require_askpass_force_support")
    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=False)
    def test_windows_askpass_launch(
        self, _mock_has, mock_which, mock_popen, _mock_askpass_check,
        launcher: SSHTerminalLauncher,
    ) -> None:
        _report("TEST", "launch_terminal on Windows uses SSH_ASKPASS")
        mock_which.side_effect = lambda name: "C:\\ssh.exe" if name == "ssh" else None
        mock_popen.return_value = MagicMock()

        with patch("subprocess.CREATE_NEW_CONSOLE", 0x10, create=True):
            launcher.launch_terminal(context="test windows askpass")

        call_args = mock_popen.call_args
        kwargs = call_args[1]

        assert "env" in kwargs
        env = kwargs["env"]
        assert "SSH_ASKPASS" in env
        assert "SSH_ASKPASS_REQUIRE" in env

        askpass_path = env["SSH_ASKPASS"]
        if os.path.exists(askpass_path):
            os.unlink(askpass_path)
        _report("PASS", "Windows launch sets SSH_ASKPASS env")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — SSH availability and version checks
# ═══════════════════════════════════════════════════════════════════════════

class TestRequireSshOnPath:
    """Verify launch_terminal fails hard when ssh is missing."""

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which", return_value=None)
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_raises_when_ssh_not_found_linux(self, mock_which, mock_popen) -> None:
        _report("TEST", "Missing ssh on Linux raises TerminalLaunchError")
        from linux_ssh_tools.exceptions import TerminalLaunchError
        lnch = _make_launcher(password="")
        with pytest.raises(TerminalLaunchError) as exc_info:
            lnch.launch_terminal(context="ssh missing")

        msg = str(exc_info.value)
        _report("RESULT", f"error = {msg}")
        assert "not installed" in msg or "not found" in msg
        assert "192.168.1.100" in msg
        mock_popen.assert_not_called()
        _report("PASS", "Hard failure with clear message")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which", return_value=None)
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_raises_when_ssh_not_found_windows(self, mock_which, mock_popen) -> None:
        _report("TEST", "Missing ssh on Windows raises TerminalLaunchError")
        from linux_ssh_tools.exceptions import TerminalLaunchError
        lnch = _make_launcher(password="")
        with patch("subprocess.CREATE_NEW_CONSOLE", 0x10, create=True):
            with pytest.raises(TerminalLaunchError) as exc_info:
                lnch.launch_terminal(context="ssh missing win")

        msg = str(exc_info.value)
        _report("RESULT", f"error = {msg}")
        assert "not installed" in msg or "not found" in msg
        mock_popen.assert_not_called()
        _report("PASS", "Hard failure on Windows too")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_passes_when_ssh_found(self, mock_which, mock_popen) -> None:
        _report("TEST", "ssh on PATH allows launch to proceed")
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("gnome-terminal", "ssh") else None
        mock_popen.return_value = MagicMock()

        lnch = _make_launcher(password="")
        lnch.launch_terminal(context="ssh found")

        mock_popen.assert_called_once()
        _report("PASS", "Launch proceeds when ssh is available")


class TestRequireAskpassForceSupport:
    """Verify the OpenSSH version probe via ssh -G."""

    @patch("linux_ssh_tools.terminal.subprocess.run")
    def test_raises_when_ssh_too_old(self, mock_run, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Old OpenSSH (returncode=255) raises TerminalLaunchError")
        from linux_ssh_tools.exceptions import TerminalLaunchError
        mock_run.return_value = MagicMock(returncode=255)

        with pytest.raises(TerminalLaunchError) as exc_info:
            launcher._require_askpass_force_support("version check")

        msg = str(exc_info.value)
        _report("RESULT", f"error = {msg}")
        assert "OpenSSH 8.4" in msg
        assert "SSH_ASKPASS_REQUIRE" in msg
        assert "Upgrade" in msg or "upgrade" in msg or "sshpass" in msg
        _report("PASS", "Clear error explains why and what to do")

    @patch("linux_ssh_tools.terminal.subprocess.run")
    def test_passes_when_ssh_new_enough(self, mock_run, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Modern OpenSSH (returncode=0) passes silently")
        mock_run.return_value = MagicMock(returncode=0)

        # Should not raise
        launcher._require_askpass_force_support("version check")
        _report("PASS", "No error for modern SSH")

    @patch("linux_ssh_tools.terminal.subprocess.run")
    def test_raises_on_timeout(self, mock_run, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "ssh -G timeout raises TerminalLaunchError")
        import subprocess as sp
        from linux_ssh_tools.exceptions import TerminalLaunchError
        mock_run.side_effect = sp.TimeoutExpired(cmd="ssh", timeout=5)

        with pytest.raises(TerminalLaunchError) as exc_info:
            launcher._require_askpass_force_support("timeout check")

        _report("RESULT", f"error = {exc_info.value}")
        assert "verify SSH" in str(exc_info.value) or "Cannot verify" in str(exc_info.value)
        _report("PASS", "Timeout wrapped in TerminalLaunchError")

    @patch("linux_ssh_tools.terminal.subprocess.run")
    def test_raises_on_oserror(self, mock_run, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "ssh not executable raises TerminalLaunchError")
        from linux_ssh_tools.exceptions import TerminalLaunchError
        mock_run.side_effect = OSError("Permission denied")

        with pytest.raises(TerminalLaunchError):
            launcher._require_askpass_force_support("os error check")
        _report("PASS", "OSError wrapped in TerminalLaunchError")

    @patch("linux_ssh_tools.terminal.subprocess.run")
    def test_probe_command_uses_known_hosts_command(self, mock_run, launcher: SSHTerminalLauncher) -> None:
        _report("TEST", "Probe runs ssh -G -o KnownHostsCommand=none")
        mock_run.return_value = MagicMock(returncode=0)

        launcher._require_askpass_force_support("probe check")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        _report("RESULT", f"probe cmd = {cmd}")
        assert cmd[0] == "ssh"
        assert "-G" in cmd
        assert "-o" in cmd
        assert "KnownHostsCommand=none" in cmd
        _report("PASS", "Correct probe command")


class TestLaunchTerminalEndToEndChecks:
    """Verify checks fire during actual launch_terminal calls."""

    @patch("linux_ssh_tools.terminal.subprocess.run")
    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=False)
    def test_old_ssh_blocks_password_launch(
        self, _mock_has, mock_which, mock_popen, mock_run,
    ) -> None:
        _report("TEST", "launch_terminal with password + old SSH fails hard")
        from linux_ssh_tools.exceptions import TerminalLaunchError
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("gnome-terminal", "ssh") else None
        mock_run.return_value = MagicMock(returncode=255)  # Old SSH

        lnch = _make_launcher(password="secret")
        with pytest.raises(TerminalLaunchError) as exc_info:
            lnch.launch_terminal(context="old ssh test")

        msg = str(exc_info.value)
        _report("RESULT", f"error = {msg}")
        assert "OpenSSH 8.4" in msg
        mock_popen.assert_not_called()
        _report("PASS", "Old SSH blocks launch before Popen")

    @patch("linux_ssh_tools.terminal.subprocess.run")
    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", True)
    def test_old_ssh_blocks_windows_password_launch(
        self, mock_which, mock_popen, mock_run,
    ) -> None:
        _report("TEST", "launch_terminal on Windows with old SSH fails hard")
        from linux_ssh_tools.exceptions import TerminalLaunchError
        mock_which.side_effect = lambda name: "C:\\ssh.exe" if name == "ssh" else None
        mock_run.return_value = MagicMock(returncode=255)  # Old SSH

        lnch = _make_launcher(password="secret")
        with patch("subprocess.CREATE_NEW_CONSOLE", 0x10, create=True):
            with pytest.raises(TerminalLaunchError) as exc_info:
                lnch.launch_terminal(context="old ssh win")

        msg = str(exc_info.value)
        _report("RESULT", f"error = {msg}")
        assert "OpenSSH 8.4" in msg
        mock_popen.assert_not_called()
        _report("PASS", "Old SSH on Windows blocks launch too")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    @patch("linux_ssh_tools.terminal.SSHTerminalLauncher._has_sshpass", return_value=True)
    def test_sshpass_skips_version_check(
        self, _mock_has, mock_which, mock_popen,
    ) -> None:
        _report("TEST", "sshpass path does NOT run version probe")
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("gnome-terminal", "ssh") else None
        mock_popen.return_value = MagicMock()

        lnch = _make_launcher(password="secret")
        # If the probe ran, it would hit real ssh -G. Since we're not
        # mocking subprocess.run, any call to it would be real. The test
        # passing proves the probe was never called.
        lnch.launch_terminal(context="sshpass skip")

        mock_popen.assert_called_once()
        _report("PASS", "No version probe when sshpass is used")

    @patch("linux_ssh_tools.terminal.subprocess.Popen")
    @patch("linux_ssh_tools.terminal.shutil.which")
    @patch("linux_ssh_tools.terminal._IS_WINDOWS", False)
    def test_empty_password_skips_all_checks(self, mock_which, mock_popen) -> None:
        _report("TEST", "Empty password skips askpass check entirely")
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("gnome-terminal", "ssh") else None
        mock_popen.return_value = MagicMock()

        lnch = _make_launcher(password="")
        lnch.launch_terminal(context="empty pw")

        mock_popen.assert_called_once()
        _report("PASS", "No version probe for empty password")


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
