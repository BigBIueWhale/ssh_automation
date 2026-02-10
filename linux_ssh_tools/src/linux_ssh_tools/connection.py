"""SSH connection management with fingerprint bypass for internal networks."""

from __future__ import annotations

import logging
import socket
import time
from pathlib import Path
from typing import Optional, Tuple

import paramiko
from typeguard import typechecked

from . import SSH_PORT, CONNECTION_TIMEOUT, COMMAND_TIMEOUT
from .exceptions import SSHConnectionError, SSHTimeoutError, SSHCommandError

logger = logging.getLogger("linux_ssh_tools.connection")


class SSHConnectionManager:
    """Manages SSH connections with automatic fingerprint bypass for internal networks."""

    def __init__(
        self,
        hostname: str,
        username: str,
        password: str,
        port: int = SSH_PORT,
        timeout: int = CONNECTION_TIMEOUT,
    ) -> None:
        """Initialize SSH connection manager.

        Args:
            hostname: IP address or hostname of Linux device
            username: SSH username
            password: SSH password
            port: SSH port (default: 22)
            timeout: Connection timeout in seconds (default: 30)
        """
        self.hostname = hostname
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.ssh_client: Optional[paramiko.SSHClient] = None

        # Clear SSH fingerprints for this host
        self._clear_ssh_fingerprints()

    def _clear_ssh_fingerprints(self) -> None:
        """Clear SSH fingerprints for this host to bypass verification."""
        known_hosts_file = Path.home() / ".ssh" / "known_hosts"

        if not known_hosts_file.is_file():
            return

        try:
            lines = known_hosts_file.read_text(encoding="utf-8").splitlines(True)

            # Filter out entries for this hostname
            filtered_lines = [
                line for line in lines
                if self.hostname not in line and f"[{self.hostname}]" not in line
            ]

            # Write back if changed
            if len(filtered_lines) != len(lines):
                removed = len(lines) - len(filtered_lines)
                logger.info(
                    "[FINGERPRINT] Cleared %d known_hosts entries for %s",
                    removed, self.hostname,
                )
                known_hosts_file.write_text("".join(filtered_lines), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[FINGERPRINT] Could not clear fingerprints for %s: %s",
                self.hostname, exc,
            )

    def _get_ssh_client(self) -> paramiko.SSHClient:
        """Get or create SSH client with auto-approval policy."""
        if self.ssh_client is None:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        return self.ssh_client

    def connect(self, context: str) -> None:
        """Establish SSH connection to the Linux device.

        Args:
            context: Description of the purpose of this connection,
                embedded into error messages.

        Raises:
            SSHConnectionError: If connection fails
            SSHTimeoutError: If connection times out
        """
        logger.info(
            "[CONNECT] [%s] Attempting SSH connection to %s@%s:%d (timeout=%ds) ...",
            context, self.username, self.hostname, self.port, self.timeout,
        )
        start_time = time.time()

        try:
            client = self._get_ssh_client()

            client.connect(
                hostname=self.hostname,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
            )

            elapsed = time.time() - start_time
            logger.info(
                "[CONNECT] [%s] Successfully connected to %s@%s:%d in %.2fs",
                context, self.username, self.hostname, self.port, elapsed,
            )

        except socket.timeout as e:
            elapsed = time.time() - start_time
            msg = (
                f"[{context}] Connection to {self.hostname}:{self.port} timed out "
                f"after {elapsed:.1f}s (limit {self.timeout}s)"
            )
            logger.error("[CONNECT] TIMEOUT — %s", msg)
            raise SSHTimeoutError(msg) from e
        except paramiko.AuthenticationException as e:
            msg = f"[{context}] Authentication failed for {self.username}@{self.hostname}:{self.port}: {e}"
            logger.error("[CONNECT] AUTH FAILED — %s", msg)
            raise SSHConnectionError(msg) from e
        except paramiko.SSHException as e:
            elapsed = time.time() - start_time
            msg = f"[{context}] SSH error connecting to {self.hostname}:{self.port}: {e}"
            logger.error("[CONNECT] SSH ERROR after %.2fs — %s", elapsed, msg)
            raise SSHConnectionError(msg) from e
        except OSError as e:
            elapsed = time.time() - start_time
            msg = f"[{context}] OS/network error connecting to {self.hostname}:{self.port}: {e}"
            logger.error("[CONNECT] OS ERROR after %.2fs — %s", elapsed, msg)
            raise SSHConnectionError(msg) from e

    def is_connected(self) -> bool:
        """Check if SSH connection is active."""
        if self.ssh_client is None:
            return False
        transport = self.ssh_client.get_transport()
        return transport is not None and transport.is_active()

    def disconnect(self) -> None:
        """Close SSH connection if open."""
        was_connected = self.is_connected()
        target = f"{self.username}@{self.hostname}:{self.port}"

        if self.ssh_client is not None:
            try:
                self.ssh_client.close()
            except Exception as exc:
                logger.warning("[DISCONNECT] Error closing connection to %s: %s", target, exc)
            finally:
                self.ssh_client = None

        if was_connected:
            logger.info("[DISCONNECT] Disconnected from %s", target)
        else:
            logger.debug("[DISCONNECT] disconnect() called on already-closed connection to %s", target)

    def __enter__(self) -> SSHConnectionManager:
        """Context manager entry."""
        self.connect(context=f"Connecting to {self.hostname}:{self.port}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        """Context manager exit - ensure connection is closed."""
        self.disconnect()

    def __del__(self) -> None:
        """Destructor - ensure connection is closed."""
        try:
            self.disconnect()
        except Exception:
            pass


@typechecked
class SSHCommandExecutor:
    """Executes commands on remote Linux devices with robust error handling."""

    def __init__(self, connection_manager: SSHConnectionManager) -> None:
        """Initialize command executor.

        Args:
            connection_manager: Active SSH connection manager
        """
        self.connection_manager = connection_manager

    def execute_command(
        self,
        command: str,
        context: str,
        timeout: int = COMMAND_TIMEOUT,
        get_pty: bool = False,
    ) -> Tuple[int, str, str]:
        """Execute command on remote device.

        Args:
            command: Command to execute
            context: Description of the purpose, embedded into error messages.
            timeout: Command timeout in seconds
            get_pty: Whether to allocate a pseudo-terminal

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            SSHConnectionError: If connection is not active
            SSHCommandError: If the command exits with a non-zero return code
        """
        host = self.connection_manager.hostname
        port = self.connection_manager.port

        if not self.connection_manager.is_connected():
            msg = f"[{context}] Cannot execute command: not connected to {host}:{port}"
            logger.error("[EXEC] %s", msg)
            raise SSHConnectionError(msg)

        logger.info("[EXEC] [%s] Running on %s:%d — %r", context, host, port, command)

        client = self.connection_manager._get_ssh_client()

        try:
            stdin, stdout, stderr = client.exec_command(
                command,
                timeout=timeout,
                get_pty=get_pty,
            )

            stdout_output = stdout.read().decode("utf-8", errors="replace")
            stderr_output = stderr.read().decode("utf-8", errors="replace")
            return_code = stdout.channel.recv_exit_status()

            logger.info(
                "[EXEC] [%s] Completed on %s:%d — rc=%d, stdout=%d bytes, stderr=%d bytes",
                context, host, port, return_code, len(stdout_output), len(stderr_output),
            )

            if return_code != 0:
                msg = (
                    f"[{context}] Command {command!r} failed on {host}:{port} "
                    f"with exit code {return_code}. "
                    f"stderr: {stderr_output.strip()[:200] or '(empty)'}"
                )
                logger.warning("[EXEC] %s", msg)
                raise SSHCommandError(
                    msg,
                    command=command,
                    return_code=return_code,
                    stdout=stdout_output,
                    stderr=stderr_output,
                )

            return (return_code, stdout_output, stderr_output)

        except SSHCommandError:
            raise
        except paramiko.SSHException as e:
            msg = f"[{context}] Error executing command '{command}' on {host}:{port}: {e}"
            logger.error("[EXEC] SSH ERROR — %s", msg)
            raise SSHConnectionError(msg) from e
        except Exception as e:
            msg = f"[{context}] Unexpected error executing command '{command}' on {host}:{port}: {e}"
            logger.error("[EXEC] UNEXPECTED ERROR — %s", msg)
            raise SSHConnectionError(msg) from e

    def execute_with_retry(
        self,
        command: str,
        context: str,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        timeout: int = COMMAND_TIMEOUT,
    ) -> Tuple[int, str, str]:
        """Execute command with retry logic.

        Only retries on connection errors. Command errors (non-zero exit
        code) propagate immediately without retry.

        Args:
            command: Command to execute
            context: Description of the purpose, embedded into error messages.
            max_retries: Maximum number of retries
            retry_delay: Delay between retries in seconds
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            SSHCommandError: Immediately on non-zero exit code (no retry).
            SSHConnectionError: After all retries are exhausted.
        """
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(
                        "[RETRY] [%s] Attempt %d/%d for %r on %s:%d",
                        context, attempt + 1, max_retries, command,
                        self.connection_manager.hostname,
                        self.connection_manager.port,
                    )
                return self.execute_command(command, context, timeout, get_pty=False)
            except SSHConnectionError as e:
                last_error = e
                logger.warning(
                    "[RETRY] [%s] Attempt %d/%d failed for %r: %s",
                    context, attempt + 1, max_retries, command, e,
                )
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)

        if last_error is not None:
            raise last_error

        raise SSHConnectionError(f"[{context}] Command execution failed after {max_retries} attempts")
