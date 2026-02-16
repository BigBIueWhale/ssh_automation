"""
Linux SSH Tools - Robust SSH & serial utilities for managing Linux devices

This package provides a comprehensive set of tools for interacting with Linux devices
from Windows 10 or Ubuntu 24.04. It includes:

- **Robust file transfer** with speed monitoring and verification
- **Command execution** with detailed error reporting
- **Interactive terminal** launch with auto-approval of SSH fingerprints
- **Connection management** with automatic cleanup
- **Serial communication** with flush-then-read pattern for hardware consoles

All operations bypass SSH fingerprint verification for stateless hardware devices
on internal networks, with full error reporting and validation.
"""

import logging
import os
from typing import List

logging.getLogger("linux_ssh_tools").addHandler(logging.NullHandler())

__version__ = "0.1.0"

# Default configuration for Linux devices.
# Credentials are read from environment variables — no secrets in the codebase.
#   SSH_DEVICE_0_HOST / SSH_DEVICE_0_USER / SSH_DEVICE_0_PASS
#   SSH_DEVICE_1_HOST / SSH_DEVICE_1_USER / SSH_DEVICE_1_PASS
DEFAULT_LINUX_DEVICES = [
    {
        "hostname": os.environ.get("SSH_DEVICE_0_HOST", "192.168.1.100"),
        "username": os.environ.get("SSH_DEVICE_0_USER", ""),
        "password": os.environ.get("SSH_DEVICE_0_PASS", ""),
    },
    {
        "hostname": os.environ.get("SSH_DEVICE_1_HOST", "192.168.1.101"),
        "username": os.environ.get("SSH_DEVICE_1_USER", ""),
        "password": os.environ.get("SSH_DEVICE_1_PASS", ""),
    },
]

# SSH port
SSH_PORT = 22

# Timeout settings
CONNECTION_TIMEOUT = 30
COMMAND_TIMEOUT = 600

# File transfer settings
CHUNK_SIZE = 8192
PROGRESS_UPDATE_INTERVAL = 0.1

# Serial communication settings
SERIAL_BAUD_RATE = 115200
SERIAL_BYTESIZE = 8       # 8 data bits
SERIAL_PARITY = "N"       # No parity
SERIAL_STOPBITS = 1       # 1 stop bit
SERIAL_READ_TIMEOUT = 0  # seconds — non-blocking; timing managed by the poll loop, not the driver
SERIAL_WRITE_TIMEOUT = 10  # seconds — blocking with failsafe; prevents infinite hangs
SERIAL_DEFAULT_DURATION_MS = 2000  # default read duration in milliseconds
SERIAL_COMMAND_TIMEOUT_MS = 30000  # default serial command execution timeout
SERIAL_PROMPT_SETTLE_MS = 200     # pause after wake-up ENTER before sending command

# Default serial device paths per machine.
# On Windows these are COM ports (COM3, COM4, …).
# On Linux these are /dev/ttyS*, /dev/ttyUSB*, or /dev/ttyACM* paths.
# Override via environment variables:
#   SERIAL_DEVICE_0_PORT / SERIAL_DEVICE_0_PORT2
#   SERIAL_DEVICE_1_PORT / SERIAL_DEVICE_1_PORT2
DEFAULT_SERIAL_DEVICES = [
    {
        "port": os.environ.get("SERIAL_DEVICE_0_PORT", "/dev/ttyUSB0"),
        "port2": os.environ.get("SERIAL_DEVICE_0_PORT2", ""),
    },
    {
        "port": os.environ.get("SERIAL_DEVICE_1_PORT", "/dev/ttyUSB1"),
        "port2": os.environ.get("SERIAL_DEVICE_1_PORT2", ""),
    },
]
