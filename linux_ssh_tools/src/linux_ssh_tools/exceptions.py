"""Custom exceptions for SSH operations."""


class SSHConnectionError(Exception):
    """Base exception for SSH connection errors."""
    pass


class SSHTimeoutError(SSHConnectionError):
    """Exception for SSH connection timeouts."""
    pass


class FileTransferError(Exception):
    """Exception for file transfer errors."""
    pass


class TerminalLaunchError(Exception):
    """Exception for terminal launch errors."""
    pass
