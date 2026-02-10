"""Interactive terminal launcher for SSH sessions."""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from typing import Optional, List

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
        """Build the base SSH command argument list."""
        hostname = self.connection_manager.hostname
        username = self.connection_manager.username

        args = [
            "ssh",
            f"{username}@{hostname}",
            "-p", str(self.connection_manager.port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
        ]

        if initial_command:
            args.extend(["-t", initial_command])

        return args

    def _get_windows_terminal_command(
        self,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
    ) -> List[str]:
        """Get command to launch Windows Terminal with SSH."""
        hostname = self.connection_manager.hostname
        ssh_args = self._build_ssh_args(initial_command)

        return [
            "wt",
            "-w", "0",
            "--title", window_title or f"SSH: {hostname}",
            "--", *ssh_args,
        ]

    def _get_cmd_terminal_command(
        self,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
    ) -> List[str]:
        """Get command to launch CMD terminal with SSH."""
        hostname = self.connection_manager.hostname
        ssh_args = self._build_ssh_args(initial_command)

        title = window_title or f"SSH: {hostname}"
        # cmd /k executes a compound command: set the title, then run ssh
        return [
            "cmd", "/k",
            "title " + title + " & " + " ".join(ssh_args),
        ]

    def _get_linux_terminal_command(
        self,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
    ) -> List[str]:
        """Get command to launch a Linux terminal emulator with SSH."""
        ssh_args = self._build_ssh_args(initial_command)
        hostname = self.connection_manager.hostname
        title = window_title or f"SSH: {hostname}"

        # Try common terminal emulators in order of preference
        for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
            if shutil.which(term):
                if term == "gnome-terminal":
                    return [term, "--title", title, "--", *ssh_args]
                if term == "xterm":
                    return [term, "-T", title, "-e", *ssh_args]
                # x-terminal-emulator (Debian/Ubuntu default)
                return [term, "-e", *ssh_args]

        # Fallback: just run ssh directly (no new window)
        return ssh_args

    def launch_terminal(
        self,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
        use_windows_terminal: bool = True,
    ) -> subprocess.Popen:
        """Launch interactive SSH terminal.

        Args:
            initial_command: Command to run immediately in the terminal
            window_title: Custom window title
            use_windows_terminal: Use Windows Terminal if available (Windows only)

        Returns:
            subprocess.Popen object

        Raises:
            TerminalLaunchError: If terminal launch fails
        """
        try:
            if _IS_WINDOWS:
                if use_windows_terminal and shutil.which("wt"):
                    cmd = self._get_windows_terminal_command(initial_command, window_title)
                else:
                    cmd = self._get_cmd_terminal_command(initial_command, window_title)
            else:
                cmd = self._get_linux_terminal_command(initial_command, window_title)

            kwargs = {}
            if _IS_WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE

            process = subprocess.Popen(cmd, **kwargs)  # type: ignore[call-overload]
            return process

        except Exception as e:
            raise TerminalLaunchError(
                f"Failed to launch terminal for {self.connection_manager.hostname}: {str(e)}"
            ) from e

    def launch_with_auto_reconnect(
        self,
        initial_command: Optional[str] = None,
        window_title: Optional[str] = None,
        max_attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> subprocess.Popen:
        """Launch terminal with automatic reconnect attempts.

        Args:
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
                return self.launch_terminal(initial_command, window_title)
            except Exception as e:
                last_error = e
                if attempt < max_attempts - 1:
                    time.sleep(retry_delay)

        if last_error is not None:
            raise last_error

        raise TerminalLaunchError(
            f"Failed to launch terminal after {max_attempts} attempts"
        )
