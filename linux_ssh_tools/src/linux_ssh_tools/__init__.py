"""
Linux SSH Tools - Robust SSH utilities for managing Linux devices from Windows

This package provides a comprehensive set of tools for interacting with Linux devices
via SSH from a Windows 10 PC. It includes:

- **Robust file transfer** with speed monitoring and verification
- **Command execution** with detailed error reporting
- **Interactive terminal** launch with auto-approval of SSH fingerprints
- **Connection management** with automatic cleanup

All operations bypass SSH fingerprint verification for stateless hardware devices
on internal networks, with full error reporting and validation.
"""

import os
from typing import List

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
