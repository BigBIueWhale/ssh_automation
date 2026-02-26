"""Serial communication with flush-then-read pattern for hardware consoles.

Provides a robust interface for reading data from serial lines attached to
Linux devices (or any UART-accessible hardware).  Typical use-case: issue a
command over SSH, then capture the serial response that arrives within a
configurable time window.

Also provides ``SerialCommandExecutor`` for running commands directly over the
serial console (ENTER → command → ENTER) with blocking read, optional
streaming callback, and content-based stop conditions.

Cross-platform: works on both Windows 10 (COMx) and Ubuntu 24.04
(/dev/ttyUSB*, /dev/ttyS*, /dev/ttyACM*).

Default line settings: 115200 8N1 (no flow control).
"""

from __future__ import annotations

import codecs
import dataclasses
import logging
import platform
import time
from typing import Callable, List, Optional, Tuple, Union

import serial
import serial.tools.list_ports
from typeguard import typechecked

from . import (
    SERIAL_BAUD_RATE,
    SERIAL_BYTESIZE,
    SERIAL_PARITY,
    SERIAL_STOPBITS,
    SERIAL_READ_TIMEOUT,
    SERIAL_WRITE_TIMEOUT,
    SERIAL_DEFAULT_DURATION_MS,
    SERIAL_COMMAND_TIMEOUT_MS,
    SERIAL_PROMPT_SETTLE_MS,
    SERIAL_POLL_INTERVAL_S,
    SERIAL_RX_BUFFER_SIZE,
)
from .exceptions import SerialCommunicationError, SerialTimeoutError

logger = logging.getLogger("linux_ssh_tools.serial_comm")

_IS_WINDOWS = platform.system() == "Windows"

# Default poll loop sleep granularity (10 ms).  All poll loops use this
# instead of the serial port's read_timeout so timing is managed by our
# code, not the driver.  Kept as a module-level fallback; callers should
# prefer the per-connection ``poll_interval_s`` parameter.
_POLL_INTERVAL_S_DEFAULT = SERIAL_POLL_INTERVAL_S

# Map string parity values to pyserial constants
_PARITY_MAP = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
}

# Map integer stopbits to pyserial constants
_STOPBITS_MAP = {
    1: serial.STOPBITS_ONE,
    2: serial.STOPBITS_TWO,
}

# Map integer bytesize to pyserial constants
_BYTESIZE_MAP = {
    5: serial.FIVEBITS,
    6: serial.SIXBITS,
    7: serial.SEVENBITS,
    8: serial.EIGHTBITS,
}


# ---------------------------------------------------------------------------
# Shared poll-read loop
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _ReadLoopResult:
    """Internal result from ``_poll_read_loop``."""
    buffer: bytes
    decoded: str
    bytes_received: int
    elapsed_seconds: float
    stopped_by_condition: bool
    read_cycles: int


def _poll_read_loop(
    ser: serial.Serial,
    port_name: str,
    timeout_s: float,
    stop_condition: Optional[Callable[[str], bool]] = None,
    on_data: Optional[Callable[[str], None]] = None,
    encoding: str = "utf-8",
    context: str = "",
    poll_interval_s: float = _POLL_INTERVAL_S_DEFAULT,
) -> _ReadLoopResult:
    """Core poll loop shared by ``read_for_duration``, ``read_until``,
    and ``execute_command``.

    Accumulates bytes from *ser* for up to *timeout_s* seconds.  Optionally
    checks a *stop_condition* against the accumulated decoded text and invokes
    *on_data* with each new decoded chunk.

    Returns a ``_ReadLoopResult`` with the raw buffer, decoded text, byte
    count, elapsed time, whether the stop condition fired, and poll-cycle
    count.
    """
    buffer = bytearray()
    decoded_so_far = ""
    stopped_by_condition = False
    start_time = time.monotonic()
    deadline = start_time + timeout_s
    read_cycles = 0

    # Incremental decoder for streaming callback — handles multi-byte
    # characters that may be split across reads.
    inc_decoder = codecs.getincrementaldecoder(encoding)("replace") if on_data is not None else None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        waiting = ser.in_waiting
        if waiting > 0:
            chunk_bytes = ser.read(waiting)
            if len(chunk_bytes) == 0:
                # in_waiting lied — sleep to prevent busy-loop
                sleep_time = min(poll_interval_s, remaining)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                read_cycles += 1
                continue

            buffer.extend(chunk_bytes)
            read_cycles += 1

            # Full-buffer decode for stop-condition accuracy
            decoded_so_far = buffer.decode(encoding, errors="replace")

            logger.debug(
                "[SERIAL-POLL] +%d bytes from %s (total %d)",
                len(chunk_bytes), port_name, len(buffer),
            )

            # Stream callback — use incremental decoder for correct
            # multi-byte character handling
            if on_data is not None and inc_decoder is not None:
                try:
                    chunk_str = inc_decoder.decode(chunk_bytes, False)
                    if chunk_str:
                        on_data(chunk_str)
                except Exception as cb_exc:
                    logger.warning(
                        "[SERIAL-POLL] on_data callback raised %s: %s "
                        "(callback errors are swallowed to protect "
                        "the read loop)",
                        type(cb_exc).__name__, cb_exc,
                    )

            # Stop-condition check
            if stop_condition is not None:
                try:
                    if stop_condition(decoded_so_far):
                        stopped_by_condition = True
                        logger.info(
                            "[SERIAL-POLL] [%s] Stop condition matched on "
                            "%s after %d bytes (%.3fs)",
                            context, port_name, len(buffer),
                            time.monotonic() - start_time,
                        )
                        break
                except Exception as sc_exc:
                    logger.warning(
                        "[SERIAL-POLL] stop_condition raised %s: %s "
                        "(treated as 'not matched', read continues)",
                        type(sc_exc).__name__, sc_exc,
                    )
        else:
            sleep_time = min(poll_interval_s, remaining)
            if sleep_time > 0:
                time.sleep(sleep_time)
            read_cycles += 1

    # Flush the incremental decoder to handle any trailing partial chars
    if on_data is not None and inc_decoder is not None:
        try:
            trailing = inc_decoder.decode(b"", True)
            if trailing:
                on_data(trailing)
        except Exception:
            pass

    elapsed = time.monotonic() - start_time

    # Final decode in case no data arrived (decoded_so_far is "")
    if len(buffer) > 0 and not decoded_so_far:
        decoded_so_far = buffer.decode(encoding, errors="replace")

    return _ReadLoopResult(
        buffer=bytes(buffer),
        decoded=decoded_so_far,
        bytes_received=len(buffer),
        elapsed_seconds=elapsed,
        stopped_by_condition=stopped_by_condition,
        read_cycles=read_cycles,
    )


def _write_all(
    ser: serial.Serial,
    data: bytes,
    port_name: str,
    context: str = "",
) -> int:
    """Write *all* bytes to the serial port and flush the OS transmit buffer.

    With a blocking ``write_timeout`` (>= 1 s), pyserial loops internally
    (POSIX) or blocks on ``GetOverlappedResult`` (Windows) until every byte
    has been accepted by the driver.  The short-write check is defense-in-depth
    — it catches the ``write_timeout=0`` footgun where ``ser.write()`` may
    return fewer bytes than requested.

    Does **not** catch exceptions — lets ``serial.SerialTimeoutException``,
    ``serial.SerialException``, and ``OSError`` propagate to the caller's
    existing exception handlers.

    Args:
        ser: The open serial object.
        data: Bytes to write.
        port_name: Port name for error messages.
        context: Logging context string.

    Returns:
        Number of bytes written (always ``len(data)`` on success).

    Raises:
        SerialCommunicationError: If a short write is detected (fewer bytes
            written than requested).
    """
    n = ser.write(data)
    if n != len(data):
        raise SerialCommunicationError(
            f"[{context}] Short write on {port_name}: "
            f"wrote {n}/{len(data)} bytes. "
            f"This usually means write_timeout is 0 (non-blocking) "
            f"and the kernel buffer is full."
        )
    ser.flush()
    logger.debug(
        "[SERIAL-WRITE-ALL] [%s] Wrote %d bytes to %s",
        context, n, port_name,
    )
    return n


class SerialConnectionManager:
    """Manages a serial port connection with automatic resource cleanup.

    Follows the same context-manager pattern used by SSHConnectionManager so
    the two can be mixed naturally in automation scripts.

    Example::

        with SerialConnectionManager("/dev/ttyUSB0") as mgr:
            reader = SerialReader(mgr)
            data = reader.flush_and_read(context="read boot log", duration_ms=3000)
            print(data)
    """

    def __init__(
        self,
        port: str,
        baud_rate: int = SERIAL_BAUD_RATE,
        bytesize: int = SERIAL_BYTESIZE,
        parity: str = SERIAL_PARITY,
        stopbits: int = SERIAL_STOPBITS,
        read_timeout: float = SERIAL_READ_TIMEOUT,
        write_timeout: Optional[float] = SERIAL_WRITE_TIMEOUT,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        rx_buffer_size: int = SERIAL_RX_BUFFER_SIZE,
        poll_interval_s: float = SERIAL_POLL_INTERVAL_S,
    ) -> None:
        """Initialize serial connection manager.

        Args:
            port: Serial port path — e.g. ``/dev/ttyUSB0`` (Linux) or ``COM3`` (Windows).
            baud_rate: Baud rate (default: 115200).
            bytesize: Number of data bits (5, 6, 7, or 8; default: 8).
            parity: Parity setting — ``"N"`` (none), ``"E"`` (even), ``"O"`` (odd),
                    ``"M"`` (mark), ``"S"`` (space).  Default: ``"N"``.
            stopbits: Number of stop bits (1 or 2; default: 1).
            read_timeout: Per-read timeout in seconds used internally for polling
                          granularity.  This is **not** the user-facing read duration.
                          Default: 0 (non-blocking).
            write_timeout: Write timeout in seconds.  Default: 10 (blocking with
                          failsafe).  ``None`` means block forever, ``0`` means
                          non-blocking (risk of silent data loss — not recommended).
            xonxoff: Enable software flow control (XON/XOFF).  Default: ``False``.
            rtscts: Enable hardware (RTS/CTS) flow control.  Default: ``False``.
            dsrdtr: Enable hardware (DSR/DTR) flow control.  Default: ``False``.
            rx_buffer_size: OS receive buffer size in bytes.  ``0`` (default) leaves
                           the driver default untouched.  On Windows the default is
                           often only 4 KB — setting this to 65536 or higher helps
                           prevent data loss at high baud rates.
            poll_interval_s: Poll loop sleep granularity in seconds.  Default: 0.01
                            (10 ms).  Lower values (e.g. 0.002) drain the OS buffer
                            more aggressively at the cost of slightly higher CPU usage.

        Raises:
            SerialCommunicationError: If any parameter value is invalid.
        """
        self.port = port
        self.baud_rate = baud_rate
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self.xonxoff = xonxoff
        self.rtscts = rtscts
        self.dsrdtr = dsrdtr
        self.rx_buffer_size = rx_buffer_size
        self.poll_interval_s = poll_interval_s
        self._serial: Optional[serial.Serial] = None

        # ---- Validate and resolve bytesize ----
        if bytesize not in _BYTESIZE_MAP:
            valid = ", ".join(str(k) for k in sorted(_BYTESIZE_MAP))
            raise SerialCommunicationError(
                f"Invalid bytesize {bytesize!r} for port {port}. "
                f"Must be one of: {valid}. "
                f"Standard UART uses 8 data bits (bytesize=8)."
            )
        self.bytesize = _BYTESIZE_MAP[bytesize]

        # ---- Validate and resolve parity ----
        parity_upper = parity.upper()
        if parity_upper not in _PARITY_MAP:
            valid = ", ".join(f'"{k}"' for k in sorted(_PARITY_MAP))
            raise SerialCommunicationError(
                f"Invalid parity {parity!r} for port {port}. "
                f"Must be one of: {valid}. "
                f'Standard UART uses no parity (parity="N").'
            )
        self.parity = _PARITY_MAP[parity_upper]

        # ---- Validate and resolve stopbits ----
        if stopbits not in _STOPBITS_MAP:
            valid = ", ".join(str(k) for k in sorted(_STOPBITS_MAP))
            raise SerialCommunicationError(
                f"Invalid stopbits {stopbits!r} for port {port}. "
                f"Must be one of: {valid}. "
                f"Standard UART uses 1 stop bit (stopbits=1)."
            )
        self.stopbits = _STOPBITS_MAP[stopbits]

        # ---- Validate baud rate ----
        if baud_rate <= 0:
            raise SerialCommunicationError(
                f"Invalid baud rate {baud_rate!r} for port {port}. "
                f"Baud rate must be a positive integer. "
                f"Common values: 9600, 19200, 38400, 57600, 115200."
            )

        # ---- Validate write timeout ----
        if write_timeout is not None and write_timeout < 0:
            raise SerialCommunicationError(
                f"Invalid write_timeout {write_timeout!r} for port {port}. "
                f"Must be None (blocking), 0 (non-blocking), or a positive number."
            )

        flow = []
        if xonxoff:
            flow.append("XON/XOFF")
        if rtscts:
            flow.append("RTS/CTS")
        if dsrdtr:
            flow.append("DSR/DTR")
        flow_str = "+".join(flow) if flow else "none"

        logger.info(
            "[SERIAL-INIT] Configured %s — %d %d%s%s (read_timeout=%.2fs, "
            "write_timeout=%s, flow=%s, rx_buf=%s, poll=%.3fs)",
            port, baud_rate, bytesize, parity, stopbits, read_timeout,
            f"{write_timeout:.2f}s" if write_timeout is not None else "None (blocking)",
            flow_str,
            f"{rx_buffer_size}" if rx_buffer_size > 0 else "default",
            poll_interval_s,
        )

    def open(self, context: str) -> None:
        """Open the serial port.

        Args:
            context: Description of the purpose, embedded into error messages.

        Raises:
            SerialCommunicationError: If the port cannot be opened.  The error
                message includes the OS-level reason, the port path, and
                platform-specific troubleshooting hints.
        """
        if self._serial is not None and self._serial.is_open:
            logger.debug("[SERIAL-OPEN] [%s] Port %s is already open — skipping", context, self.port)
            return

        logger.info(
            "[SERIAL-OPEN] [%s] Opening %s at %d baud ...", context, self.port, self.baud_rate,
        )

        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baud_rate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.read_timeout,
                write_timeout=self.write_timeout,
                xonxoff=self.xonxoff,
                rtscts=self.rtscts,
                dsrdtr=self.dsrdtr,
            )
            if self.rx_buffer_size > 0:
                self._serial.set_buffer_size(
                    rx_size=self.rx_buffer_size,
                    tx_size=self.rx_buffer_size,
                )
                logger.info(
                    "[SERIAL-OPEN] [%s] Set OS buffer size to %d bytes on %s",
                    context, self.rx_buffer_size, self.port,
                )
            logger.info("[SERIAL-OPEN] [%s] Successfully opened %s", context, self.port)

        except serial.SerialException as exc:
            hint = self._platform_hint()
            msg = (
                f"[{context}] Failed to open serial port {self.port} at {self.baud_rate} baud: {exc}. "
                f"{hint}"
            )
            logger.error("[SERIAL-OPEN] FAILED — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            hint = self._platform_hint()
            msg = (
                f"[{context}] OS error opening serial port {self.port}: {exc}. "
                f"{hint}"
            )
            logger.error("[SERIAL-OPEN] OS ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

    def is_open(self) -> bool:
        """Check whether the serial port is currently open."""
        return self._serial is not None and self._serial.is_open

    def close(self) -> None:
        """Close the serial port if open."""
        was_open = self.is_open()

        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as exc:
                logger.warning(
                    "[SERIAL-CLOSE] Error closing port %s: %s", self.port, exc,
                )
            finally:
                self._serial = None

        if was_open:
            logger.info("[SERIAL-CLOSE] Closed %s", self.port)
        else:
            logger.debug(
                "[SERIAL-CLOSE] close() called on already-closed port %s", self.port,
            )

    def get_serial(self) -> serial.Serial:
        """Return the underlying ``serial.Serial`` object.

        Raises:
            SerialCommunicationError: If the port is not open.
        """
        if self._serial is None or not self._serial.is_open:
            raise SerialCommunicationError(
                f"Cannot access serial port {self.port}: port is not open. "
                f"Call open() or use the context manager first."
            )
        return self._serial

    # ---- Context manager ----

    def __enter__(self) -> SerialConnectionManager:
        """Context manager entry — opens the serial port."""
        self.open(context=f"Opening {self.port}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        """Context manager exit — ensures the port is closed."""
        self.close()

    def __del__(self) -> None:
        """Destructor — ensures the port is closed."""
        try:
            self.close()
        except Exception:
            pass

    # ---- Helpers ----

    @staticmethod
    def list_available_ports() -> List[str]:
        """Return a list of serial port names visible to the operating system.

        Useful for diagnostics when the caller is unsure which port to use.
        """
        ports = serial.tools.list_ports.comports()
        descriptions = []
        for p in ports:
            descriptions.append(f"{p.device} — {p.description}")
            logger.debug("[SERIAL-LIST] Found port: %s (%s)", p.device, p.description)
        return descriptions

    def _platform_hint(self) -> str:
        """Return a platform-specific troubleshooting hint."""
        if _IS_WINDOWS:
            return (
                "On Windows: verify the COM port number in Device Manager "
                "(Ports → COM & LPT). Ensure no other application (PuTTY, "
                "TeraTerm, Arduino IDE) has the port open. "
                "Available ports: " + ", ".join(
                    p.device for p in serial.tools.list_ports.comports()
                ) + "."
            )
        return (
            "On Linux: verify the device path exists (ls /dev/ttyUSB* /dev/ttyACM* "
            "/dev/ttyS*). Ensure your user is in the 'dialout' group "
            "(sudo usermod -aG dialout $USER) and that no other process "
            "(minicom, screen, picocom) has the port open. "
            "Available ports: " + ", ".join(
                p.device for p in serial.tools.list_ports.comports()
            ) + "."
        )


@typechecked
class SerialReader:
    """Reads data from a serial port with a flush-then-read pattern.

    Designed for the common embedded workflow:

    1. **Flush** — discard any stale data sitting in the OS receive buffer.
    2. **Read for N milliseconds** — accumulate every byte that arrives
       within the requested time window.
    3. **Return** — hand back the decoded string, the byte count, and the
       actual elapsed time.

    Example::

        with SerialConnectionManager("/dev/ttyUSB0") as mgr:
            reader = SerialReader(mgr)

            # Issue a command via SSH, then capture the serial console output
            # that appears within the next 3 seconds.
            data = reader.flush_and_read(context="capture boot log", duration_ms=3000)
            print(f"Serial output: {data!r}")
    """

    def __init__(self, connection_manager: SerialConnectionManager) -> None:
        """Initialize serial reader.

        Args:
            connection_manager: An **open** ``SerialConnectionManager``.
        """
        self.connection_manager = connection_manager

    def flush(self, context: str) -> int:
        """Flush (discard) all data currently waiting in the serial receive buffer.

        Args:
            context: Description of the purpose, embedded into error messages.

        Returns:
            Number of bytes that were discarded.

        Raises:
            SerialCommunicationError: If the port is not open or the flush fails.
        """
        port_name = self.connection_manager.port

        if not self.connection_manager.is_open():
            msg = (
                f"[{context}] Cannot flush serial port {port_name}: port is not open. "
                f"Did you forget to call open() or use a context manager?"
            )
            logger.error("[SERIAL-FLUSH] %s", msg)
            raise SerialCommunicationError(msg)

        ser = self.connection_manager.get_serial()

        try:
            waiting = ser.in_waiting
            if waiting > 0:
                discarded = ser.read(waiting)
                logger.info(
                    "[SERIAL-FLUSH] [%s] Discarded %d stale bytes from %s",
                    context, len(discarded), port_name,
                )
                # Also reset the OS-level buffers
                ser.reset_input_buffer()
                return len(discarded)

            ser.reset_input_buffer()
            logger.info("[SERIAL-FLUSH] [%s] Input buffer on %s was already empty", context, port_name)
            return 0

        except serial.SerialException as exc:
            msg = (
                f"[{context}] Error flushing serial port {port_name}: {exc}. "
                f"The port may have been disconnected or the USB cable unplugged."
            )
            logger.error("[SERIAL-FLUSH] ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"[{context}] OS error flushing serial port {port_name}: {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-FLUSH] OS ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

    def read_for_duration(
        self,
        context: str,
        duration_ms: int = SERIAL_DEFAULT_DURATION_MS,
        encoding: str = "utf-8",
    ) -> Tuple[str, int, float]:
        """Read from the serial port for exactly ``duration_ms`` milliseconds.

        Accumulates all bytes received during the time window, then decodes
        them as a single string.

        Args:
            context: Description of the purpose, embedded into error messages.
            duration_ms: How long to listen, in milliseconds.  Must be > 0.
            encoding: Character encoding for decoding the raw bytes.
                      Default: ``"utf-8"``.  Use ``"latin-1"`` for raw 8-bit
                      passthrough if the device sends non-UTF-8 data.

        Returns:
            A 3-tuple of ``(decoded_string, bytes_received, actual_seconds)``.

        Raises:
            SerialCommunicationError: If the port is not open or a read error
                occurs.
            SerialTimeoutError: If zero bytes were received during the entire
                duration (the line was completely silent).
        """
        port_name = self.connection_manager.port

        if duration_ms <= 0:
            raise SerialCommunicationError(
                f"[{context}] Invalid read duration {duration_ms} ms for port {port_name}. "
                f"Duration must be a positive integer (e.g. 2000 for 2 seconds)."
            )

        if not self.connection_manager.is_open():
            msg = (
                f"[{context}] Cannot read from serial port {port_name}: port is not open. "
                f"Did you forget to call open() or use a context manager?"
            )
            logger.error("[SERIAL-READ] %s", msg)
            raise SerialCommunicationError(msg)

        ser = self.connection_manager.get_serial()
        duration_s = duration_ms / 1000.0

        logger.info(
            "[SERIAL-READ] [%s] Listening on %s for %d ms (%.2fs) ...",
            context, port_name, duration_ms, duration_s,
        )

        try:
            loop_result = _poll_read_loop(
                ser=ser,
                port_name=port_name,
                timeout_s=duration_s,
                stop_condition=None,
                on_data=None,
                encoding=encoding,
                context=context,
                poll_interval_s=self.connection_manager.poll_interval_s,
            )
        except serial.SerialException as exc:
            msg = (
                f"[{context}] Serial read error on {port_name}: {exc}. "
                f"The device may have been disconnected during the read."
            )
            logger.error("[SERIAL-READ] ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"[{context}] OS error reading from {port_name}: {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-READ] OS ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

        elapsed = loop_result.elapsed_seconds
        bytes_received = loop_result.bytes_received

        logger.info(
            "[SERIAL-READ] [%s] Completed on %s — %d bytes in %.3fs (%d poll cycles)",
            context, port_name, bytes_received, elapsed, loop_result.read_cycles,
        )

        if bytes_received == 0:
            msg = (
                f"[{context}] No data received from serial port {port_name} during "
                f"{duration_ms} ms read window. The serial line was completely "
                f"silent. Possible causes: (1) the remote device did not send "
                f"any output, (2) the baud rate ({self.connection_manager.baud_rate}) "
                f"does not match the device, (3) TX/RX lines are swapped or "
                f"disconnected, (4) the wrong serial port was specified. "
                f"Available ports: "
                + ", ".join(p.device for p in serial.tools.list_ports.comports())
                + "."
            )
            logger.warning("[SERIAL-READ] TIMEOUT (no data) — %s", msg)
            raise SerialTimeoutError(msg)

        decoded = loop_result.decoded
        if not decoded:
            try:
                decoded = loop_result.buffer.decode(encoding, errors="replace")
            except (UnicodeDecodeError, LookupError) as exc:
                msg = (
                    f"[{context}] Failed to decode {bytes_received} bytes from {port_name} "
                    f"using encoding {encoding!r}: {exc}. "
                    f'Try encoding="latin-1" for raw 8-bit passthrough.'
                )
                logger.error("[SERIAL-READ] DECODE ERROR — %s", msg)
                raise SerialCommunicationError(msg) from exc

        logger.info(
            "[SERIAL-READ] [%s] Decoded %d bytes → %d characters from %s",
            context, bytes_received, len(decoded), port_name,
        )

        return (decoded, bytes_received, elapsed)

    def flush_and_read(
        self,
        context: str,
        duration_ms: int = SERIAL_DEFAULT_DURATION_MS,
        encoding: str = "utf-8",
    ) -> str:
        """Convenience: flush stale data, then read for a duration.

        This is the primary method for the typical workflow:

        1. Discard anything sitting in the buffer from previous activity.
        2. Read fresh data arriving within the next ``duration_ms`` ms.
        3. Return the decoded string (only).

        Args:
            context: Description of the purpose, embedded into error messages.
            duration_ms: How long to listen after flushing, in milliseconds.
            encoding: Character encoding (default ``"utf-8"``).

        Returns:
            The decoded string received during the read window.

        Raises:
            SerialCommunicationError: On I/O errors.
            SerialTimeoutError: If no data was received.
        """
        port_name = self.connection_manager.port

        logger.info(
            "[SERIAL-FLUSH+READ] [%s] Starting flush-then-read on %s (duration=%d ms) ...",
            context, port_name, duration_ms,
        )

        discarded = self.flush(context)
        if discarded > 0:
            logger.info(
                "[SERIAL-FLUSH+READ] [%s] Flushed %d stale bytes before reading on %s",
                context, discarded, port_name,
            )

        decoded, bytes_received, elapsed = self.read_for_duration(context, duration_ms, encoding)

        logger.info(
            "[SERIAL-FLUSH+READ] [%s] Done on %s — received %d bytes (%.3fs): %s",
            context, port_name, bytes_received, elapsed,
            decoded[:200] + ("..." if len(decoded) > 200 else ""),
        )

        return decoded

    def write_and_read(
        self,
        data: str,
        context: str,
        duration_ms: int = SERIAL_DEFAULT_DURATION_MS,
        encoding: str = "utf-8",
    ) -> str:
        """Flush, write a string to the serial port, then read the response.

        Useful when the serial device expects a command before it sends output
        (e.g. an AT-command interface or a U-Boot prompt).

        Args:
            data: The string to send over serial (a newline is **not**
                  appended automatically — include ``"\\n"`` if needed).
            context: Description of the purpose, embedded into error messages.
            duration_ms: How long to listen for a response after writing.
            encoding: Character encoding for both writing and reading.

        Returns:
            The decoded response string.

        Raises:
            SerialCommunicationError: On I/O errors.
            SerialTimeoutError: If no response was received.
        """
        port_name = self.connection_manager.port

        if not self.connection_manager.is_open():
            msg = (
                f"[{context}] Cannot write to serial port {port_name}: port is not open. "
                f"Did you forget to call open() or use a context manager?"
            )
            logger.error("[SERIAL-WRITE] %s", msg)
            raise SerialCommunicationError(msg)

        ser = self.connection_manager.get_serial()

        logger.info(
            "[SERIAL-WRITE+READ] [%s] Flushing, writing %d bytes to %s, "
            "then reading for %d ms ...",
            context, len(data), port_name, duration_ms,
        )

        self.flush(context)

        try:
            encoded = data.encode(encoding)
            bytes_written = _write_all(ser, encoded, port_name, context=context)
            logger.info(
                "[SERIAL-WRITE] [%s] Wrote %d bytes to %s: %s",
                context, bytes_written, port_name,
                data[:100] + ("..." if len(data) > 100 else ""),
            )
        except serial.SerialException as exc:
            msg = (
                f"[{context}] Failed to write to serial port {port_name}: {exc}. "
                f"The device may have been disconnected."
            )
            logger.error("[SERIAL-WRITE] ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"[{context}] OS error writing to serial port {port_name}: {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-WRITE] OS ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

        decoded, bytes_received, elapsed = self.read_for_duration(context, duration_ms, encoding)

        logger.info(
            "[SERIAL-WRITE+READ] [%s] Done on %s — wrote %d bytes, received %d bytes (%.3fs)",
            context, port_name, bytes_written, bytes_received, elapsed,
        )

        return decoded

    def read_until(
        self,
        condition: Union[str, Callable[[str], bool]],
        context: str,
        timeout_ms: int = SERIAL_COMMAND_TIMEOUT_MS,
        on_data: Optional[Callable[[str], None]] = None,
        encoding: str = "utf-8",
    ) -> Tuple[str, int, float, bool]:
        """Read from the serial port until a condition is met or timeout expires.

        Does **not** send any data — pure read.  Useful for waiting on
        asynchronous device output (e.g. boot messages, unsolicited events).

        Args:
            condition: Either a substring to match (``str``), or a callable
                that receives the **full accumulated decoded text** and
                returns ``True`` to stop.
            context: Description of the purpose, embedded into error messages.
            timeout_ms: Hard upper-bound in milliseconds.  Default: 30 000 ms.
            on_data: Optional streaming callback invoked with each new decoded
                chunk as it arrives.
            encoding: Character encoding (default ``"utf-8"``).

        Returns:
            A 4-tuple of ``(decoded_text, bytes_received, elapsed_seconds,
            matched)``.  ``matched`` is ``True`` if the condition was
            satisfied before timeout.

        Raises:
            SerialCommunicationError: If the port is not open or a read error
                occurs.
            SerialTimeoutError: If the timeout expires before the condition
                matches.
        """
        port_name = self.connection_manager.port

        if timeout_ms <= 0:
            raise SerialCommunicationError(
                f"[{context}] Invalid timeout {timeout_ms} ms for read_until on "
                f"{port_name}. Timeout must be a positive integer."
            )

        if not self.connection_manager.is_open():
            msg = (
                f"[{context}] Cannot read from serial port {port_name}: port is not open. "
                f"Did you forget to call open() or use a context manager?"
            )
            logger.error("[SERIAL-READ-UNTIL] %s", msg)
            raise SerialCommunicationError(msg)

        ser = self.connection_manager.get_serial()

        # Normalise condition: str → substring match callable
        if isinstance(condition, str):
            match_str = condition
            stop_fn: Callable[[str], bool] = lambda text: match_str in text
        else:
            stop_fn = condition

        logger.info(
            "[SERIAL-READ-UNTIL] [%s] Waiting on %s (up to %d ms) ...",
            context, port_name, timeout_ms,
        )

        try:
            loop_result = _poll_read_loop(
                ser=ser,
                port_name=port_name,
                timeout_s=timeout_ms / 1000.0,
                stop_condition=stop_fn,
                on_data=on_data,
                encoding=encoding,
                context=context,
                poll_interval_s=self.connection_manager.poll_interval_s,
            )
        except serial.SerialException as exc:
            msg = (
                f"[{context}] Serial read error on {port_name}: {exc}. "
                f"The device may have been disconnected during read_until."
            )
            logger.error("[SERIAL-READ-UNTIL] ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"[{context}] OS error reading from {port_name}: {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-READ-UNTIL] OS ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

        matched = loop_result.stopped_by_condition

        logger.info(
            "[SERIAL-READ-UNTIL] [%s] Done on %s — %d bytes in %.3fs (matched=%s)",
            context, port_name, loop_result.bytes_received,
            loop_result.elapsed_seconds, matched,
        )

        if not matched:
            msg = (
                f"[{context}] Timeout ({timeout_ms} ms) expired on {port_name} before "
                f"read_until condition matched. Received {loop_result.bytes_received} "
                f"bytes in {loop_result.elapsed_seconds:.3f}s. "
                f"Output tail: {loop_result.decoded[-200:] if loop_result.decoded else '(empty)'}"
            )
            logger.warning("[SERIAL-READ-UNTIL] TIMEOUT — %s", msg)
            raise SerialTimeoutError(msg)

        return (loop_result.decoded, loop_result.bytes_received,
                loop_result.elapsed_seconds, matched)


# ---------------------------------------------------------------------------
# Serial command execution
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SerialCommandResult:
    """Immutable result of a serial command execution.

    Attributes:
        command: The command string that was sent.
        output: All data received after the command was sent, decoded.
        bytes_received: Total raw bytes received.
        elapsed_seconds: Wall-clock time from command send to completion.
        stopped_by_condition: ``True`` if a caller-supplied stop-condition
            matched.  ``False`` if the read ended because the timeout expired.
        timed_out: ``True`` only when a ``stop_condition`` was provided but
            was **not** matched before the timeout expired.  When no
            ``stop_condition`` is given, ``timed_out`` is always ``False``
            (the timeout is simply the expected read duration, not a failure).
    """
    command: str
    output: str
    bytes_received: int
    elapsed_seconds: float
    stopped_by_condition: bool
    timed_out: bool


@typechecked
class SerialCommandExecutor:
    """Executes commands over a serial console with immaculate cleanup.

    Designed for the workflow where the serial line is connected to a device
    shell (Linux console, U-Boot, busybox, etc.) and you want to:

    1. **Wake up** the line with a bare ENTER.
    2. **Flush** every stale byte from both OS and device buffers.
    3. **Send** the command followed by ENTER.
    4. **Block** while accumulating the response.
    5. **Stop** when a caller-defined condition matches the accumulated
       output, *or* when a hard timeout expires — whichever comes first.
    6. Optionally **stream** every chunk to a callback as it arrives.

    The method works perfectly fine when no data comes back at all (returns
    an empty-output result rather than raising).

    Example::

        with SerialConnectionManager("/dev/ttyUSB0") as mgr:
            executor = SerialCommandExecutor(mgr)

            # Run 'ls /' and wait for the shell prompt '# ' to reappear
            result = executor.execute_command(
                "ls /",
                context="list root filesystem",
                timeout_ms=5000,
                stop_condition=lambda text: "# " in text.rsplit("\\n", 1)[-1],
                on_data=lambda chunk: print(chunk, end="", flush=True),
            )

            print(f"Done (condition={result.stopped_by_condition})")
            print(result.output)
    """

    def __init__(self, connection_manager: SerialConnectionManager) -> None:
        """Initialize serial command executor.

        Args:
            connection_manager: An **open** ``SerialConnectionManager``.
        """
        self.connection_manager = connection_manager

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_open(self, operation: str, context: str) -> serial.Serial:
        """Validate the port is open and return the underlying Serial object.

        Raises:
            SerialCommunicationError: If the port is not open.
        """
        port_name = self.connection_manager.port
        if not self.connection_manager.is_open():
            msg = (
                f"[{context}] Cannot {operation} on serial port {port_name}: port is not "
                f"open. Did you forget to call open() or use a context manager?"
            )
            logger.error("[SERIAL-CMD] %s", msg)
            raise SerialCommunicationError(msg)
        return self.connection_manager.get_serial()

    def _drain_and_reset(
        self,
        ser: serial.Serial,
        label: str,
        context: str,
        max_drain_s: float = 2.0,
    ) -> int:
        """Read and discard every byte in the receive buffer, then reset it.

        This is a *thorough* flush: we first drain all application-visible
        bytes (``in_waiting``), then call ``reset_input_buffer()`` to clear
        the OS-level kernel buffer, then ``reset_output_buffer()`` to ensure
        no outbound bytes are still in-flight from a previous write.

        A deadline of *max_drain_s* seconds prevents infinite loops when
        ``in_waiting`` keeps reporting phantom bytes while ``read()``
        returns empty.

        Args:
            ser: The open serial object.
            label: A logging label describing *why* we're flushing (e.g.
                ``"pre-wake"`` or ``"post-wake"``).
            context: Description of the purpose, embedded into error messages.
            max_drain_s: Maximum time in seconds to spend draining.
                Default: 2.0 s.

        Returns:
            Number of application-level bytes that were discarded.
        """
        port_name = self.connection_manager.port
        total_discarded = 0

        try:
            # Drain in a loop — bytes can keep arriving between reads
            drain_deadline = time.monotonic() + max_drain_s
            while True:
                if time.monotonic() >= drain_deadline:
                    logger.warning(
                        "[SERIAL-CMD] [%s] [%s] Drain timed out after %.1fs on %s "
                        "(discarded %d bytes so far)",
                        context, label, max_drain_s, port_name, total_discarded,
                    )
                    break
                waiting = ser.in_waiting
                if waiting <= 0:
                    break
                chunk = ser.read(waiting)
                if len(chunk) == 0:
                    break  # in_waiting lied — stop draining
                total_discarded += len(chunk)
                logger.debug(
                    "[SERIAL-CMD] [%s] [%s] Drained %d bytes from %s (total discarded: %d)",
                    context, label, len(chunk), port_name, total_discarded,
                )

            ser.reset_input_buffer()
            ser.reset_output_buffer()

            if total_discarded > 0:
                logger.info(
                    "[SERIAL-CMD] [%s] [%s] Discarded %d stale bytes and reset "
                    "I/O buffers on %s",
                    context, label, total_discarded, port_name,
                )
            else:
                logger.debug(
                    "[SERIAL-CMD] [%s] [%s] Buffers already clean on %s",
                    context, label, port_name,
                )

        except serial.SerialException as exc:
            msg = (
                f"[{context}] Error during {label} flush on {port_name}: {exc}. "
                f"The port may have been disconnected."
            )
            logger.error("[SERIAL-CMD] [%s] ERROR — %s", label, msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"[{context}] OS error during {label} flush on {port_name}: {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-CMD] [%s] OS ERROR — %s", label, msg)
            raise SerialCommunicationError(msg) from exc

        return total_discarded

    def _send_bytes(self, ser: serial.Serial, data: bytes, label: str, context: str) -> int:
        """Write raw bytes and flush the OS transmit buffer.

        Args:
            ser: The open serial object.
            data: Bytes to send.
            label: Logging label.
            context: Description of the purpose, embedded into error messages.

        Returns:
            Number of bytes written.

        Raises:
            SerialCommunicationError: On write failure.
        """
        port_name = self.connection_manager.port
        try:
            n = _write_all(ser, data, port_name, context=f"{context}/{label}")
            logger.debug(
                "[SERIAL-CMD] [%s] [%s] Sent %d bytes to %s: %r",
                context, label, n, port_name, data,
            )
            return n
        except serial.SerialException as exc:
            msg = (
                f"[{context}] Failed to write ({label}) to serial port {port_name}: {exc}. "
                f"Attempted to send {len(data)} bytes: {data!r}. "
                f"The device may have been disconnected."
            )
            logger.error("[SERIAL-CMD] [%s] WRITE ERROR — %s", label, msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"[{context}] OS error writing ({label}) to serial port {port_name}: {exc}. "
                f"Attempted to send {len(data)} bytes: {data!r}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-CMD] [%s] OS WRITE ERROR — %s", label, msg)
            raise SerialCommunicationError(msg) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_command(
        self,
        command: str,
        context: str,
        timeout_ms: int,
        stop_condition: Optional[Callable[[str], bool]],
        on_data: Optional[Callable[[str], None]] = None,
        encoding: str = "utf-8",
        prompt_settle_ms: int = SERIAL_PROMPT_SETTLE_MS,
    ) -> SerialCommandResult:
        """Send a command over the serial console and capture the response.

        **Sequence of operations (immaculate cleanup):**

        1. **Drain + reset** — discard every byte in the receive buffer and
           reset both the input and output OS-level buffers.
        2. **Wake-up ENTER** — send a bare ``\\r\\n`` to ensure the remote
           shell is at a prompt (handles sleeping consoles, partial
           previous input, etc.).
        3. **Settle pause** — wait ``prompt_settle_ms`` for any prompt /
           echo to appear.
        4. **Drain + reset again** — discard the prompt echo and any other
           noise produced by the wake-up ENTER so the read starts perfectly
           clean.
        5. **Send command + ENTER** — write ``command`` followed by ``\\r\\n``.
        6. **Blocking read loop** — accumulate bytes until:
           - ``stop_condition(accumulated_text)`` returns ``True``, **or**
           - ``timeout_ms`` milliseconds have elapsed.
        7. Return a ``SerialCommandResult`` with all details.

        Args:
            command: The command string to execute (without trailing newline).
            context: Description of the purpose, embedded into error messages.
            timeout_ms: Hard upper-bound in milliseconds.  The read will
                always stop when this expires, regardless of the stop
                condition.  Required — no default is provided so the caller
                must make a conscious choice.
            stop_condition: A callable that receives the **full accumulated
                decoded text so far** and returns ``True`` when the command
                is considered complete.  Common patterns::

                    # Stop when a shell prompt reappears
                    stop_condition=lambda t: t.rstrip().endswith("# ")

                    # Stop when a specific string appears anywhere
                    stop_condition=lambda t: "DONE" in t

                Pass ``None`` explicitly to read for the full ``timeout_ms``.
            on_data: An optional streaming callback invoked with each **new
                decoded chunk** as it arrives.  Useful for real-time
                display::

                    on_data=lambda chunk: print(chunk, end="", flush=True)

                The callback must not raise.  If it does, the exception is
                logged and swallowed so that the read loop is not disrupted.
            encoding: Character encoding (default ``"utf-8"``).
            prompt_settle_ms: How long to wait after the wake-up ENTER
                before flushing the echo and sending the actual command.
                Default: 200 ms.

        Returns:
            A ``SerialCommandResult`` with the command output, timing, and
            whether the stop-condition fired or the timeout expired.

        Raises:
            SerialCommunicationError: If the port is not open, or a fatal
                I/O error occurs during the write or read phase.
            SerialTimeoutError: If a ``stop_condition`` was provided and the
                timeout expired before it matched.  The ``.result`` attribute
                contains the partial ``SerialCommandResult``.
        """
        port_name = self.connection_manager.port
        ser = self._assert_open("execute_command", context)

        if timeout_ms <= 0:
            raise SerialCommunicationError(
                f"[{context}] Invalid timeout {timeout_ms} ms for execute_command on "
                f"{port_name}. Timeout must be a positive integer "
                f"(e.g. 5000 for 5 seconds)."
            )

        logger.info(
            "[SERIAL-CMD] [%s] Executing on %s: %r (timeout=%d ms, "
            "stop_condition=%s, streaming=%s)",
            context, port_name, command, timeout_ms,
            "yes" if stop_condition is not None else "no",
            "yes" if on_data is not None else "no",
        )

        # ---- Phase 1: Pre-command cleanup ----
        logger.info("[SERIAL-CMD] [%s] Phase 1/4: Draining stale data on %s ...", context, port_name)
        self._drain_and_reset(ser, "pre-wake", context)

        # ---- Phase 2: Wake-up ENTER + settle + second flush ----
        logger.info(
            "[SERIAL-CMD] [%s] Phase 2/4: Sending wake-up ENTER on %s, "
            "settling %d ms ...",
            context, port_name, prompt_settle_ms,
        )
        self._send_bytes(ser, b"\r\n", "wake-up-enter", context)

        if prompt_settle_ms > 0:
            time.sleep(prompt_settle_ms / 1000.0)

        self._drain_and_reset(ser, "post-wake", context)

        # ---- Phase 3: Send command + ENTER ----
        logger.info(
            "[SERIAL-CMD] [%s] Phase 3/4: Sending command on %s: %r",
            context, port_name, command,
        )
        cmd_bytes = (command + "\r\n").encode(encoding)
        self._send_bytes(ser, cmd_bytes, "command", context)

        # ---- Phase 4: Blocking read loop ----
        logger.info(
            "[SERIAL-CMD] [%s] Phase 4/4: Reading response on %s (up to %d ms) ...",
            context, port_name, timeout_ms,
        )

        try:
            loop_result = _poll_read_loop(
                ser=ser,
                port_name=port_name,
                timeout_s=timeout_ms / 1000.0,
                stop_condition=stop_condition,
                on_data=on_data,
                encoding=encoding,
                context=context,
                poll_interval_s=self.connection_manager.poll_interval_s,
            )
        except serial.SerialException as exc:
            elapsed = time.monotonic()
            msg = (
                f"[{context}] Serial read error on {port_name} during execute_command "
                f"(command={command!r}): {exc}. "
                f"The device may have been disconnected during the read."
            )
            logger.error("[SERIAL-CMD] READ ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"[{context}] OS error reading from {port_name} during execute_command "
                f"(command={command!r}): {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-CMD] OS READ ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

        elapsed = loop_result.elapsed_seconds
        bytes_received = loop_result.bytes_received
        stopped_by_condition = loop_result.stopped_by_condition
        decoded_so_far = loop_result.decoded

        # timed_out is True only when a stop_condition was given but not matched
        timed_out = (stop_condition is not None) and (not stopped_by_condition)

        result = SerialCommandResult(
            command=command,
            output=decoded_so_far,
            bytes_received=bytes_received,
            elapsed_seconds=elapsed,
            stopped_by_condition=stopped_by_condition,
            timed_out=timed_out,
        )

        if timed_out:
            msg = (
                f"[{context}] Timeout ({timeout_ms} ms) expired on {port_name} before "
                f"stop condition matched. Received {bytes_received} bytes in "
                f"{elapsed:.3f}s. Command: {command!r}. "
                f"Output tail: {decoded_so_far[-200:] if decoded_so_far else '(empty)'}"
            )
            logger.warning("[SERIAL-CMD] %s", msg)
            raise SerialTimeoutError(msg, result=result)

        logger.info(
            "[SERIAL-CMD] [%s] Completed on %s — %d bytes in %.3fs "
            "(%d poll cycles, stopped_by_condition=%s). Command: %r",
            context, port_name, bytes_received, elapsed, loop_result.read_cycles,
            stopped_by_condition, command,
        )

        return result
