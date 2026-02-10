"""Pytest configuration — path setup and logging for full visibility."""

import logging
import os
import sys

# ---------------------------------------------------------------------------
# Path setup: ensure the package is importable regardless of installation
# ---------------------------------------------------------------------------
_SRC_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, "linux_ssh_tools", "src"
)
_SRC_DIR = os.path.normpath(_SRC_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# ---------------------------------------------------------------------------
# Logging: route all library log output to the console so pytest -s shows it
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet down paramiko's own noisy transport-level debug logs
logging.getLogger("paramiko").setLevel(logging.WARNING)
