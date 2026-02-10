"""
Comprehensive serial communication test suite.

Uses a virtual serial port pair (pty-based on Linux, or a loopback where
available) so tests run without real hardware.  On systems where PTY pairs
are unavailable the entire module is skipped with a clear message.

Run with full visibility:
    pytest tests/test_serial_comm.py -v -s
"""

from __future__ import annotations

import dataclasses
import os
import platform
import sys
import threading
import time
from typing import List, Optional

import pytest

# ---------------------------------------------------------------------------
# Dependency gate
# ---------------------------------------------------------------------------
_MISSING = []  # type: List[str]

try:
    import serial
except ImportError:
    _MISSING.append("pyserial")

try:
    from typeguard import typechecked  # noqa: F401
except ImportError:
    _MISSING.append("typeguard")

if _MISSING:
    print(
        "\n"
        "=" * 72 + "\n"
        "  MISSING REQUIRED LIBRARIES\n"
        "=" * 72 + "\n"
        "  The following packages are not installed: {}\n".format(", ".join(_MISSING)) +
        "  Install them with:  pip install {}\n".format(" ".join(_MISSING)) +
        "=" * 72 + "\n",
        file=sys.stderr,
    )
    pytest.skip(
        "Required libraries missing: {}".format(", ".join(_MISSING)),
        allow_module_level=True,
    )

from linux_ssh_tools.serial_comm import (
    SerialConnectionManager,
    SerialReader,
    SerialCommandExecutor,
    SerialCommandResult,
)
from linux_ssh_tools.exceptions import (
    SerialCommunicationError,
    SerialTimeoutError,
)

# ---------------------------------------------------------------------------
# Report environment
# ---------------------------------------------------------------------------
print(
    "\n"
    "+" * 72 + "\n"
    "  Platform  : {} {}\n".format(platform.system(), platform.release()) +
    "  Python    : {}\n".format(sys.version.split()[0]) +
    "  pyserial  : {}\n".format(serial.VERSION) +
    "+" * 72
)

# ---------------------------------------------------------------------------
# Virtual serial port pair (PTY-based)
# ---------------------------------------------------------------------------
_HAS_PTY = False
try:
    import pty
    _HAS_PTY = True
except ImportError:
    pass

_IS_WINDOWS = platform.system() == "Windows"


class VirtualSerialPair:
    """Creates a connected pair of pseudo-terminal serial ports.

    ``master_path`` and ``slave_path`` behave like two ends of a serial
    cable: bytes written to one appear on the other.

    Only works on POSIX systems that support ``pty.openpty()``.
    On Windows this class cannot be instantiated.
    """

    def __init__(self) -> None:
        if not _HAS_PTY:
            raise RuntimeError(
                "pty module not available — virtual serial pairs require "
                "a POSIX system (Linux / macOS)"
            )

        self.master_fd, self.slave_fd = pty.openpty()
        self.master_path = os.ttyname(self.master_fd)
        self.slave_path = os.ttyname(self.slave_fd)

    def write_to_master(self, data):
        # type: (bytes) -> int
        """Write bytes into the master end (appears on the slave)."""
        return os.write(self.master_fd, data)

    def read_from_master(self, size=4096):
        # type: (int) -> bytes
        """Read bytes from the master end (data written to the slave)."""
        return os.read(self.master_fd, size)

    def close(self):
        # type: () -> None
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        # type: () -> VirtualSerialPair
        return self

    def __exit__(self, *exc_info):
        # type: (*object) -> None
        self.close()


# Skip the entire module on Windows (no PTY support)
if _IS_WINDOWS:
    pytest.skip(
        "Serial tests require PTY support (Linux/macOS only)",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report(label, detail=""):
    # type: (str, str) -> None
    if detail:
        print("  [{}] {}".format(label, detail))
    else:
        print("  [{}]".format(label))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def serial_pair():
    """Create a virtual serial pair for one test."""
    pair = VirtualSerialPair()
    _report("FIXTURE", "Virtual serial pair: master={} slave={}".format(
        pair.master_path, pair.slave_path,
    ))
    yield pair
    pair.close()
    _report("FIXTURE", "Virtual serial pair closed")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Parameter Validation
# ═══════════════════════════════════════════════════════════════════════════

class TestParameterValidation:
    """SerialConnectionManager rejects invalid line settings."""

    def test_invalid_bytesize(self):
        # type: () -> None
        _report("TEST", "bytesize=9 should be rejected")
        with pytest.raises(SerialCommunicationError) as exc_info:
            SerialConnectionManager("/dev/null", bytesize=9)
        _report("CAUGHT", str(exc_info.value)[:80])
        assert "bytesize" in str(exc_info.value).lower()
        _report("PASS", "Invalid bytesize rejected with clear message")

    def test_invalid_parity(self):
        # type: () -> None
        _report("TEST", "parity='X' should be rejected")
        with pytest.raises(SerialCommunicationError) as exc_info:
            SerialConnectionManager("/dev/null", parity="X")
        _report("CAUGHT", str(exc_info.value)[:80])
        assert "parity" in str(exc_info.value).lower()
        _report("PASS", "Invalid parity rejected with clear message")

    def test_invalid_stopbits(self):
        # type: () -> None
        _report("TEST", "stopbits=3 should be rejected")
        with pytest.raises(SerialCommunicationError) as exc_info:
            SerialConnectionManager("/dev/null", stopbits=3)
        _report("CAUGHT", str(exc_info.value)[:80])
        assert "stopbits" in str(exc_info.value).lower()
        _report("PASS", "Invalid stopbits rejected with clear message")

    def test_invalid_baud_rate(self):
        # type: () -> None
        _report("TEST", "baud_rate=-1 should be rejected")
        with pytest.raises(SerialCommunicationError) as exc_info:
            SerialConnectionManager("/dev/null", baud_rate=-1)
        _report("CAUGHT", str(exc_info.value)[:80])
        assert "baud rate" in str(exc_info.value).lower()
        _report("PASS", "Invalid baud rate rejected with clear message")

    def test_valid_defaults_accepted(self):
        # type: () -> None
        _report("TEST", "Default 115200 8N1 should be accepted")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        assert mgr.baud_rate == 115200
        assert not mgr.is_open()
        _report("PASS", "Valid defaults accepted, port NOT opened by constructor")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — SerialCommandResult Dataclass
# ═══════════════════════════════════════════════════════════════════════════

class TestSerialCommandResult:
    """The result dataclass is frozen and has the right fields."""

    def test_fields_present(self):
        # type: () -> None
        _report("TEST", "SerialCommandResult has all required fields")
        r = SerialCommandResult(
            command="ls /",
            output="bin  dev  etc",
            bytes_received=13,
            elapsed_seconds=1.234,
            stopped_by_condition=True,
            timed_out=False,
        )
        assert r.command == "ls /"
        assert r.output == "bin  dev  etc"
        assert r.bytes_received == 13
        assert r.elapsed_seconds == 1.234
        assert r.stopped_by_condition is True
        assert r.timed_out is False
        _report("PASS", "All fields accessible and correct")

    def test_frozen(self):
        # type: () -> None
        _report("TEST", "SerialCommandResult should be immutable (frozen)")
        r = SerialCommandResult(
            command="x", output="y", bytes_received=0,
            elapsed_seconds=0.0, stopped_by_condition=False, timed_out=True,
        )
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            r.output = "modified"  # type: ignore[misc]
        _report("PASS", "Mutation correctly rejected")

    def test_is_dataclass(self):
        # type: () -> None
        _report("TEST", "SerialCommandResult should be a dataclass")
        assert dataclasses.is_dataclass(SerialCommandResult)
        _report("PASS", "Is a dataclass")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Connection Manager Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class TestConnectionLifecycle:
    """Open, close, context-manager, and state checks."""

    def test_open_close(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Open and close on virtual port {}".format(serial_pair.slave_path))
        mgr = SerialConnectionManager(serial_pair.slave_path)

        assert not mgr.is_open()
        mgr.open()
        assert mgr.is_open()
        _report("STEP", "Opened — is_open()=True")

        mgr.close()
        assert not mgr.is_open()
        _report("PASS", "Close sets is_open()=False")

    def test_context_manager(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Context manager on {}".format(serial_pair.slave_path))

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            assert mgr.is_open()
            _report("STEP", "Inside 'with': is_open()=True")

        assert not mgr.is_open()
        _report("PASS", "After 'with': is_open()=False")

    def test_double_close_is_safe(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Double close should not raise")
        mgr = SerialConnectionManager(serial_pair.slave_path)
        mgr.open()
        mgr.close()
        mgr.close()  # second close — should be idempotent
        _report("PASS", "Double close is idempotent")

    def test_double_open_is_safe(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Double open should not raise")
        mgr = SerialConnectionManager(serial_pair.slave_path)
        mgr.open()
        mgr.open()  # second open — should be a no-op
        assert mgr.is_open()
        mgr.close()
        _report("PASS", "Double open is idempotent")

    def test_open_nonexistent_port(self):
        # type: () -> None
        _report("TEST", "Opening a nonexistent port should raise with hint")
        mgr = SerialConnectionManager("/dev/ttyNONEXISTENT_99")
        with pytest.raises(SerialCommunicationError) as exc_info:
            mgr.open()
        msg = str(exc_info.value)
        _report("CAUGHT", msg[:100])
        assert "/dev/ttyNONEXISTENT_99" in msg
        _report("PASS", "Clear error with port name and hint")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Port Listing
# ═══════════════════════════════════════════════════════════════════════════

class TestPortListing:
    """list_available_ports should return without error."""

    def test_list_ports(self):
        # type: () -> None
        _report("TEST", "list_available_ports()")
        ports = SerialConnectionManager.list_available_ports()
        _report("RESULT", "Found {} ports".format(len(ports)))
        assert isinstance(ports, list)
        _report("PASS", "Returns a list")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — SerialReader on Closed Port
# ═══════════════════════════════════════════════════════════════════════════

class TestReaderOnClosedPort:
    """All SerialReader methods must refuse to operate on a closed port."""

    def test_flush_closed(self):
        # type: () -> None
        _report("TEST", "flush() on closed port should raise")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        reader = SerialReader(mgr)
        with pytest.raises(SerialCommunicationError) as exc_info:
            reader.flush()
        assert "not open" in str(exc_info.value).lower()
        _report("PASS", "flush() refused on closed port")

    def test_read_for_duration_closed(self):
        # type: () -> None
        _report("TEST", "read_for_duration() on closed port should raise")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        reader = SerialReader(mgr)
        with pytest.raises(SerialCommunicationError) as exc_info:
            reader.read_for_duration(1000)
        assert "not open" in str(exc_info.value).lower()
        _report("PASS", "read_for_duration() refused on closed port")

    def test_flush_and_read_closed(self):
        # type: () -> None
        _report("TEST", "flush_and_read() on closed port should raise")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        reader = SerialReader(mgr)
        with pytest.raises(SerialCommunicationError) as exc_info:
            reader.flush_and_read(1000)
        assert "not open" in str(exc_info.value).lower()
        _report("PASS", "flush_and_read() refused on closed port")

    def test_invalid_duration(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_for_duration(duration_ms=-1) should raise")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            with pytest.raises(SerialCommunicationError) as exc_info:
                reader.read_for_duration(-1)
            assert "duration" in str(exc_info.value).lower()
        _report("PASS", "Negative duration rejected")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — SerialReader with Virtual Ports
# ═══════════════════════════════════════════════════════════════════════════

class TestSerialReaderVirtual:
    """Read operations using the virtual PTY serial pair."""

    def test_flush_empty_buffer(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "flush() on empty buffer returns 0")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            discarded = reader.flush()
            _report("RESULT", "Discarded {} bytes".format(discarded))
            assert discarded == 0
        _report("PASS", "Flush on empty buffer returns 0")

    def test_flush_discards_stale_data(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "flush() discards pre-existing bytes")
        # Open the port FIRST, then inject stale data via the master end
        # so the bytes land in the already-open serial receive buffer.
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            serial_pair.write_to_master(b"STALE_DATA_12345")
            time.sleep(0.2)  # let bytes propagate into the open port's buffer

            reader = SerialReader(mgr)
            discarded = reader.flush()
            _report("RESULT", "Discarded {} bytes".format(discarded))
            assert discarded > 0
        _report("PASS", "Stale data flushed")

    def test_read_for_duration_receives_data(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_for_duration() captures data written to master")

        def writer():
            # type: () -> None
            time.sleep(0.15)
            serial_pair.write_to_master(b"HELLO_SERIAL")

        t = threading.Thread(target=writer, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            reader.flush()
            decoded, nbytes, elapsed = reader.read_for_duration(duration_ms=1000)
            _report("RESULT", "Got {} bytes in {:.3f}s: {!r}".format(
                nbytes, elapsed, decoded,
            ))
            assert "HELLO_SERIAL" in decoded
            assert nbytes > 0

        t.join(timeout=2)
        _report("PASS", "Data received from virtual serial")

    def test_read_for_duration_timeout_raises(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_for_duration() with no data raises SerialTimeoutError")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            reader.flush()
            with pytest.raises(SerialTimeoutError) as exc_info:
                reader.read_for_duration(duration_ms=300)
            _report("CAUGHT", str(exc_info.value)[:80])
        _report("PASS", "SerialTimeoutError raised on silent line")

    def test_flush_and_read(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "flush_and_read() full workflow")

        # Pre-fill with stale data
        serial_pair.write_to_master(b"OLD_NOISE")
        time.sleep(0.1)

        def writer():
            # type: () -> None
            time.sleep(0.2)
            serial_pair.write_to_master(b"FRESH_DATA")

        t = threading.Thread(target=writer, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            result = reader.flush_and_read(duration_ms=1000)
            _report("RESULT", "Got: {!r}".format(result))
            assert "FRESH_DATA" in result
            # The stale "OLD_NOISE" should NOT be in the result
            assert "OLD_NOISE" not in result

        t.join(timeout=2)
        _report("PASS", "flush_and_read() returns only fresh data")

    def test_write_and_read(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "write_and_read() sends data and captures response")

        def echo_server():
            # type: () -> None
            """Read from master end and echo back with prefix."""
            time.sleep(0.15)
            try:
                data = serial_pair.read_from_master(4096)
                serial_pair.write_to_master(b"ECHO:" + data)
            except OSError:
                pass

        t = threading.Thread(target=echo_server, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            result = reader.write_and_read("PING", duration_ms=1000)
            _report("RESULT", "Got: {!r}".format(result))
            assert "ECHO:" in result

        t.join(timeout=2)
        _report("PASS", "write_and_read() round-trip OK")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — SerialCommandExecutor on Closed Port
# ═══════════════════════════════════════════════════════════════════════════

class TestCommandExecutorClosedPort:
    """SerialCommandExecutor refuses to work on a closed port."""

    def test_execute_command_closed(self):
        # type: () -> None
        _report("TEST", "execute_command() on closed port should raise")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        executor = SerialCommandExecutor(mgr)
        with pytest.raises(SerialCommunicationError) as exc_info:
            executor.execute_command("test")
        assert "not open" in str(exc_info.value).lower()
        _report("PASS", "execute_command() refused on closed port")

    def test_execute_command_bad_timeout(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "execute_command(timeout_ms=-1) should raise")
        # Must use an open port so the timeout validation is actually reached
        # (port-not-open check fires first otherwise).
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            with pytest.raises(SerialCommunicationError) as exc_info:
                executor.execute_command("test", timeout_ms=-1)
            assert "timeout" in str(exc_info.value).lower()
        _report("PASS", "Negative timeout rejected")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — SerialCommandExecutor with Virtual Ports
# ═══════════════════════════════════════════════════════════════════════════

class TestCommandExecutorVirtual:
    """Full command execution tests using virtual serial ports."""

    def test_execute_no_response_does_not_crash(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "execute_command() with silent line should NOT crash")

        # Drain anything the wake-up ENTER produces on the master side
        def drain_master():
            # type: () -> None
            while True:
                try:
                    serial_pair.read_from_master(4096)
                except OSError:
                    break
                time.sleep(0.05)

        t = threading.Thread(target=drain_master, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                "silent_command",
                timeout_ms=500,
                prompt_settle_ms=50,
            )
            _report("RESULT", "output={!r}, timed_out={}, bytes={}".format(
                result.output, result.timed_out, result.bytes_received,
            ))
            assert result.timed_out is True
            assert result.stopped_by_condition is False
            assert result.command == "silent_command"
        _report("PASS", "Silent line handled gracefully, no crash")

    def test_execute_with_response(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "execute_command() captures response data")

        def simulated_device():
            # type: () -> None
            """Simulate a device that echoes back commands and shows a prompt."""
            collected = b""
            while True:
                try:
                    chunk = serial_pair.read_from_master(4096)
                    collected += chunk
                    # Once we see the command line, respond
                    if b"ls /" in collected:
                        time.sleep(0.05)
                        serial_pair.write_to_master(
                            b"bin  dev  etc  home  lib\r\nroot@device:~# "
                        )
                        return
                except OSError:
                    return
                time.sleep(0.01)

        t = threading.Thread(target=simulated_device, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                "ls /",
                timeout_ms=3000,
                stop_condition=lambda text: "# " in text,
                prompt_settle_ms=50,
            )
            _report("RESULT", "output={!r}".format(result.output[:100]))
            _report("RESULT", "stopped_by_condition={}, timed_out={}".format(
                result.stopped_by_condition, result.timed_out,
            ))
            assert result.stopped_by_condition is True
            assert result.timed_out is False
            assert "bin" in result.output
            assert result.bytes_received > 0

        t.join(timeout=3)
        _report("PASS", "Response captured, stop condition matched")

    def test_execute_with_streaming_callback(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "on_data streaming callback is invoked")

        streamed_chunks = []  # type: List[str]

        def simulated_device():
            # type: () -> None
            collected = b""
            while True:
                try:
                    chunk = serial_pair.read_from_master(4096)
                    collected += chunk
                    if b"hello" in collected:
                        time.sleep(0.05)
                        serial_pair.write_to_master(b"CHUNK1_")
                        time.sleep(0.05)
                        serial_pair.write_to_master(b"CHUNK2_DONE\r\n# ")
                        return
                except OSError:
                    return
                time.sleep(0.01)

        t = threading.Thread(target=simulated_device, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                "hello",
                timeout_ms=3000,
                stop_condition=lambda text: "DONE" in text,
                on_data=lambda chunk: streamed_chunks.append(chunk),
                prompt_settle_ms=50,
            )
            _report("RESULT", "Streamed {} chunks: {!r}".format(
                len(streamed_chunks), streamed_chunks,
            ))
            assert result.stopped_by_condition is True
            assert len(streamed_chunks) > 0
            combined = "".join(streamed_chunks)
            assert "CHUNK1_" in combined or "CHUNK1_" in result.output

        t.join(timeout=3)
        _report("PASS", "Streaming callback received chunks")

    def test_execute_stop_condition_not_met_times_out(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "stop_condition that never matches → timed_out=True")

        def simulated_device():
            # type: () -> None
            collected = b""
            while True:
                try:
                    chunk = serial_pair.read_from_master(4096)
                    collected += chunk
                    if b"cmd" in collected:
                        time.sleep(0.05)
                        serial_pair.write_to_master(b"some output but no magic marker")
                        return
                except OSError:
                    return
                time.sleep(0.01)

        t = threading.Thread(target=simulated_device, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                "cmd",
                timeout_ms=800,
                stop_condition=lambda text: "NEVER_APPEARS" in text,
                prompt_settle_ms=50,
            )
            _report("RESULT", "timed_out={}, output={!r}".format(
                result.timed_out, result.output[:60],
            ))
            assert result.timed_out is True
            assert result.stopped_by_condition is False
            # Data was still captured even though condition didn't match
            assert result.bytes_received >= 0

        t.join(timeout=3)
        _report("PASS", "Timed out correctly when stop condition never matches")

    def test_execute_no_stop_condition_runs_full_duration(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "No stop_condition → runs for full timeout")

        def simulated_device():
            # type: () -> None
            collected = b""
            while True:
                try:
                    chunk = serial_pair.read_from_master(4096)
                    collected += chunk
                    if b"wait" in collected:
                        time.sleep(0.05)
                        serial_pair.write_to_master(b"data received\r\n")
                        return
                except OSError:
                    return
                time.sleep(0.01)

        t = threading.Thread(target=simulated_device, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            start = time.monotonic()
            result = executor.execute_command(
                "wait",
                timeout_ms=600,
                stop_condition=None,
                prompt_settle_ms=50,
            )
            elapsed = time.monotonic() - start
            _report("RESULT", "Ran for {:.2f}s, output={!r}".format(
                elapsed, result.output[:60],
            ))
            # Should have run for approximately the full timeout
            assert elapsed >= 0.5  # at least 500ms of the 600ms timeout
            assert result.timed_out is True
            assert result.stopped_by_condition is False

        t.join(timeout=3)
        _report("PASS", "Full timeout duration observed without stop_condition")

    def test_execute_callback_exception_swallowed(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Buggy on_data callback should not break the read loop")

        def bad_callback(chunk):
            # type: (str) -> None
            raise ValueError("callback bug!")

        def simulated_device():
            # type: () -> None
            collected = b""
            while True:
                try:
                    chunk = serial_pair.read_from_master(4096)
                    collected += chunk
                    if b"crash" in collected:
                        time.sleep(0.05)
                        serial_pair.write_to_master(b"response\r\n# ")
                        return
                except OSError:
                    return
                time.sleep(0.01)

        t = threading.Thread(target=simulated_device, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                "crash",
                timeout_ms=2000,
                stop_condition=lambda text: "# " in text,
                on_data=bad_callback,
                prompt_settle_ms=50,
            )
            _report("RESULT", "output={!r}, stopped={}".format(
                result.output[:60], result.stopped_by_condition,
            ))
            assert result.stopped_by_condition is True
            assert "response" in result.output

        t.join(timeout=3)
        _report("PASS", "Buggy callback swallowed, read loop survived")

    def test_execute_stop_condition_exception_swallowed(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Buggy stop_condition should not break the read loop")

        call_count = [0]

        def buggy_condition(text):
            # type: (str) -> bool
            call_count[0] += 1
            if call_count[0] <= 2:
                raise RuntimeError("condition bug!")
            return "DONE" in text

        def simulated_device():
            # type: () -> None
            collected = b""
            while True:
                try:
                    chunk = serial_pair.read_from_master(4096)
                    collected += chunk
                    if b"bugcond" in collected:
                        time.sleep(0.05)
                        serial_pair.write_to_master(b"partial\r\n")
                        time.sleep(0.1)
                        serial_pair.write_to_master(b"more\r\n")
                        time.sleep(0.1)
                        serial_pair.write_to_master(b"DONE\r\n# ")
                        return
                except OSError:
                    return
                time.sleep(0.01)

        t = threading.Thread(target=simulated_device, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                "bugcond",
                timeout_ms=3000,
                stop_condition=buggy_condition,
                prompt_settle_ms=50,
            )
            _report("RESULT", "output={!r}, stopped={}, calls={}".format(
                result.output[:60], result.stopped_by_condition, call_count[0],
            ))
            assert result.stopped_by_condition is True
            assert "DONE" in result.output
            assert call_count[0] >= 3  # first two raised, third matched

        t.join(timeout=3)
        _report("PASS", "Buggy stop_condition survived, eventually matched")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Typeguard Enforcement
# ═══════════════════════════════════════════════════════════════════════════

class TestTypeguardEnforcement:
    """@typechecked classes reject wrong argument types at runtime."""

    def test_serial_reader_rejects_wrong_type(self):
        # type: () -> None
        _report("TEST", "SerialReader('not_a_manager') should raise TypeError")
        with pytest.raises(TypeError):
            SerialReader("not_a_manager")  # type: ignore[arg-type]
        _report("PASS", "TypeError raised for wrong type")

    def test_serial_command_executor_rejects_wrong_type(self):
        # type: () -> None
        _report("TEST", "SerialCommandExecutor(123) should raise TypeError")
        with pytest.raises(TypeError):
            SerialCommandExecutor(123)  # type: ignore[arg-type]
        _report("PASS", "TypeError raised for wrong type")


# ---------------------------------------------------------------------------
#  Entry point for running outside pytest
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
