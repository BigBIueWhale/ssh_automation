"""Command-line interface for Linux SSH tools."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import DEFAULT_LINUX_DEVICES
from .connection import SSHConnectionManager, SSHCommandExecutor
from .file_transfer import SSHFileTransfer
from .terminal import SSHTerminalLauncher


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
    
    try:
        with connection_manager:
            executor = SSHCommandExecutor(connection_manager)
            return_code, stdout, stderr = executor.execute_command(args.command)
            
            print(f"Return code: {return_code}")
            if stdout:
                print(f"STDOUT:\n{stdout}")
            if stderr:
                print(f"STDERR:\n{stderr}", file=sys.stderr)
            
            return 0 if return_code == 0 else 1
            
    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


def command_upload(args) -> int:
    """Upload file to remote device."""
    connection_manager = create_connection_manager(args.device, args.port)
    
    try:
        with connection_manager:
            file_transfer = SSHFileTransfer(connection_manager)
            bytes_transferred, speed = file_transfer.upload_file(
                args.local_path, args.remote_path
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
    
    try:
        with connection_manager:
            file_transfer = SSHFileTransfer(connection_manager)
            bytes_transferred, speed = file_transfer.download_file(
                args.remote_path, args.local_path
            )
            
            print(
                f"Downloaded {bytes_transferred} bytes at "
                f"{file_transfer._format_speed(speed)}"
            )
            return 0
            
    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


def command_terminal(args) -> int:
    """Launch interactive terminal."""
    connection_manager = create_connection_manager(args.device, args.port)
    
    try:
        launcher = SSHTerminalLauncher(connection_manager)
        process = launcher.launch_terminal(
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
    
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
