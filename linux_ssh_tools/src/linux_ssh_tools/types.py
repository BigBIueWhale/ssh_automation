"""Type definitions for Linux SSH Tools."""

from typing import Dict, Tuple, Optional

# Connection types
ConnectionResult = Tuple[int, str, str]  # (return_code, stdout, stderr)
TransferResult = Tuple[int, float]  # (bytes_transferred, transfer_speed)

# Serial communication types
SerialResult = Tuple[str, int, float]  # (received_data, bytes_received, duration_seconds)

# Device configuration
DeviceConfig = Dict[str, str]  # {"hostname": str, "username": str, "password": str}
