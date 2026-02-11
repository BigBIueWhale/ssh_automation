"""Interactive terminal launcher for SSH sessions."""

from __future__ import annotations

import atexit
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Dict, Optional, List, Tuple, Union

from typeguard import typechecked

from .connection import SSHConnectionManager
from .exceptions import TerminalLaunchError

_IS_WINDOWS = platform.system() == "Windows"


@typechecked
class SSHTerminalLauncher:
    """Launches interactive SSH terminals with auto-approval and customization."""

    def __init__(self, connection_manager: SSHConnectionManager) -> None:
        """Initialize terminal launcher.

        Args:
            connection_manager: SSH connection manager
        """
        self.connection_manager = connection_manager

    def _build_ssh_args(self, initial_command: Optional[str] = None) -> List[str]:
        """Build the base SSH command argument list.

        When *initial_command* is given the remote session runs that command
        and then ``exec``-s the user's login shell so the terminal stays
        interactive.
        """
        hostname = self.connection_manager.hostname
        username = self.connection_manager.username
        null_file = "NUL" if _IS_WINDOWS else "/dev/null"

        args = [
            "ssh",
            f"{username}@{hostname}",
            "-p", str(self.connection_manager.port),
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={null_file}",
        ]

        if initial_command:
            # -t forces PTY allocation (required for interactive use).
            # After the requested command finishes, exec the user's login
            # shell so the session stays open for interactive control.
            args.extend(["-t", f"{initial_command} ; exec ${{SHELL:-/bin/bash}}"])

        return args

    # ------------------------------------------------------------------
    #  Password automation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _has_sshpass() -> bool:
        """Return True if sshpass is available on PATH."""
        return shutil.which("sshpass") is not None

    @staticmethod
    def _create_askpass_script(password: str) -> str:
        """Write a temporary script that echoes *password* for SSH_ASKPASS.

        On Linux the script is a POSIX shell script (mode 0700).
        On Windows the script is a ``.bat`` file.
        Returns the absolute path to the created file.
        """
        if _IS_WINDOWS:
            # Escape cmd.exe metacharacters so the password is literal.
            safe = password
            for ch in "^&|<>()":
                safe = safe.replace(ch, f"^{ch}")
            content = f"@echo off\necho {safe}\n"
            suffix = ".bat"
        else:
            # Single-quote the password; escape embedded single quotes.
            safe = password.replace("'", "'\\''")
            content = f"#!/bin/sh\necho '{safe}'\n"
            suffix = ".sh"

        fd, path = tempfile.mkstemp(suffix=suffix, prefix="ssh_askpass_")
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)

        if not _IS_WINDOWS:
            os.chmod(path, stat.S_IRWXU)  # 0700

        # Safety net: delete when the interpreter exits if still present.
        atexit.register(lambda p=path: os.unlink(p) if os.path.exists(p) else None)

        return path

    def _schedule_askpass_cleanup(self, path: str, delay: float = 5.0) -> None:
        """Delete the askpass script after *delay* seconds.

        Uses a daemon thread so it won't prevent interpreter shutdown.
        """
        def _cleanup() -> None:
            try:
                os.unlink(path)
            except OSError:
                pass

        t = threading.Timer(delay, _cleanup)
        t.daemon = True
        t.start()

    def _require_ssh_on_path(self, context: str) -> None:
        """Raise `.TerminalLaunchError` if ``ssh`` is not on PATH."""
        if not shutil.which("ssh"):
            raise TerminalLaunchError(
                f"[{context}] SSH client is not installed or not found on "
                f"PATH. Cannot launch terminal session to "
                f"{self.connection_manager.hostname}. Install OpenSSH and "
                f"ensure 'ssh' is available in your system PATH."
            )

    def _require_askpass_force_support(self, context: str) -> None:
        """Raise `.TerminalLaunchError` if SSH is too old for SSH_ASKPASS.

        Probes by asking ``ssh -G`` to resolve a config that includes
        ``KnownHostsCommand`` — a config keyword introduced in OpenSSH 8.5
        (one minor release after 8.4 which added
        ``SSH_ASKPASS_REQUIRE=force``).  No network connection is made;
        ``-G`` just parses config and exits.
        """
        probe_cmd = ["ssh", "-G", "-o", "KnownHostsCommand=none", "_probe"]
        probe_kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 5,
        }
        # Prevent a console window flash on Windows.
        _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if _IS_WINDOWS and _CREATE_NO_WINDOW:
            probe_kwargs["creationflags"] = _CREATE_NO_WINDOW

        try:
            result = subprocess.run(probe_cmd, **probe_kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TerminalLaunchError(
                f"[{context}] Cannot verify SSH feature support for "
                f"{self.connection_manager.hostname}: {exc}"
            ) from exc

        if result.returncode != 0:
            raise TerminalLaunchError(
                f"[{context}] Password automation requires OpenSSH 8.4+ "
                f"(which introduced SSH_ASKPASS_REQUIRE=force). The "
                f"installed SSH client does not support this — it failed "
                f"to recognize KnownHostsCommand, a config option from the "
                f"same era. Upgrade OpenSSH, use key-based authentication "
                f"(empty password), or on Linux install sshpass "
                f"(apt install sshpass)."
            )

    def _prepare_password_env(
        self,
    ) -> Tuple[List[str], Dict[str, str], Optional[str]]:
        """Choose a password-delivery strategy.

        Returns ``(ssh_prefix, env_vars, askpass_path)``:

        * *ssh_prefix*  — extra tokens to prepend before ``ssh`` (e.g.
          ``["sshpass", "-e"]``).
        * *env_vars*    — environment variables to merge into the child
          process environment.
        * *askpass_path* — path to a temporary askpass script that should
          be cleaned up after launch, or ``None``.
        """
        password = self.connection_manager.password
        if not password:
            return [], {}, None

        # Preferred: sshpass on Linux
        if not _IS_WINDOWS and self._has_sshpass():
            return ["sshpass", "-e"], {"SSHPASS": password}, None

        # Fallback: SSH_ASKPASS
        askpass_path = self._create_askpass_script(password)
        env: Dict[str, str] = {
            "SSH_ASKPASS": askpass_path,
            "SSH_ASKPASS_REQUIRE": "force",
        }
        if not _IS_WINDOWS:
            env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
        return [], env, askpass_path

    def _get_windows_terminal_command(
        self,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
        ssh_prefix: Optional[List[str]] = None,
    ) -> List[str]:
        """Get command to launch Windows Terminal with SSH."""
        hostname = self.connection_manager.hostname
        ssh_args = self._build_ssh_args(initial_command)
        prefix = ssh_prefix or []

        return [
            "wt",
            "-w", "0",
            "--title", window_title or f"SSH: {hostname}",
            "--", *prefix, *ssh_args,
        ]

    def _get_cmd_terminal_command(
        self,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
        ssh_prefix: Optional[List[str]] = None,
    ) -> str:
        """Get command to launch CMD terminal with SSH.

        Returns a *string* (not a list) because ``cmd /k`` interprets
        everything after ``/k`` as a single command line.  Returning a list
        would cause ``subprocess.list2cmdline`` to add outer quotes around
        the compound command, which prevents ``&`` from acting as a command
        separator inside cmd.exe.
        """
        hostname = self.connection_manager.hostname
        ssh_args = self._build_ssh_args(initial_command)
        prefix = ssh_prefix or []

        title = window_title or f"SSH: {hostname}"
        # Escape cmd.exe metacharacters in the title so they are literal.
        safe_title = title
        for ch in "&|<>^()":
            safe_title = safe_title.replace(ch, f"^{ch}")

        # list2cmdline produces correct Windows quoting for each ssh arg.
        ssh_cmd = subprocess.list2cmdline([*prefix, *ssh_args])
        return f"cmd /k title {safe_title}& {ssh_cmd}"

    def _get_linux_terminal_command(
        self,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
        ssh_prefix: Optional[List[str]] = None,
    ) -> List[str]:
        """Get command to launch a Linux terminal emulator with SSH."""
        ssh_args = self._build_ssh_args(initial_command)
        hostname = self.connection_manager.hostname
        title = window_title or f"SSH: {hostname}"
        prefix = ssh_prefix or []

        # Try common terminal emulators in order of preference
        for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
            if shutil.which(term):
                if term == "gnome-terminal":
                    return [term, "--title", title, "--", *prefix, *ssh_args]
                if term == "xterm":
                    return [term, "-T", title, "-e", *prefix, *ssh_args]
                # x-terminal-emulator (Debian/Ubuntu default)
                return [term, "-e", *prefix, *ssh_args]

        # Fallback: just run ssh directly (no new window)
        return [*prefix, *ssh_args]

    def launch_terminal(
        self,
        context: str,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
        use_windows_terminal: bool = True,
    ) -> subprocess.Popen:
        """Launch interactive SSH terminal.

        Args:
            context: Description of the purpose, embedded into error messages.
            initial_command: Command to run immediately in the terminal
            window_title: Custom window title
            use_windows_terminal: Use Windows Terminal if available (Windows only)

        Returns:
            subprocess.Popen object

        Raises:
            TerminalLaunchError: If terminal launch fails
        """
        try:
            self._require_ssh_on_path(context)

            ssh_prefix, password_env, askpass_path = self._prepare_password_env()

            if askpass_path is not None:
                self._require_askpass_force_support(context)

            if _IS_WINDOWS:
                if use_windows_terminal and shutil.which("wt"):
                    cmd = self._get_windows_terminal_command(
                        initial_command, window_title, ssh_prefix=ssh_prefix,
                    )
                else:
                    cmd = self._get_cmd_terminal_command(
                        initial_command, window_title, ssh_prefix=ssh_prefix,
                    )
            else:
                cmd = self._get_linux_terminal_command(
                    initial_command, window_title, ssh_prefix=ssh_prefix,
                )

            kwargs: dict = {}
            if _IS_WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE

            if password_env:
                env = os.environ.copy()
                env.update(password_env)
                kwargs["env"] = env

            process = subprocess.Popen(cmd, **kwargs)  # type: ignore[call-overload]

            if askpass_path:
                self._schedule_askpass_cleanup(askpass_path)

            return process

        except Exception as e:
            raise TerminalLaunchError(
                f"[{context}] Failed to launch terminal for {self.connection_manager.hostname}: {str(e)}"
            ) from e

    def launch_with_auto_reconnect(
        self,
        context: str,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
        max_attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> subprocess.Popen:
        """Launch terminal with automatic reconnect attempts.

        Args:
            context: Description of the purpose, embedded into error messages.
            initial_command: Command to run immediately in the terminal
            window_title: Custom window title
            max_attempts: Maximum number of launch attempts
            retry_delay: Delay between attempts in seconds

        Returns:
            subprocess.Popen object
        """
        last_error: Optional[Exception] = None

        for attempt in range(max_attempts):
            try:
                return self.launch_terminal(context, initial_command, window_title)
            except Exception as e:
                last_error = e
                if attempt < max_attempts - 1:
                    time.sleep(retry_delay)

        if last_error is not None:
            raise last_error

        raise TerminalLaunchError(
            f"[{context}] Failed to launch terminal after {max_attempts} attempts"
        )
