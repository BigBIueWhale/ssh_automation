"""File transfer utilities with speed monitoring and verification."""

from __future__ import annotations

import os
import posixpath
import time
from typing import Optional, Tuple

import paramiko
from tqdm import tqdm
from typeguard import typechecked

from . import CHUNK_SIZE, PROGRESS_UPDATE_INTERVAL
from .connection import SSHConnectionManager
from .exceptions import SSHConnectionError, FileTransferError


@typechecked
class SSHFileTransfer:
    """Handles robust file transfers to Linux devices with speed monitoring."""

    def __init__(self, connection_manager: SSHConnectionManager) -> None:
        """Initialize file transfer handler.

        Args:
            connection_manager: Active SSH connection manager
        """
        self.connection_manager = connection_manager

    def _get_sftp_client(self) -> paramiko.SFTPClient:
        """Get or create SFTP client."""
        client = self.connection_manager._get_ssh_client()
        return client.open_sftp()

    def _calculate_transfer_speed(
        self, start_time: float, bytes_transferred: int
    ) -> float:
        """Calculate transfer speed in bytes per second."""
        elapsed = time.time() - start_time
        return bytes_transferred / elapsed if elapsed > 0 else 0

    def _format_speed(self, speed: float) -> str:
        """Format speed for display."""
        if speed < 1024:
            return f"{speed:.2f} B/s"
        elif speed < 1024 * 1024:
            return f"{speed / 1024:.2f} KB/s"
        else:
            return f"{speed / (1024 * 1024):.2f} MB/s"

    def _validate_local_file(self, local_path: str, context: str) -> None:
        """Validate that local file exists and is readable."""
        if not os.path.exists(local_path):
            raise FileTransferError(f"[{context}] Local file does not exist: {local_path}")

        if not os.path.isfile(local_path):
            raise FileTransferError(f"[{context}] Path is not a file: {local_path}")

        if not os.access(local_path, os.R_OK):
            raise FileTransferError(f"[{context}] Cannot read file: {local_path}")

    def _validate_remote_path(self, remote_path: str) -> None:
        """Validate remote path is writable."""
        # We'll check this during the actual transfer
        pass

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        context: str,
        show_progress: bool = True,
        chunk_size: int = CHUNK_SIZE,
    ) -> Tuple[int, float]:
        """Upload file to Linux device with progress monitoring.

        Args:
            local_path: Local file path
            remote_path: Remote destination path
            context: Description of the purpose, embedded into error messages.
            show_progress: Whether to display progress bar
            chunk_size: Chunk size for reading

        Returns:
            Tuple of (bytes_transferred, transfer_speed)

        Raises:
            FileTransferError: If file transfer fails
            SSHConnectionError: If connection is not active
        """
        if not self.connection_manager.is_connected():
            raise SSHConnectionError(
                f"[{context}] Cannot upload file: not connected to {self.connection_manager.hostname}"
            )

        self._validate_local_file(local_path, context)

        file_size = os.path.getsize(local_path)
        start_time = time.time()
        bytes_transferred = 0

        try:
            sftp = self._get_sftp_client()

            with open(local_path, "rb") as local_file:
                with sftp.open(remote_path, "wb") as remote_file:
                    if show_progress:
                        progress_bar = tqdm(
                            total=file_size,
                            unit="B",
                            unit_scale=True,
                            unit_divisor=1024,
                            desc=f"Uploading to {self.connection_manager.hostname}",
                        )

                    while bytes_transferred < file_size:
                        chunk = local_file.read(chunk_size)
                        if not chunk:
                            break

                        remote_file.write(chunk)
                        bytes_transferred += len(chunk)

                        if show_progress:
                            progress_bar.update(len(chunk))
                            progress_bar.set_postfix(
                                {"speed": self._format_speed(
                                    self._calculate_transfer_speed(start_time, bytes_transferred)
                                )}
                            )

                    if show_progress:
                        progress_bar.close()

            transfer_speed = self._calculate_transfer_speed(start_time, bytes_transferred)

            return (bytes_transferred, transfer_speed)

        except paramiko.SFTPError as e:
            raise FileTransferError(
                f"[{context}] SFTP error uploading {local_path} to {remote_path}: {str(e)}"
            ) from e
        except IOError as e:
            raise FileTransferError(
                f"[{context}] IO error uploading {local_path} to {remote_path}: {str(e)}"
            ) from e
        except Exception as e:
            raise FileTransferError(
                f"[{context}] Unexpected error uploading {local_path} to {remote_path}: {str(e)}"
            ) from e

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        context: str,
        show_progress: bool = True,
        chunk_size: int = CHUNK_SIZE,
    ) -> Tuple[int, float]:
        """Download file from Linux device with progress monitoring.

        Args:
            remote_path: Remote file path
            local_path: Local destination path
            context: Description of the purpose, embedded into error messages.
            show_progress: Whether to display progress bar
            chunk_size: Chunk size for reading

        Returns:
            Tuple of (bytes_transferred, transfer_speed)

        Raises:
            FileTransferError: If file transfer fails
            SSHConnectionError: If connection is not active
        """
        if not self.connection_manager.is_connected():
            raise SSHConnectionError(
                f"[{context}] Cannot download file: not connected to {self.connection_manager.hostname}"
            )

        # Ensure local directory exists
        local_dir = os.path.dirname(local_path)
        if local_dir and not os.path.exists(local_dir):
            os.makedirs(local_dir, exist_ok=True)

        start_time = time.time()
        bytes_transferred = 0

        try:
            sftp = self._get_sftp_client()

            # Get remote file size
            remote_stat = sftp.stat(remote_path)
            file_size = remote_stat.st_size

            with sftp.open(remote_path, "rb") as remote_file:
                with open(local_path, "wb") as local_file:
                    if show_progress:
                        progress_bar = tqdm(
                            total=file_size,
                            unit="B",
                            unit_scale=True,
                            unit_divisor=1024,
                            desc=f"Downloading from {self.connection_manager.hostname}",
                        )

                    while bytes_transferred < file_size:
                        chunk = remote_file.read(chunk_size)
                        if not chunk:
                            break

                        local_file.write(chunk)
                        bytes_transferred += len(chunk)

                        if show_progress:
                            progress_bar.update(len(chunk))
                            progress_bar.set_postfix(
                                {"speed": self._format_speed(
                                    self._calculate_transfer_speed(start_time, bytes_transferred)
                                )}
                            )

                    if show_progress:
                        progress_bar.close()

            transfer_speed = self._calculate_transfer_speed(start_time, bytes_transferred)

            return (bytes_transferred, transfer_speed)

        except paramiko.SFTPError as e:
            raise FileTransferError(
                f"[{context}] SFTP error downloading {remote_path} to {local_path}: {str(e)}"
            ) from e
        except IOError as e:
            raise FileTransferError(
                f"[{context}] IO error downloading {remote_path} to {local_path}: {str(e)}"
            ) from e
        except Exception as e:
            raise FileTransferError(
                f"[{context}] Unexpected error downloading {remote_path} to {local_path}: {str(e)}"
            ) from e

    def upload_directory(
        self,
        local_dir: str,
        remote_dir: str,
        context: str,
        show_progress: bool = True,
    ) -> None:
        """Upload entire directory recursively.

        Args:
            local_dir: Local directory path
            remote_dir: Remote directory path
            context: Description of the purpose, embedded into error messages.
            show_progress: Whether to display progress

        Raises:
            FileTransferError: If directory upload fails
        """
        if not os.path.isdir(local_dir):
            raise FileTransferError(f"[{context}] Local path is not a directory: {local_dir}")

        try:
            sftp = self._get_sftp_client()

            # Create remote directory if it doesn't exist
            try:
                sftp.mkdir(remote_dir)
            except IOError:
                # Directory already exists
                pass

            for root, dirs, files in os.walk(local_dir):
                # Compute the relative path from the local base, then join
                # with the remote base using POSIX separators (remote is Linux).
                rel = os.path.relpath(root, local_dir)
                if rel == ".":
                    remote_root = remote_dir
                else:
                    remote_root = posixpath.join(remote_dir, rel.replace(os.sep, "/"))

                for dir_name in dirs:
                    try:
                        sftp.mkdir(posixpath.join(remote_root, dir_name))
                    except IOError:
                        # Directory already exists
                        pass

                for file_name in files:
                    local_file = os.path.join(root, file_name)
                    remote_file = posixpath.join(remote_root, file_name)
                    self.upload_file(local_file, remote_file, context, show_progress)

        except FileTransferError:
            raise
        except Exception as e:
            raise FileTransferError(
                f"[{context}] Error uploading directory {local_dir} to {remote_dir}: {str(e)}"
            ) from e
