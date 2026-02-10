"""Custom exceptions for SSH and serial operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .serial_comm import SerialCommandResult


class LinuxSSHToolsError(Exception):
    """Common base exception for all linux_ssh_tools errors."""
    pass


class SSHConnectionError(LinuxSSHToolsError):
    """Exception for SSH connection errors."""
    pass


class SSHTimeoutError(SSHConnectionError):
    """Exception for SSH connection timeouts."""
    pass


class SSHCommandError(LinuxSSHToolsError):
    """Exception for non-zero SSH command exit codes.

    Attributes:
        command: The command that failed.
        return_code: The non-zero exit code.
        stdout: Standard output from the command.
        stderr: Standard error from the command.
    """

    def __init__(
        self,
        message: str,
        *,
        command: str,
        return_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class FileTransferError(LinuxSSHToolsError):
    """Exception for file transfer errors."""
    pass


class TerminalLaunchError(LinuxSSHToolsError):
    """Exception for terminal launch errors."""
    pass


class SerialCommunicationError(LinuxSSHToolsError):
    """Base exception for serial communication errors.

    Raised when the serial port cannot be opened, configured, read from,
    or when any unexpected I/O failure occurs during serial operations.
    """
    pass


class SerialTimeoutError(SerialCommunicationError):
    """Exception for serial communication timeouts.

    Raised when a serial read operation completes its full duration
    without receiving any data, or when the port fails to respond
    within the expected timeframe.

    When raised by ``SerialCommandExecutor.execute_command``, the
    ``.result`` attribute contains the partial ``SerialCommandResult``.
    """

    def __init__(
        self,
        message: str,
        *,
        result: SerialCommandResult | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result
