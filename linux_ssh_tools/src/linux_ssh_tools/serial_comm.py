"""Serial communication with flush-then-read pattern for hardware consoles.

Provides a robust interface for reading data from serial lines attached to
Linux devices (or any UART-accessible hardware).  Typical use-case: issue a
command over SSH, then capture the serial response that arrives within a
configurable time window.

Cross-platform: works on both Windows 10 (COMx) and Ubuntu 24.04
(/dev/ttyUSB*, /dev/ttyS*, /dev/ttyACM*).

Default line settings: 115200 8N1 (no flow control).
"""

from __future__ import annotations

import logging
import platform
import time
from typing import Optional, Tuple

import serial
import serial.tools.list_ports
from typeguard import typechecked

from . import (
    SERIAL_BAUD_RATE,
    SERIAL_BYTESIZE,
    SERIAL_PARITY,
    SERIAL_STOPBITS,
    SERIAL_READ_TIMEOUT,
    SERIAL_DEFAULT_DURATION_MS,
)
from .exceptions import SerialCommunicationError, SerialTimeoutError

logger = logging.getLogger("linux_ssh_tools.serial_comm")

_IS_WINDOWS = platform.system() == "Windows"

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


class SerialConnectionManager:
    """Manages a serial port connection with automatic resource cleanup.

    Follows the same context-manager pattern used by SSHConnectionManager so
    the two can be mixed naturally in automation scripts.

    Example::

        with SerialConnectionManager("/dev/ttyUSB0") as mgr:
            reader = SerialReader(mgr)
            data = reader.flush_and_read(duration_ms=3000)
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
                          Default: 0.1 s.

        Raises:
            SerialCommunicationError: If any parameter value is invalid.
        """
        self.port = port
        self.baud_rate = baud_rate
        self.read_timeout = read_timeout
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

        logger.info(
            "[SERIAL-INIT] Configured %s — %d %d%s%s (read_timeout=%.2fs)",
            port, baud_rate, bytesize, parity, stopbits, read_timeout,
        )

    def open(self) -> None:
        """Open the serial port.

        Raises:
            SerialCommunicationError: If the port cannot be opened.  The error
                message includes the OS-level reason, the port path, and
                platform-specific troubleshooting hints.
        """
        if self._serial is not None and self._serial.is_open:
            logger.debug("[SERIAL-OPEN] Port %s is already open — skipping", self.port)
            return

        logger.info(
            "[SERIAL-OPEN] Opening %s at %d baud ...", self.port, self.baud_rate,
        )

        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baud_rate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.read_timeout,
                write_timeout=self.read_timeout,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            logger.info("[SERIAL-OPEN] Successfully opened %s", self.port)

        except serial.SerialException as exc:
            hint = self._platform_hint()
            msg = (
                f"Failed to open serial port {self.port} at {self.baud_rate} baud: {exc}. "
                f"{hint}"
            )
            logger.error("[SERIAL-OPEN] FAILED — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            hint = self._platform_hint()
            msg = (
                f"OS error opening serial port {self.port}: {exc}. "
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
        self.open()
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
    def list_available_ports() -> list[str]:
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
            data = reader.flush_and_read(duration_ms=3000)
            print(f"Serial output: {data!r}")
    """

    def __init__(self, connection_manager: SerialConnectionManager) -> None:
        """Initialize serial reader.

        Args:
            connection_manager: An **open** ``SerialConnectionManager``.
        """
        self.connection_manager = connection_manager

    def flush(self) -> int:
        """Flush (discard) all data currently waiting in the serial receive buffer.

        Returns:
            Number of bytes that were discarded.

        Raises:
            SerialCommunicationError: If the port is not open or the flush fails.
        """
        port_name = self.connection_manager.port

        if not self.connection_manager.is_open():
            msg = (
                f"Cannot flush serial port {port_name}: port is not open. "
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
                    "[SERIAL-FLUSH] Discarded %d stale bytes from %s",
                    len(discarded), port_name,
                )
                # Also reset the OS-level buffers
                ser.reset_input_buffer()
                return len(discarded)

            ser.reset_input_buffer()
            logger.info("[SERIAL-FLUSH] Input buffer on %s was already empty", port_name)
            return 0

        except serial.SerialException as exc:
            msg = (
                f"Error flushing serial port {port_name}: {exc}. "
                f"The port may have been disconnected or the USB cable unplugged."
            )
            logger.error("[SERIAL-FLUSH] ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"OS error flushing serial port {port_name}: {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-FLUSH] OS ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

    def read_for_duration(
        self,
        duration_ms: int = SERIAL_DEFAULT_DURATION_MS,
        encoding: str = "utf-8",
    ) -> Tuple[str, int, float]:
        """Read from the serial port for exactly ``duration_ms`` milliseconds.

        Accumulates all bytes received during the time window, then decodes
        them as a single string.

        Args:
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
                f"Invalid read duration {duration_ms} ms for port {port_name}. "
                f"Duration must be a positive integer (e.g. 2000 for 2 seconds)."
            )

        if not self.connection_manager.is_open():
            msg = (
                f"Cannot read from serial port {port_name}: port is not open. "
                f"Did you forget to call open() or use a context manager?"
            )
            logger.error("[SERIAL-READ] %s", msg)
            raise SerialCommunicationError(msg)

        ser = self.connection_manager.get_serial()
        duration_s = duration_ms / 1000.0
        buffer = bytearray()

        logger.info(
            "[SERIAL-READ] Listening on %s for %d ms (%.2fs) ...",
            port_name, duration_ms, duration_s,
        )

        start_time = time.monotonic()
        deadline = start_time + duration_s
        read_cycles = 0

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                waiting = ser.in_waiting
                if waiting > 0:
                    chunk = ser.read(waiting)
                    buffer.extend(chunk)
                    logger.debug(
                        "[SERIAL-READ] +%d bytes from %s (total %d)",
                        len(chunk), port_name, len(buffer),
                    )
                else:
                    # Nothing waiting — sleep for a short interval to avoid
                    # busy-looping, but never longer than the remaining time.
                    sleep_time = min(self.connection_manager.read_timeout, remaining)
                    if sleep_time > 0:
                        time.sleep(sleep_time)

                read_cycles += 1

        except serial.SerialException as exc:
            elapsed = time.monotonic() - start_time
            msg = (
                f"Serial read error on {port_name} after {elapsed:.3f}s "
                f"({len(buffer)} bytes received so far): {exc}. "
                f"The device may have been disconnected during the read."
            )
            logger.error("[SERIAL-READ] ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            elapsed = time.monotonic() - start_time
            msg = (
                f"OS error reading from {port_name} after {elapsed:.3f}s "
                f"({len(buffer)} bytes received so far): {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-READ] OS ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

        elapsed = time.monotonic() - start_time
        bytes_received = len(buffer)

        logger.info(
            "[SERIAL-READ] Completed on %s — %d bytes in %.3fs (%d poll cycles)",
            port_name, bytes_received, elapsed, read_cycles,
        )

        if bytes_received == 0:
            msg = (
                f"No data received from serial port {port_name} during "
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

        try:
            decoded = buffer.decode(encoding, errors="replace")
        except (UnicodeDecodeError, LookupError) as exc:
            msg = (
                f"Failed to decode {bytes_received} bytes from {port_name} "
                f"using encoding {encoding!r}: {exc}. "
                f'Try encoding="latin-1" for raw 8-bit passthrough.'
            )
            logger.error("[SERIAL-READ] DECODE ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

        logger.info(
            "[SERIAL-READ] Decoded %d bytes → %d characters from %s",
            bytes_received, len(decoded), port_name,
        )

        return (decoded, bytes_received, elapsed)

    def flush_and_read(
        self,
        duration_ms: int = SERIAL_DEFAULT_DURATION_MS,
        encoding: str = "utf-8",
    ) -> str:
        """Convenience: flush stale data, then read for a duration.

        This is the primary method for the typical workflow:

        1. Discard anything sitting in the buffer from previous activity.
        2. Read fresh data arriving within the next ``duration_ms`` ms.
        3. Return the decoded string (only).

        Args:
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
            "[SERIAL-FLUSH+READ] Starting flush-then-read on %s (duration=%d ms) ...",
            port_name, duration_ms,
        )

        discarded = self.flush()
        if discarded > 0:
            logger.info(
                "[SERIAL-FLUSH+READ] Flushed %d stale bytes before reading on %s",
                discarded, port_name,
            )

        decoded, bytes_received, elapsed = self.read_for_duration(duration_ms, encoding)

        logger.info(
            "[SERIAL-FLUSH+READ] Done on %s — received %d bytes (%.3fs): %s",
            port_name, bytes_received, elapsed,
            decoded[:200] + ("..." if len(decoded) > 200 else ""),
        )

        return decoded

    def write_and_read(
        self,
        data: str,
        duration_ms: int = SERIAL_DEFAULT_DURATION_MS,
        encoding: str = "utf-8",
    ) -> str:
        """Flush, write a string to the serial port, then read the response.

        Useful when the serial device expects a command before it sends output
        (e.g. an AT-command interface or a U-Boot prompt).

        Args:
            data: The string to send over serial (a newline is **not**
                  appended automatically — include ``"\\n"`` if needed).
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
                f"Cannot write to serial port {port_name}: port is not open. "
                f"Did you forget to call open() or use a context manager?"
            )
            logger.error("[SERIAL-WRITE] %s", msg)
            raise SerialCommunicationError(msg)

        ser = self.connection_manager.get_serial()

        logger.info(
            "[SERIAL-WRITE+READ] Flushing, writing %d bytes to %s, "
            "then reading for %d ms ...",
            len(data), port_name, duration_ms,
        )

        self.flush()

        try:
            encoded = data.encode(encoding)
            bytes_written = ser.write(encoded)
            ser.flush()  # ensure all bytes are physically transmitted
            logger.info(
                "[SERIAL-WRITE] Wrote %d bytes to %s: %s",
                bytes_written, port_name,
                data[:100] + ("..." if len(data) > 100 else ""),
            )
        except serial.SerialException as exc:
            msg = (
                f"Failed to write to serial port {port_name}: {exc}. "
                f"The device may have been disconnected."
            )
            logger.error("[SERIAL-WRITE] ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc
        except OSError as exc:
            msg = (
                f"OS error writing to serial port {port_name}: {exc}. "
                f"The device may have been physically removed."
            )
            logger.error("[SERIAL-WRITE] OS ERROR — %s", msg)
            raise SerialCommunicationError(msg) from exc

        decoded, bytes_received, elapsed = self.read_for_duration(duration_ms, encoding)

        logger.info(
            "[SERIAL-WRITE+READ] Done on %s — wrote %d bytes, received %d bytes (%.3fs)",
            port_name, bytes_written, bytes_received, elapsed,
        )

        return decoded
