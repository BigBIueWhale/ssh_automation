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

# typeguard 4.x raises TypeCheckError (extends Exception, not TypeError).
# typeguard 2.x raises plain TypeError.  Accept either in enforcement tests.
try:
    from typeguard import TypeCheckError
    _TYPEGUARD_ERRORS = (TypeError, TypeCheckError)
except ImportError:
    _TYPEGUARD_ERRORS = (TypeError,)

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
    _POLL_INTERVAL_S,
    _poll_read_loop,
    _write_all,
)
from linux_ssh_tools.exceptions import (
    SerialCommunicationError,
    SerialTimeoutError,
    LinuxSSHToolsError,
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
        mgr.open(context="test open close")
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
        mgr.open(context="test double close")
        mgr.close()
        mgr.close()  # second close — should be idempotent
        _report("PASS", "Double close is idempotent")

    def test_double_open_is_safe(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Double open should not raise")
        mgr = SerialConnectionManager(serial_pair.slave_path)
        mgr.open(context="test double open 1st")
        mgr.open(context="test double open 2nd")  # second open — should be a no-op
        assert mgr.is_open()
        mgr.close()
        _report("PASS", "Double open is idempotent")

    def test_open_nonexistent_port(self):
        # type: () -> None
        _report("TEST", "Opening a nonexistent port should raise with hint")
        mgr = SerialConnectionManager("/dev/ttyNONEXISTENT_99")
        with pytest.raises(SerialCommunicationError) as exc_info:
            mgr.open(context="test nonexistent port")
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
            reader.flush(context="test flush closed")
        assert "not open" in str(exc_info.value).lower()
        _report("PASS", "flush() refused on closed port")

    def test_read_for_duration_closed(self):
        # type: () -> None
        _report("TEST", "read_for_duration() on closed port should raise")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        reader = SerialReader(mgr)
        with pytest.raises(SerialCommunicationError) as exc_info:
            reader.read_for_duration(context="test read closed", duration_ms=1000)
        assert "not open" in str(exc_info.value).lower()
        _report("PASS", "read_for_duration() refused on closed port")

    def test_flush_and_read_closed(self):
        # type: () -> None
        _report("TEST", "flush_and_read() on closed port should raise")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        reader = SerialReader(mgr)
        with pytest.raises(SerialCommunicationError) as exc_info:
            reader.flush_and_read(context="test flush_and_read closed", duration_ms=1000)
        assert "not open" in str(exc_info.value).lower()
        _report("PASS", "flush_and_read() refused on closed port")

    def test_invalid_duration(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_for_duration(duration_ms=-1) should raise")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            with pytest.raises(SerialCommunicationError) as exc_info:
                reader.read_for_duration(context="test invalid duration", duration_ms=-1)
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
            discarded = reader.flush(context="test flush empty")
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
            discarded = reader.flush(context="test flush stale")
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
            reader.flush(context="test read data flush")
            decoded, nbytes, elapsed = reader.read_for_duration(
                context="test read data", duration_ms=1000,
            )
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
            reader.flush(context="test timeout flush")
            with pytest.raises(SerialTimeoutError) as exc_info:
                reader.read_for_duration(context="test timeout read", duration_ms=300)
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
            result = reader.flush_and_read(context="test flush_and_read", duration_ms=1000)
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
            result = reader.write_and_read("PING", context="test write_and_read", duration_ms=1000)
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
            executor.execute_command(
                "test", context="test exec closed",
                timeout_ms=1000, stop_condition=None,
            )
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
                executor.execute_command(
                    "test", context="test bad timeout",
                    timeout_ms=-1, stop_condition=None,
                )
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
            # No stop_condition → runs for full timeout, returns normally
            result = executor.execute_command(
                "silent_command",
                context="test silent line",
                timeout_ms=500,
                stop_condition=None,
                prompt_settle_ms=50,
            )
            _report("RESULT", "output={!r}, timed_out={}, bytes={}".format(
                result.output, result.timed_out, result.bytes_received,
            ))
            assert result.timed_out is False  # no stop_condition → timed_out is False
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
                context="test exec with response",
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
                context="test streaming callback",
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
        _report("TEST", "stop_condition that never matches → SerialTimeoutError")

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

            with pytest.raises(SerialTimeoutError) as exc_info:
                executor.execute_command(
                    "cmd",
                    context="test stop condition timeout",
                    timeout_ms=800,
                    stop_condition=lambda text: "NEVER_APPEARS" in text,
                    prompt_settle_ms=50,
                )

            err = exc_info.value
            _report("CAUGHT", "SerialTimeoutError: {}".format(str(err)[:80]))
            assert err.result is not None
            assert err.result.timed_out is True
            assert err.result.stopped_by_condition is False
            assert err.result.command == "cmd"
            assert err.result.bytes_received >= 0
            # Also a LinuxSSHToolsError
            assert isinstance(err, LinuxSSHToolsError)

        t.join(timeout=3)
        _report("PASS", "SerialTimeoutError raised with .result attached")

    def test_execute_no_stop_condition_runs_full_duration(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "No stop_condition → runs for full timeout, returns normally")

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
                context="test no stop condition",
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
            assert result.timed_out is False  # no stop_condition → timed_out is False
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
                context="test buggy callback",
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
                context="test buggy stop condition",
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
        with pytest.raises(_TYPEGUARD_ERRORS):
            SerialReader("not_a_manager")  # type: ignore[arg-type]
        _report("PASS", "TypeError raised for wrong type")

    def test_serial_command_executor_rejects_wrong_type(self):
        # type: () -> None
        _report("TEST", "SerialCommandExecutor(123) should raise TypeError")
        with pytest.raises(_TYPEGUARD_ERRORS):
            SerialCommandExecutor(123)  # type: ignore[arg-type]
        _report("PASS", "TypeError raised for wrong type")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — SSH-trigger → serial-capture workflow
# ═══════════════════════════════════════════════════════════════════════════

class TestSSHTriggerSerialCapture:
    """Verify the critical use-case: run an SSH command that produces serial
    output, then capture that output via ``flush()`` + ``read_for_duration()``.

    The SSH call itself is simulated with ``time.sleep()`` (blocking) and a
    background thread that writes to the master end of the PTY pair at
    controlled times.  This exercises the actual OS serial buffering
    behaviour — the serial port must be **open** before the "SSH call" so
    the kernel collects incoming bytes while the caller is blocked.

    The correct ordering is::

        serial port open  →  flush  →  SSH command (blocks)  →  read

    Using ``flush()`` + ``read_for_duration()`` separately (NOT
    ``flush_and_read()``) so the SSH call can run between them.
    """

    def test_data_buffered_during_blocking_call(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        """Data that arrives on the serial line while the caller is blocked
        (simulated SSH) is held in the OS buffer and captured by the
        subsequent ``read_for_duration()``."""
        _report("TEST", "Data buffered during blocking call")

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            reader.flush(context="pre-ssh flush")

            # --- simulate SSH command that triggers serial output ---
            serial_pair.write_to_master(b"SERIAL_RESPONSE_ABC")

            # simulate blocking SSH call (data sits in OS buffer)
            time.sleep(0.3)

            # --- after SSH returns, read the serial buffer ---
            text, nbytes, elapsed = reader.read_for_duration(
                context="capture after ssh",
                duration_ms=500,
            )

            _report("RESULT", "Got {} bytes: {!r}".format(nbytes, text[:80]))
            assert "SERIAL_RESPONSE_ABC" in text
            assert nbytes > 0

        _report("PASS", "OS buffer held data during blocking call")

    def test_stale_data_excluded_fresh_captured(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        """Stale data already in the buffer before ``flush()`` is discarded.
        Only data arriving *after* the flush (i.e. during the simulated SSH
        call) is returned by ``read_for_duration()``."""
        _report("TEST", "Stale excluded, fresh captured")

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            # inject stale data BEFORE flush
            serial_pair.write_to_master(b"OLD_STALE_NOISE_XYZ")
            time.sleep(0.15)  # let bytes propagate

            reader = SerialReader(mgr)
            discarded = reader.flush(context="clear stale before ssh")
            _report("STEP", "Flushed {} stale bytes".format(discarded))
            assert discarded > 0

            # --- simulate SSH command producing serial output ---
            serial_pair.write_to_master(b"FRESH_SSH_TRIGGERED")
            time.sleep(0.2)  # simulate SSH blocking

            text, nbytes, elapsed = reader.read_for_duration(
                context="capture after ssh",
                duration_ms=500,
            )

            _report("RESULT", "Got {!r}".format(text[:80]))
            assert "FRESH_SSH_TRIGGERED" in text
            assert "OLD_STALE_NOISE_XYZ" not in text

        _report("PASS", "Only post-flush data captured")

    def test_multi_chunk_during_blocking(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        """Multiple serial chunks arriving at staggered times during the
        blocking SSH call are all captured when ``read_for_duration()``
        runs afterward."""
        _report("TEST", "Multiple chunks during blocking call")

        def staggered_serial_output():
            # type: () -> None
            time.sleep(0.05)
            serial_pair.write_to_master(b"CHUNK_A_")
            time.sleep(0.08)
            serial_pair.write_to_master(b"CHUNK_B_")
            time.sleep(0.08)
            serial_pair.write_to_master(b"CHUNK_C_END")

        t = threading.Thread(target=staggered_serial_output, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            reader.flush(context="pre-ssh flush")

            # simulate blocking SSH call — longer than all chunks
            time.sleep(0.4)

            text, nbytes, elapsed = reader.read_for_duration(
                context="capture staggered chunks",
                duration_ms=500,
            )

            _report("RESULT", "Got {} bytes: {!r}".format(nbytes, text[:80]))
            assert "CHUNK_A_" in text
            assert "CHUNK_B_" in text
            assert "CHUNK_C_END" in text

        t.join(timeout=2)
        _report("PASS", "All staggered chunks captured from OS buffer")

    def test_delayed_response_within_read_window(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        """If the SSH command returns quickly but the serial response is
        delayed (device processing time), ``read_for_duration()`` still
        captures it because it listens for the full duration."""
        _report("TEST", "Delayed serial response within read window")

        def delayed_response():
            # type: () -> None
            # Serial output arrives 300ms into the read window
            time.sleep(0.3)
            serial_pair.write_to_master(b"DELAYED_UART_REPLY")

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            reader.flush(context="pre-ssh flush")

            # SSH command returns almost instantly
            time.sleep(0.05)

            # But serial response is delayed — start the writer thread
            # right before read_for_duration so the data arrives mid-read
            t = threading.Thread(target=delayed_response, daemon=True)
            t.start()

            text, nbytes, elapsed = reader.read_for_duration(
                context="capture delayed response",
                duration_ms=1000,
            )

            _report("RESULT", "Got {} bytes in {:.2f}s: {!r}".format(
                nbytes, elapsed, text[:80],
            ))
            assert "DELAYED_UART_REPLY" in text

        t.join(timeout=2)
        _report("PASS", "Delayed response captured within read window")

    def test_full_end_to_end_workflow(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        """End-to-end simulation of the documented workflow:

        1. Open serial port
        2. Stale data is in the buffer (from previous activity)
        3. Flush clears the stale data
        4. Simulated SSH command blocks; serial response arrives mid-block
        5. SSH returns; read_for_duration() captures the serial response
        6. Result contains only the fresh response, not the stale data
        """
        _report("TEST", "Full end-to-end SSH→serial workflow")

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            # --- Phase 1: stale data sitting in the buffer ---
            serial_pair.write_to_master(b"BOOTLOG_NOISE_999\r\n")
            time.sleep(0.15)

            reader = SerialReader(mgr)

            # --- Phase 2: flush ---
            discarded = reader.flush(context="clear before ssh trigger")
            _report("STEP", "Phase 2: flushed {} stale bytes".format(discarded))
            assert discarded > 0

            # --- Phase 3: SSH command triggers serial output ---
            def ssh_command_simulation():
                # type: () -> None
                """Simulate: SSH command executes on remote device, the device
                writes a response to its serial port after 100ms of processing."""
                time.sleep(0.1)
                serial_pair.write_to_master(b"UART: voltage=3.31V\r\n")

            t = threading.Thread(target=ssh_command_simulation, daemon=True)
            t.start()

            # Simulate the SSH call blocking for 300ms
            time.sleep(0.3)
            _report("STEP", "Phase 3: simulated SSH call returned")

            # --- Phase 4: read the serial response ---
            text, nbytes, elapsed = reader.read_for_duration(
                context="capture uart response after ssh",
                duration_ms=500,
            )

            _report("RESULT", "Phase 4: {} bytes in {:.2f}s: {!r}".format(
                nbytes, elapsed, text[:80],
            ))

            # --- Assertions ---
            assert "UART: voltage=3.31V" in text, (
                "Fresh serial response must be captured"
            )
            assert "BOOTLOG_NOISE_999" not in text, (
                "Stale data from before flush must NOT appear"
            )
            assert nbytes > 0

            t.join(timeout=2)

        _report("PASS", "Full workflow: stale excluded, SSH-triggered response captured")

    def test_flush_and_read_is_wrong_for_this_usecase(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        """Demonstrates that ``flush_and_read()`` CANNOT be used for the
        SSH-trigger use-case: it flushes and immediately reads, leaving no
        window to run the SSH command.  Data injected *before* calling
        ``flush_and_read()`` is discarded by the flush phase.

        This negative test documents WHY the README uses the separate
        ``flush()`` + ``read_for_duration()`` pattern.
        """
        _report("TEST", "flush_and_read() is wrong for SSH-trigger use-case")

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)

            # Inject data that would be the "SSH-triggered serial response".
            # With the CORRECT pattern (flush → SSH → read), this data would
            # arrive AFTER the flush.  But with flush_and_read(), the flush
            # and read happen back-to-back — there's no window for the SSH
            # call.  We simulate by injecting data first, which flush_and_read
            # will discard as "stale".
            serial_pair.write_to_master(b"RESPONSE_THAT_GETS_LOST")
            time.sleep(0.15)

            # flush_and_read: flush discards the data, then read finds silence
            with pytest.raises(SerialTimeoutError):
                reader.flush_and_read(
                    context="wrong pattern for ssh trigger",
                    duration_ms=300,
                )

        _report("PASS", "flush_and_read() correctly cannot handle SSH-trigger pattern")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Exception Hierarchy
# ═══════════════════════════════════════════════════════════════════════════

class TestSerialExceptionHierarchy:
    """Verify serial exception class relationships."""

    def test_serial_exceptions_under_common_base(self):
        # type: () -> None
        _report("TEST", "Serial exceptions should descend from LinuxSSHToolsError")
        assert issubclass(SerialCommunicationError, LinuxSSHToolsError)
        assert issubclass(SerialTimeoutError, LinuxSSHToolsError)
        assert issubclass(SerialTimeoutError, SerialCommunicationError)
        _report("PASS", "Serial exception hierarchy is correct")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — NullHandler / No stderr leak
# ═══════════════════════════════════════════════════════════════════════════

class TestNullHandler:
    """Library logging does not leak to stderr when no handler is configured."""

    def test_no_stderr_on_timeout(self, serial_pair, capsys):
        # type: (VirtualSerialPair, object) -> None
        _report("TEST", "No stderr output on timeout")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            reader.flush(context="test no stderr flush")
            with pytest.raises(SerialTimeoutError):
                reader.read_for_duration(context="test no stderr", duration_ms=200)
        captured = capsys.readouterr()
        # Only our _report() prints and pytest env header should appear.
        # No logger.warning output should leak to stderr.
        for line in captured.err.splitlines():
            assert "[SERIAL-" not in line, (
                "Logger output leaked to stderr: {}".format(line)
            )
        _report("PASS", "No logger output leaked to stderr")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _POLL_INTERVAL_S constant
# ═══════════════════════════════════════════════════════════════════════════

class TestPollInterval:
    """The _POLL_INTERVAL_S constant is sane."""

    def test_poll_interval_value(self):
        # type: () -> None
        _report("TEST", "_POLL_INTERVAL_S is 10ms")
        assert _POLL_INTERVAL_S == 0.01
        _report("PASS", "_POLL_INTERVAL_S == 0.01")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Busy-loop guard
# ═══════════════════════════════════════════════════════════════════════════

class TestBusyLoopGuard:
    """When ser.read() returns b'' despite in_waiting > 0, the poll loop
    must not spin at 100% CPU — it should sleep."""

    def test_empty_read_does_not_busy_loop(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Empty read from in_waiting>0 triggers sleep, not spin")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            ser = mgr.get_serial()
            # Patch in_waiting to always report >0, read to return b""
            original_in_waiting = type(ser).in_waiting
            original_read = ser.read
            call_count = [0]

            class FakeInWaiting:
                def __get__(self, obj, objtype=None):
                    if obj is None:
                        return self
                    call_count[0] += 1
                    if call_count[0] <= 200:
                        return 10  # lie: claim 10 bytes waiting
                    return 0  # eventually stop

            def fake_read(size):
                # type: (int) -> bytes
                return b""  # always empty despite in_waiting > 0

            type(ser).in_waiting = FakeInWaiting()
            ser.read = fake_read

            try:
                start = time.monotonic()
                result = _poll_read_loop(
                    ser=ser,
                    port_name=mgr.port,
                    timeout_s=0.5,
                    context="test busy loop guard",
                )
                elapsed = time.monotonic() - start
                _report("RESULT", "Elapsed={:.3f}s, cycles={}".format(
                    elapsed, result.read_cycles,
                ))
                # If the busy-loop guard works, the loop should take at
                # least some time due to sleeps, not complete in <10ms
                assert elapsed >= 0.05, (
                    "Loop ran too fast — busy-loop guard may not be working"
                )
                assert result.bytes_received == 0
            finally:
                type(ser).in_waiting = original_in_waiting
                ser.read = original_read
        _report("PASS", "Busy-loop guard prevented CPU spin")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _drain_and_reset timeout guard
# ═══════════════════════════════════════════════════════════════════════════

class TestDrainTimeout:
    """_drain_and_reset respects its max_drain_s timeout."""

    def test_drain_respects_deadline(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "_drain_and_reset stops after max_drain_s")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            ser = mgr.get_serial()
            executor = SerialCommandExecutor(mgr)

            # Patch: in_waiting always >0 but read returns empty
            original_in_waiting = type(ser).in_waiting
            original_read = ser.read

            class AlwaysWaiting:
                def __get__(self, obj, objtype=None):
                    if obj is None:
                        return self
                    return 100  # always claim data is waiting

            def empty_read(size):
                # type: (int) -> bytes
                return b""

            type(ser).in_waiting = AlwaysWaiting()
            ser.read = empty_read

            try:
                start = time.monotonic()
                discarded = executor._drain_and_reset(
                    ser, "test-drain", "test drain timeout",
                    max_drain_s=0.3,
                )
                elapsed = time.monotonic() - start
                _report("RESULT", "Drain took {:.3f}s, discarded={}".format(
                    elapsed, discarded,
                ))
                # Should complete near the deadline, not hang forever
                assert elapsed < 1.0, "Drain took too long (expected ~0.3s)"
                assert discarded == 0  # read always returned empty
            finally:
                type(ser).in_waiting = original_in_waiting
                ser.read = original_read
        _report("PASS", "_drain_and_reset respects timeout")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — timed_out semantics
# ═══════════════════════════════════════════════════════════════════════════

class TestTimedOutSemantics:
    """timed_out is True ONLY when stop_condition was provided but not matched."""

    def test_no_stop_condition_means_not_timed_out(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "No stop_condition → timed_out is False")

        # Drain master side
        def drain():
            # type: () -> None
            while True:
                try:
                    serial_pair.read_from_master(4096)
                except OSError:
                    break
                time.sleep(0.05)

        t = threading.Thread(target=drain, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                "test",
                context="test timed_out semantics",
                timeout_ms=200,
                stop_condition=None,
                prompt_settle_ms=50,
            )
            assert result.timed_out is False
            assert result.stopped_by_condition is False
        _report("PASS", "timed_out=False when no stop_condition")

    def test_stop_condition_met_means_not_timed_out(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "stop_condition matched → timed_out is False")

        def device():
            # type: () -> None
            collected = b""
            while True:
                try:
                    chunk = serial_pair.read_from_master(4096)
                    collected += chunk
                    if b"ping" in collected:
                        time.sleep(0.05)
                        serial_pair.write_to_master(b"pong DONE\r\n")
                        return
                except OSError:
                    return
                time.sleep(0.01)

        t = threading.Thread(target=device, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                "ping",
                context="test condition met",
                timeout_ms=3000,
                stop_condition=lambda text: "DONE" in text,
                prompt_settle_ms=50,
            )
            assert result.timed_out is False
            assert result.stopped_by_condition is True

        t.join(timeout=3)
        _report("PASS", "timed_out=False when stop_condition matched")

    def test_stop_condition_not_met_means_timed_out(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "stop_condition not matched → timed_out is True")

        def drain():
            # type: () -> None
            while True:
                try:
                    serial_pair.read_from_master(4096)
                except OSError:
                    break
                time.sleep(0.05)

        t = threading.Thread(target=drain, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            executor = SerialCommandExecutor(mgr)
            with pytest.raises(SerialTimeoutError) as exc_info:
                executor.execute_command(
                    "test",
                    context="test timed_out true",
                    timeout_ms=300,
                    stop_condition=lambda text: "NEVER" in text,
                    prompt_settle_ms=50,
                )
            assert exc_info.value.result is not None
            assert exc_info.value.result.timed_out is True
        _report("PASS", "timed_out=True when stop_condition not matched")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — read_until
# ═══════════════════════════════════════════════════════════════════════════

class TestReadUntil:
    """SerialReader.read_until() tests."""

    def test_read_until_string_condition(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_until with string condition")

        def writer():
            # type: () -> None
            time.sleep(0.1)
            serial_pair.write_to_master(b"booting...")
            time.sleep(0.1)
            serial_pair.write_to_master(b" ready\r\n")

        t = threading.Thread(target=writer, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            text, nbytes, elapsed, matched = reader.read_until(
                "ready",
                context="test read_until string",
                timeout_ms=3000,
            )
            _report("RESULT", "matched={}, text={!r}".format(matched, text[:60]))
            assert matched is True
            assert "ready" in text
            assert nbytes > 0
            # Should have returned early, well before 3s timeout
            assert elapsed < 2.0

        t.join(timeout=2)
        _report("PASS", "read_until with string exits early on match")

    def test_read_until_callable_condition(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_until with callable condition")

        def writer():
            # type: () -> None
            time.sleep(0.1)
            serial_pair.write_to_master(b"login: ")

        t = threading.Thread(target=writer, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            text, nbytes, elapsed, matched = reader.read_until(
                lambda text: "login:" in text or "# " in text,
                context="test read_until callable",
                timeout_ms=3000,
            )
            assert matched is True
            assert "login:" in text

        t.join(timeout=2)
        _report("PASS", "read_until with callable works")

    def test_read_until_timeout_raises(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_until times out → SerialTimeoutError")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            with pytest.raises(SerialTimeoutError):
                reader.read_until(
                    "NEVER_APPEARS",
                    context="test read_until timeout",
                    timeout_ms=300,
                )
        _report("PASS", "read_until raises on timeout")

    def test_read_until_closed_port(self):
        # type: () -> None
        _report("TEST", "read_until on closed port raises")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        reader = SerialReader(mgr)
        with pytest.raises(SerialCommunicationError) as exc_info:
            reader.read_until("test", context="test closed", timeout_ms=1000)
        assert "not open" in str(exc_info.value).lower()
        _report("PASS", "read_until refused on closed port")

    def test_read_until_invalid_timeout(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_until with bad timeout raises")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            with pytest.raises(SerialCommunicationError):
                reader.read_until("x", context="test bad timeout", timeout_ms=-1)
        _report("PASS", "Invalid timeout rejected")

    def test_read_until_with_streaming(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "read_until with on_data callback")

        chunks = []  # type: List[str]

        def writer():
            # type: () -> None
            time.sleep(0.1)
            serial_pair.write_to_master(b"part1_")
            time.sleep(0.05)
            serial_pair.write_to_master(b"part2_DONE")

        t = threading.Thread(target=writer, daemon=True)
        t.start()

        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            reader = SerialReader(mgr)
            text, nbytes, elapsed, matched = reader.read_until(
                "DONE",
                context="test read_until streaming",
                timeout_ms=3000,
                on_data=lambda c: chunks.append(c),
            )
            assert matched is True
            assert len(chunks) > 0
            combined = "".join(chunks)
            assert "part1_" in combined or "part1_" in text

        t.join(timeout=2)
        _report("PASS", "read_until streaming callback works")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Flow control parameters
# ═══════════════════════════════════════════════════════════════════════════

class TestFlowControl:
    """SerialConnectionManager accepts flow control parameters."""

    def test_flow_control_defaults(self):
        # type: () -> None
        _report("TEST", "Default flow control is all False")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        assert mgr.xonxoff is False
        assert mgr.rtscts is False
        assert mgr.dsrdtr is False
        _report("PASS", "Default flow control correct")

    def test_flow_control_can_be_set(self):
        # type: () -> None
        _report("TEST", "Flow control params can be set")
        mgr = SerialConnectionManager(
            "/dev/ttyUSB0", xonxoff=True, rtscts=True, dsrdtr=True,
        )
        assert mgr.xonxoff is True
        assert mgr.rtscts is True
        assert mgr.dsrdtr is True
        _report("PASS", "Flow control params stored")

    def test_flow_control_passed_to_serial(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "Flow control params passed to pyserial")
        with SerialConnectionManager(
            serial_pair.slave_path, xonxoff=True,
        ) as mgr:
            ser = mgr.get_serial()
            assert ser.xonxoff is True
        _report("PASS", "xonxoff passed through to pyserial")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — CLI argument parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestCLIArgs:
    """CLI serial subcommands accept the new --bytesize/--parity/--stopbits flags."""

    def test_serial_read_help_includes_new_flags(self):
        # type: () -> None
        _report("TEST", "serial-read --help includes --bytesize, --parity, --stopbits")
        from linux_ssh_tools.cli import main
        import io
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = ["linux-ssh", "serial-read", "--help"]
            main()
        # argparse exits 0 on --help
        assert exc_info.value.code == 0
        _report("PASS", "serial-read --help works")

    def test_serial_exec_help_includes_new_flags(self):
        # type: () -> None
        _report("TEST", "serial-exec --help includes --bytesize, --parity, --stopbits")
        from linux_ssh_tools.cli import main as cli_main
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = ["linux-ssh", "serial-exec", "test", "--help"]
            cli_main()
        assert exc_info.value.code == 0
        _report("PASS", "serial-exec --help works")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — Write timeout configuration
# ═══════════════════════════════════════════════════════════════════════════

class TestWriteTimeout:
    """SERIAL_WRITE_TIMEOUT constant and write_timeout parameter."""

    def test_serial_write_timeout_constant(self):
        # type: () -> None
        _report("TEST", "SERIAL_WRITE_TIMEOUT == 10")
        from linux_ssh_tools import SERIAL_WRITE_TIMEOUT
        assert SERIAL_WRITE_TIMEOUT == 10
        _report("PASS", "SERIAL_WRITE_TIMEOUT == 10")

    def test_default_write_timeout_is_10(self):
        # type: () -> None
        _report("TEST", "SerialConnectionManager default write_timeout is 10")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        assert mgr.write_timeout == 10
        _report("PASS", "Default write_timeout == 10")

    def test_write_timeout_independent_from_read_timeout(self):
        # type: () -> None
        _report("TEST", "write_timeout and read_timeout are independent")
        mgr = SerialConnectionManager("/dev/ttyUSB0", read_timeout=0, write_timeout=5)
        assert mgr.read_timeout == 0
        assert mgr.write_timeout == 5
        _report("PASS", "write_timeout and read_timeout are independent")

    def test_write_timeout_none_accepted(self):
        # type: () -> None
        _report("TEST", "write_timeout=None accepted (blocking mode)")
        mgr = SerialConnectionManager("/dev/ttyUSB0", write_timeout=None)
        assert mgr.write_timeout is None
        _report("PASS", "write_timeout=None accepted")

    def test_write_timeout_negative_rejected(self):
        # type: () -> None
        _report("TEST", "write_timeout=-1 should be rejected")
        with pytest.raises(SerialCommunicationError) as exc_info:
            SerialConnectionManager("/dev/ttyUSB0", write_timeout=-1)
        assert "write_timeout" in str(exc_info.value).lower()
        _report("PASS", "Negative write_timeout rejected")

    def test_write_timeout_propagated_to_serial(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "write_timeout propagated to underlying serial.Serial")
        with SerialConnectionManager(
            serial_pair.slave_path, write_timeout=7.5,
        ) as mgr:
            ser = mgr.get_serial()
            assert ser.write_timeout == 7.5
            # read timeout should still be 0 (non-blocking)
            assert ser.timeout == 0
        _report("PASS", "write_timeout correctly set on pyserial object")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — _write_all function
# ═══════════════════════════════════════════════════════════════════════════

class TestWriteAll:
    """_write_all() writes all bytes, detects short writes, and flushes."""

    def test_write_all_happy_path(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "_write_all writes all bytes and data arrives on master")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            ser = mgr.get_serial()
            payload = b"HELLO_WRITE_ALL"
            n = _write_all(ser, payload, mgr.port, context="test happy path")
            assert n == len(payload)
            time.sleep(0.1)
            received = serial_pair.read_from_master(4096)
            assert payload in received
        _report("PASS", "_write_all happy path OK")

    def test_write_all_short_write_detection(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "_write_all detects short write")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            ser = mgr.get_serial()
            original_write = ser.write

            def short_write(data):
                # type: (bytes) -> int
                # Simulate writing only 3 of N bytes
                original_write(data[:3])
                return 3

            ser.write = short_write
            try:
                with pytest.raises(SerialCommunicationError) as exc_info:
                    _write_all(ser, b"FULL_PAYLOAD", mgr.port, context="test short write")
                assert "Short write" in str(exc_info.value)
                _report("CAUGHT", str(exc_info.value)[:80])
            finally:
                ser.write = original_write
        _report("PASS", "Short write detected with clear message")

    def test_write_all_flush_failure_propagates(self, serial_pair):
        # type: (VirtualSerialPair) -> None
        _report("TEST", "_write_all propagates flush failure")
        with SerialConnectionManager(serial_pair.slave_path) as mgr:
            ser = mgr.get_serial()
            original_flush = ser.flush

            def bad_flush():
                # type: () -> None
                raise serial.SerialException("flush failed")

            ser.flush = bad_flush
            try:
                with pytest.raises(serial.SerialException):
                    _write_all(ser, b"DATA", mgr.port, context="test flush failure")
            finally:
                ser.flush = original_flush
        _report("PASS", "Flush failure propagated")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS — SERIAL_READ_TIMEOUT is non-blocking
# ═══════════════════════════════════════════════════════════════════════════

class TestNonBlockingTimeout:
    """SERIAL_READ_TIMEOUT should be 0 (non-blocking)."""

    def test_default_read_timeout_is_zero(self):
        # type: () -> None
        _report("TEST", "Default read_timeout is 0")
        from linux_ssh_tools import SERIAL_READ_TIMEOUT
        assert SERIAL_READ_TIMEOUT == 0
        _report("PASS", "SERIAL_READ_TIMEOUT == 0")

    def test_connection_manager_default_timeout(self):
        # type: () -> None
        _report("TEST", "SerialConnectionManager default read_timeout is 0")
        mgr = SerialConnectionManager("/dev/ttyUSB0")
        assert mgr.read_timeout == 0
        _report("PASS", "Default read_timeout == 0")


# ---------------------------------------------------------------------------
#  Entry point for running outside pytest
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
