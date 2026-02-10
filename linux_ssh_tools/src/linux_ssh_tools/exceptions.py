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


class SerialCommunicationError(Exception):
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
    """
    pass
