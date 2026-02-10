"""Command-line interface for Linux SSH tools."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import DEFAULT_LINUX_DEVICES, DEFAULT_SERIAL_DEVICES
from .connection import SSHConnectionManager, SSHCommandExecutor
from .exceptions import SSHCommandError, SerialTimeoutError
from .file_transfer import SSHFileTransfer
from .terminal import SSHTerminalLauncher
from .serial_comm import SerialConnectionManager, SerialReader, SerialCommandExecutor


def create_connection_manager(device_index: int = 0, port: int = 22) -> SSHConnectionManager:
    """Create connection manager for specified device."""
    if device_index < 0 or device_index >= len(DEFAULT_LINUX_DEVICES):
        raise ValueError(f"Invalid device index. Must be 0-{len(DEFAULT_LINUX_DEVICES)-1}")

    device = DEFAULT_LINUX_DEVICES[device_index]
    return SSHConnectionManager(
        hostname=device["hostname"],
        username=device["username"],
        password=device["password"],
        port=port,
    )


def command_execute(args) -> int:
    """Execute command on remote device."""
    connection_manager = create_connection_manager(args.device, args.port)
    device = DEFAULT_LINUX_DEVICES[args.device]
    ctx = f"CLI exec on {device['hostname']}:{args.port}"

    try:
        with connection_manager:
            executor = SSHCommandExecutor(connection_manager)
            return_code, stdout, stderr = executor.execute_command(
                args.command, context=ctx,
            )

            print(f"Return code: {return_code}")
            if stdout:
                print(f"STDOUT:\n{stdout}")
            if stderr:
                print(f"STDERR:\n{stderr}", file=sys.stderr)

            return 0 if return_code == 0 else 1

    except SSHCommandError as e:
        print(f"Command failed (exit {e.return_code}): {e}", file=sys.stderr)
        if e.stdout:
            print(f"STDOUT:\n{e.stdout}")
        if e.stderr:
            print(f"STDERR:\n{e.stderr}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


def command_upload(args) -> int:
    """Upload file to remote device."""
    connection_manager = create_connection_manager(args.device, args.port)
    device = DEFAULT_LINUX_DEVICES[args.device]
    ctx = f"CLI upload to {device['hostname']}:{args.port}"

    try:
        with connection_manager:
            file_transfer = SSHFileTransfer(connection_manager)
            bytes_transferred, speed = file_transfer.upload_file(
                args.local_path, args.remote_path, context=ctx,
            )

            print(
                f"Uploaded {bytes_transferred} bytes at "
                f"{file_transfer._format_speed(speed)}"
            )
            return 0

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


def command_download(args) -> int:
    """Download file from remote device."""
    connection_manager = create_connection_manager(args.device, args.port)
    device = DEFAULT_LINUX_DEVICES[args.device]
    ctx = f"CLI download from {device['hostname']}:{args.port}"

    try:
        with connection_manager:
            file_transfer = SSHFileTransfer(connection_manager)
            bytes_transferred, speed = file_transfer.download_file(
                args.remote_path, args.local_path, context=ctx,
            )

            print(
                f"Downloaded {bytes_transferred} bytes at "
                f"{file_transfer._format_speed(speed)}"
            )
            return 0

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


def command_serial_read(args) -> int:
    """Read from serial port after flushing."""
    serial_device = DEFAULT_SERIAL_DEVICES[args.device]
    port_key = "port2" if args.line == 2 else "port"
    port = args.serial_port or serial_device.get(port_key, "")

    if not port:
        print(
            f"Error: No serial port configured for device {args.device}, "
            f"line {args.line}. Set SERIAL_DEVICE_{args.device}_PORT"
            f"{'2' if args.line == 2 else ''} or pass --serial-port.",
            file=sys.stderr,
        )
        return 1

    ctx = f"CLI serial-read on {port}"

    try:
        with SerialConnectionManager(
            port=port,
            baud_rate=args.baud_rate,
        ) as mgr:
            reader = SerialReader(mgr)
            data = reader.flush_and_read(context=ctx, duration_ms=args.duration)

            print(data, end="")
            return 0

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


def command_serial_exec(args) -> int:
    """Execute a command over the serial console."""
    serial_device = DEFAULT_SERIAL_DEVICES[args.device]
    port_key = "port2" if args.line == 2 else "port"
    port = args.serial_port or serial_device.get(port_key, "")

    if not port:
        print(
            f"Error: No serial port configured for device {args.device}, "
            f"line {args.line}. Set SERIAL_DEVICE_{args.device}_PORT"
            f"{'2' if args.line == 2 else ''} or pass --serial-port.",
            file=sys.stderr,
        )
        return 1

    # Build optional stop condition from --stop-on string
    stop_condition = None
    if args.stop_on:
        stop_text = args.stop_on
        stop_condition = lambda text: stop_text in text  # noqa: E731

    ctx = f"CLI serial-exec on {port}"

    try:
        with SerialConnectionManager(
            port=port,
            baud_rate=args.baud_rate,
        ) as mgr:
            executor = SerialCommandExecutor(mgr)
            result = executor.execute_command(
                command=args.serial_command,
                context=ctx,
                timeout_ms=args.timeout,
                stop_condition=stop_condition,
                on_data=lambda chunk: print(chunk, end="", flush=True) if args.stream else None,
            )

            if not args.stream:
                print(result.output, end="")

            return 0

    except SerialTimeoutError as e:
        if e.result is not None:
            if not args.stream:
                print(e.result.output, end="")
            print(
                f"\n[timed out after {e.result.elapsed_seconds:.1f}s "
                f"without matching --stop-on {args.stop_on!r}]",
                file=sys.stderr,
            )
        else:
            print(f"Error: {str(e)}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


def command_serial_list(args) -> int:
    """List available serial ports."""
    ports = SerialConnectionManager.list_available_ports()
    if not ports:
        print("No serial ports found.")
    else:
        print("Available serial ports:")
        for p in ports:
            print(f"  {p}")
    return 0


def command_terminal(args) -> int:
    """Launch interactive terminal."""
    connection_manager = create_connection_manager(args.device, args.port)
    device = DEFAULT_LINUX_DEVICES[args.device]
    ctx = f"CLI terminal to {device['hostname']}:{args.port}"

    try:
        launcher = SSHTerminalLauncher(connection_manager)
        process = launcher.launch_terminal(
            context=ctx,
            initial_command=args.command,
            window_title=args.title,
        )
        return 0

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


def main() -> int:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Linux SSH Tools - Manage Linux devices from Windows"
    )

    # Device selection
    parser.add_argument(
        "--device",
        type=int,
        default=0,
        help="Device index (0 or 1)",
    )

    # SSH port
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port (default: 22)",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Execute command
    exec_parser = subparsers.add_parser("exec", help="Execute command")
    exec_parser.add_argument("command", help="Command to execute")
    exec_parser.set_defaults(func=command_execute)

    # Upload file
    upload_parser = subparsers.add_parser("upload", help="Upload file")
    upload_parser.add_argument("local_path", help="Local file path")
    upload_parser.add_argument("remote_path", help="Remote destination path")
    upload_parser.set_defaults(func=command_upload)

    # Download file
    download_parser = subparsers.add_parser("download", help="Download file")
    download_parser.add_argument("remote_path", help="Remote file path")
    download_parser.add_argument("local_path", help="Local destination path")
    download_parser.set_defaults(func=command_download)

    # Launch terminal
    term_parser = subparsers.add_parser("terminal", help="Launch terminal")
    term_parser.add_argument("--command", help="Initial command to run")
    term_parser.add_argument("--title", help="Window title")
    term_parser.set_defaults(func=command_terminal)

    # Serial read
    serial_parser = subparsers.add_parser(
        "serial-read", help="Flush serial buffer and read for a duration",
    )
    serial_parser.add_argument(
        "--duration", type=int, default=2000,
        help="Read duration in milliseconds (default: 2000)",
    )
    serial_parser.add_argument(
        "--baud-rate", type=int, default=115200,
        help="Baud rate (default: 115200)",
    )
    serial_parser.add_argument(
        "--serial-port", type=str, default=None,
        help="Serial port path (e.g. /dev/ttyUSB0 or COM3). "
             "Overrides the device config.",
    )
    serial_parser.add_argument(
        "--line", type=int, default=1, choices=[1, 2],
        help="Serial line number per device (1 or 2, default: 1)",
    )
    serial_parser.set_defaults(func=command_serial_read)

    # Serial exec
    serial_exec_parser = subparsers.add_parser(
        "serial-exec",
        help="Execute a command over the serial console (ENTER, command, ENTER)",
    )
    serial_exec_parser.add_argument(
        "serial_command", metavar="COMMAND",
        help="Command to send over the serial line",
    )
    serial_exec_parser.add_argument(
        "--timeout", type=int, default=30000,
        help="Hard timeout in milliseconds (default: 30000)",
    )
    serial_exec_parser.add_argument(
        "--stop-on", type=str, default=None,
        help="Stop reading when this string appears in the output "
             "(e.g. '# ' for a shell prompt)",
    )
    serial_exec_parser.add_argument(
        "--stream", action="store_true", default=False,
        help="Stream output to stdout as it arrives",
    )
    serial_exec_parser.add_argument(
        "--baud-rate", type=int, default=115200,
        help="Baud rate (default: 115200)",
    )
    serial_exec_parser.add_argument(
        "--serial-port", type=str, default=None,
        help="Serial port path (e.g. /dev/ttyUSB0 or COM3). "
             "Overrides the device config.",
    )
    serial_exec_parser.add_argument(
        "--line", type=int, default=1, choices=[1, 2],
        help="Serial line number per device (1 or 2, default: 1)",
    )
    serial_exec_parser.set_defaults(func=command_serial_exec)

    # Serial list
    serial_list_parser = subparsers.add_parser(
        "serial-list", help="List available serial ports",
    )
    serial_list_parser.set_defaults(func=command_serial_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
