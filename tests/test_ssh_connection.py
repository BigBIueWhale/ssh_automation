"""
Comprehensive SSH connection test suite.

Uses real in-process SSH servers built on paramiko's ServerInterface — no
system sshd, no mocking, works on both Windows and Linux.

Two independent servers are started on OS-assigned ports so there are
never port conflicts, even in parallel CI.

Run with full visibility:
    pytest tests/test_ssh_connection.py -v -s
"""

from __future__ import annotations

import platform
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Dependency gate — report clearly if anything is missing
# ---------------------------------------------------------------------------
_MISSING: List[str] = []

try:
    import paramiko
except ImportError:
    _MISSING.append("paramiko")

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
        f"  The following packages are not installed: {', '.join(_MISSING)}\n"
        f"  Install them with:  pip install {' '.join(_MISSING)}\n"
        "=" * 72 + "\n",
        file=sys.stderr,
    )
    pytest.skip(
        f"Required libraries missing: {', '.join(_MISSING)}",
        allow_module_level=True,
    )

from linux_ssh_tools.connection import SSHConnectionManager, SSHCommandExecutor
from linux_ssh_tools.exceptions import SSHConnectionError, SSHTimeoutError

# ---------------------------------------------------------------------------
# Report environment
# ---------------------------------------------------------------------------
print(
    "\n"
    "+" * 72 + "\n"
    f"  Platform : {platform.system()} {platform.release()}\n"
    f"  Python   : {sys.version.split()[0]}\n"
    f"  paramiko : {paramiko.__version__}\n"
    "+" * 72
)

# ---------------------------------------------------------------------------
# Test-only credentials (used by the in-process SSH server, never real hosts)
# ---------------------------------------------------------------------------
TEST_HOST = "127.0.0.1"
TEST_USER = "testuser"
TEST_PASS = "testpass"
TEST_USERS = {TEST_USER: TEST_PASS}


# ═══════════════════════════════════════════════════════════════════════════
#  IN-PROCESS SSH SERVER  (paramiko ServerInterface)
# ═══════════════════════════════════════════════════════════════════════════

class _TestSSHServer(paramiko.ServerInterface):
    """Minimal SSH server that accepts password auth and runs commands."""

    def __init__(self, users: Dict[str, str]) -> None:
        self.users = users
        self.command_event = threading.Event()
        self._command: Optional[str] = None

    # -- authentication ----------------------------------------------------

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_auth_password(self, username: str, password: str) -> int:
        if self.users.get(username) == password:
            print(f"    [SERVER] Auth OK for user={username!r}")
            return paramiko.AUTH_SUCCESSFUL
        print(f"    [SERVER] Auth REJECTED for user={username!r}")
        return paramiko.AUTH_FAILED

    # -- channel handling --------------------------------------------------

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel: paramiko.Channel, command: bytes) -> bool:
        cmd_str = command.decode("utf-8") if isinstance(command, bytes) else command
        self._command = cmd_str
        print(f"    [SERVER] exec request: {cmd_str!r}")
        threading.Thread(
            target=self._run_command, args=(channel, cmd_str), daemon=True
        ).start()
        return True

    def check_channel_pty_request(
        self, channel: paramiko.Channel, term: bytes, width: int, height: int,
        pixelwidth: int, pixelheight: int, modes: bytes,
    ) -> bool:
        return True

    # -- command execution -------------------------------------------------

    @staticmethod
    def _run_command(channel: paramiko.Channel, command: str) -> None:
        """Execute *command* via the OS shell and pipe results back."""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, timeout=15,
            )
            if result.stdout:
                channel.sendall(result.stdout)
            if result.stderr:
                channel.sendall_stderr(result.stderr)
            channel.send_exit_status(result.returncode)
            print(
                f"    [SERVER] command finished — rc={result.returncode}, "
                f"stdout={len(result.stdout)}B, stderr={len(result.stderr)}B"
            )
        except subprocess.TimeoutExpired:
            channel.sendall_stderr(b"Command timed out\n")
            channel.send_exit_status(1)
            print("    [SERVER] command TIMED OUT")
        except Exception as exc:
            channel.sendall_stderr(f"Server error: {exc}\n".encode())
            channel.send_exit_status(1)
            print(f"    [SERVER] command ERROR: {exc}")
        finally:
            channel.close()


class SSHTestServer:
    """
    Manages a single in-process SSH server on an OS-assigned port.

    Usage:
        srv = SSHTestServer(users=TEST_USERS)
        srv.start()       # binds, listens, ready
        ...               # connect to 127.0.0.1:srv.port
        srv.stop()        # tears everything down
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        users: Optional[Dict[str, str]] = None,
    ) -> None:
        self.host = host
        self.users = users or dict(TEST_USERS)
        self.host_key = paramiko.RSAKey.generate(2048)
        self._server_socket: Optional[socket.socket] = None
        self._running = False
        self._accept_thread: Optional[threading.Thread] = None
        self._transports: List[paramiko.Transport] = []
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        if self._server_socket is None:
            raise RuntimeError("Server not started")
        return self._server_socket.getsockname()[1]

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.settimeout(1.0)
        self._server_socket.bind((self.host, 0))  # OS picks a free port
        self._server_socket.listen(5)
        self._running = True

        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True
        )
        self._accept_thread.start()
        # Wait until the socket is actually listening
        self._wait_ready()
        print(
            f"  [SERVER] STARTED on {self.host}:{self.port} "
            f"(users: {list(self.users.keys())})"
        )

    def stop(self) -> None:
        self._running = False

        # Close every active transport so client threads unblock
        with self._lock:
            for t in list(self._transports):
                try:
                    t.close()
                except Exception:
                    pass
            self._transports.clear()

        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None

        if self._accept_thread:
            self._accept_thread.join(timeout=5)
            self._accept_thread = None

        print(f"  [SERVER] STOPPED (was on port {getattr(self, '_last_port', '?')})")

    def _wait_ready(self, timeout: float = 5.0) -> None:
        """Block until the server accepts TCP connections."""
        self._last_port = self.port  # stash for the stop message
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(
            f"SSH test server on {self.host}:{self.port} failed to become ready "
            f"within {timeout}s"
        )

    # -- accept loop -------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client_sock, addr = self._server_socket.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client, args=(client_sock, addr), daemon=True
            ).start()

    def _handle_client(
        self, client_sock: socket.socket, addr: tuple  # type: ignore[type-arg]
    ) -> None:
        transport: Optional[paramiko.Transport] = None
        try:
            transport = paramiko.Transport(client_sock)
            transport.add_server_key(self.host_key)

            server_if = _TestSSHServer(users=self.users)
            transport.start_server(server=server_if)

            with self._lock:
                self._transports.append(transport)

            # Keep alive until client disconnects or server shuts down
            while self._running and transport.is_active():
                time.sleep(0.1)

        except Exception:
            pass
        finally:
            if transport:
                with self._lock:
                    if transport in self._transports:
                        self._transports.remove(transport)
                try:
                    transport.close()
                except Exception:
                    pass

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> SSHTestServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


class BlackHoleServer:
    """
    TCP server that accepts connections but never sends an SSH banner.
    This forces paramiko to hit its banner_timeout / socket timeout,
    which lets us verify timeout error handling.
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._held: List[socket.socket] = []
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        if self._sock is None:
            raise RuntimeError("BlackHoleServer not started")
        return self._sock.getsockname()[1]

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        self._sock.bind((self.host, 0))
        self._sock.listen(5)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        print(f"  [BLACKHOLE] STARTED on {self.host}:{self.port}")

    def stop(self) -> None:
        self._running = False
        for s in self._held:
            try:
                s.close()
            except Exception:
                pass
        self._held.clear()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        print(f"  [BLACKHOLE] STOPPED")

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()  # type: ignore[union-attr]
                self._held.append(conn)  # hold open, never respond
            except socket.timeout:
                continue
            except OSError:
                break

    def __enter__(self) -> BlackHoleServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


# ═══════════════════════════════════════════════════════════════════════════
#  FIXTURES
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def ssh_servers():
    """Spin up two independent SSH servers for the entire test module."""
    print("\n" + "=" * 72)
    print("  FIXTURE SETUP: Starting two in-process SSH servers ...")
    print("=" * 72)

    srv1 = SSHTestServer(users=TEST_USERS)
    srv2 = SSHTestServer(users=TEST_USERS)
    srv1.start()
    srv2.start()

    print(f"  Server 1 → 127.0.0.1:{srv1.port}")
    print(f"  Server 2 → 127.0.0.1:{srv2.port}")
    print("=" * 72 + "\n")

    yield srv1, srv2

    print("\n" + "=" * 72)
    print("  FIXTURE TEARDOWN: Stopping SSH servers ...")
    print("=" * 72)
    srv1.stop()
    srv2.stop()
    print("=" * 72 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _report(label: str, detail: str = "") -> None:
    """Uniform test-level print."""
    if detail:
        print(f"  [{label}] {detail}")
    else:
        print(f"  [{label}]")


def _report_result(rc: int, stdout: str, stderr: str) -> None:
    """Print command execution result in full."""
    print(f"    return_code = {rc}")
    print(f"    stdout      = {stdout.strip()!r}")
    if stderr.strip():
        print(f"    stderr      = {stderr.strip()!r}")


# ═══════════════════════════════════════════════════════════════════════════
#  TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestConnectionManagerCreation:
    """Test SSHConnectionManager object construction (no network)."""

    def test_custom_ports(self) -> None:
        _report("TEST", "Creating managers with custom ports 2222 and 2223")

        mgr1 = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS, port=2222,
        )
        mgr2 = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS, port=2223,
        )

        _report("ASSERT", f"manager1.port == 2222 → {mgr1.port}")
        assert mgr1.port == 2222
        _report("ASSERT", f"manager2.port == 2223 → {mgr2.port}")
        assert mgr2.port == 2223
        _report("PASS", "Custom port assignment verified")

    def test_default_state(self) -> None:
        _report("TEST", "Freshly created manager should NOT be connected")

        mgr = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
        )

        _report("ASSERT", f"is_connected() == False → {mgr.is_connected()}")
        assert not mgr.is_connected()
        _report("PASS", "Default state is disconnected")


class TestSuccessfulConnection:
    """Test connecting to both real test SSH servers."""

    def test_connect_server1(self, ssh_servers: tuple) -> None:
        srv1, _ = ssh_servers
        _report("TEST", f"Connecting to server 1 on port {srv1.port}")

        mgr = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        )
        try:
            mgr.connect()
            connected = mgr.is_connected()
            _report("ASSERT", f"is_connected() == True → {connected}")
            assert connected
            _report("PASS", f"Connected to server 1 (port {srv1.port})")
        finally:
            mgr.disconnect()
            _report("CLEANUP", f"Disconnected from server 1 (port {srv1.port})")

    def test_connect_server2(self, ssh_servers: tuple) -> None:
        _, srv2 = ssh_servers
        _report("TEST", f"Connecting to server 2 on port {srv2.port}")

        mgr = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv2.port,
        )
        try:
            mgr.connect()
            connected = mgr.is_connected()
            _report("ASSERT", f"is_connected() == True → {connected}")
            assert connected
            _report("PASS", f"Connected to server 2 (port {srv2.port})")
        finally:
            mgr.disconnect()
            _report("CLEANUP", f"Disconnected from server 2 (port {srv2.port})")


class TestContextManager:
    """Verify the with-statement connect/disconnect lifecycle."""

    def test_context_manager_server1(self, ssh_servers: tuple) -> None:
        srv1, _ = ssh_servers
        _report("TEST", f"Context-manager connect to server 1 (port {srv1.port})")

        with SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        ) as mgr:
            connected = mgr.is_connected()
            _report("ASSERT", f"Inside 'with': is_connected() == True → {connected}")
            assert connected

        _report("ASSERT", "After 'with': manager should be disconnected (client=None)")
        assert mgr.ssh_client is None
        _report("PASS", "Context manager properly connects and disconnects")

    def test_context_manager_server2(self, ssh_servers: tuple) -> None:
        _, srv2 = ssh_servers
        _report("TEST", f"Context-manager connect to server 2 (port {srv2.port})")

        with SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv2.port,
        ) as mgr:
            connected = mgr.is_connected()
            _report("ASSERT", f"Inside 'with': is_connected() == True → {connected}")
            assert connected

        assert mgr.ssh_client is None
        _report("PASS", "Context manager lifecycle OK on server 2")


class TestCommandExecution:
    """Execute real shell commands through the SSH channel."""

    def test_echo_server1(self, ssh_servers: tuple) -> None:
        srv1, _ = ssh_servers
        _report("TEST", f"Execute 'echo' on server 1 (port {srv1.port})")

        with SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        ) as mgr:
            executor = SSHCommandExecutor(mgr)
            rc, stdout, stderr = executor.execute_command("echo 'Hello from server 1'")
            _report("RESULT", "Command output:")
            _report_result(rc, stdout, stderr)

            _report("ASSERT", "return_code == 0")
            assert rc == 0
            _report("ASSERT", "'Hello from server 1' in stdout")
            assert "Hello from server 1" in stdout
        _report("PASS", "echo on server 1 succeeded")

    def test_echo_server2(self, ssh_servers: tuple) -> None:
        _, srv2 = ssh_servers
        _report("TEST", f"Execute 'echo' on server 2 (port {srv2.port})")

        with SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv2.port,
        ) as mgr:
            executor = SSHCommandExecutor(mgr)
            rc, stdout, stderr = executor.execute_command("echo 'Hello from server 2'")
            _report("RESULT", "Command output:")
            _report_result(rc, stdout, stderr)

            assert rc == 0
            assert "Hello from server 2" in stdout
        _report("PASS", "echo on server 2 succeeded")

    def test_nonzero_exit(self, ssh_servers: tuple) -> None:
        srv1, _ = ssh_servers
        _report("TEST", f"Execute command that exits non-zero on server 1 (port {srv1.port})")

        with SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        ) as mgr:
            executor = SSHCommandExecutor(mgr)
            rc, stdout, stderr = executor.execute_command("exit 42")
            _report("RESULT", "Command output:")
            _report_result(rc, stdout, stderr)

            _report("ASSERT", "return_code == 42")
            assert rc == 42
        _report("PASS", "Non-zero exit code correctly reported")


class TestMultipleConnections:
    """Simultaneous connections to both servers."""

    def test_dual_connection(self, ssh_servers: tuple) -> None:
        srv1, srv2 = ssh_servers
        _report("TEST", f"Simultaneous connections to ports {srv1.port} and {srv2.port}")

        mgr1 = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        )
        mgr2 = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv2.port,
        )

        try:
            mgr1.connect()
            mgr2.connect()

            _report("ASSERT", f"mgr1.is_connected() → {mgr1.is_connected()}")
            assert mgr1.is_connected()
            _report("ASSERT", f"mgr2.is_connected() → {mgr2.is_connected()}")
            assert mgr2.is_connected()

            exec1 = SSHCommandExecutor(mgr1)
            exec2 = SSHCommandExecutor(mgr2)

            rc1, out1, _ = exec1.execute_command("echo server1")
            rc2, out2, _ = exec2.execute_command("echo server2")

            _report("RESULT", f"Server 1: rc={rc1}, stdout={out1.strip()!r}")
            _report("RESULT", f"Server 2: rc={rc2}, stdout={out2.strip()!r}")

            assert rc1 == 0 and "server1" in out1
            assert rc2 == 0 and "server2" in out2
            _report("PASS", "Both connections work simultaneously")
        finally:
            mgr1.disconnect()
            mgr2.disconnect()
            _report("CLEANUP", "Both connections closed")


class TestErrorHandling:
    """Verify error paths: refused connections, timeouts, bad auth."""

    def test_connection_refused(self) -> None:
        """Port with nothing listening → SSHConnectionError."""
        _report("TEST", "Connecting to a port with nothing listening (should fail)")

        # Bind-then-close to guarantee an unused port
        tmp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tmp.bind((TEST_HOST, 0))
        dead_port = tmp.getsockname()[1]
        tmp.close()

        _report("DETAIL", f"Using guaranteed-dead port {dead_port}")

        mgr = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=dead_port, timeout=2,
        )

        with pytest.raises(SSHConnectionError) as exc_info:
            mgr.connect()

        _report("CAUGHT", f"{type(exc_info.value).__name__}: {exc_info.value}")
        _report("PASS", "SSHConnectionError raised for refused connection")

    def test_connection_timeout(self) -> None:
        """Server that accepts TCP but never sends SSH banner → timeout error."""
        _report("TEST", "Connecting to black-hole server (should timeout)")

        with BlackHoleServer() as bh:
            _report("DETAIL", f"Black-hole server on port {bh.port}")

            mgr = SSHConnectionManager(
                hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
                port=bh.port, timeout=2,
            )

            with pytest.raises(SSHConnectionError) as exc_info:
                mgr.connect()

            _report("CAUGHT", f"{type(exc_info.value).__name__}: {exc_info.value}")
            _report("PASS", "Connection error raised for unresponsive server")

    def test_bad_password(self, ssh_servers: tuple) -> None:
        """Wrong password → SSHConnectionError."""
        srv1, _ = ssh_servers
        _report("TEST", f"Connecting with wrong password to server 1 (port {srv1.port})")

        mgr = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password="WRONG",
            port=srv1.port, timeout=5,
        )

        with pytest.raises(SSHConnectionError) as exc_info:
            mgr.connect()

        _report("CAUGHT", f"{type(exc_info.value).__name__}: {exc_info.value}")
        _report("PASS", "SSHConnectionError raised for bad credentials")

    def test_execute_without_connection(self) -> None:
        """execute_command on a disconnected manager → SSHConnectionError."""
        _report("TEST", "Calling execute_command without connecting first")

        mgr = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
        )
        executor = SSHCommandExecutor(mgr)

        with pytest.raises(SSHConnectionError) as exc_info:
            executor.execute_command("echo should_not_run")

        _report("CAUGHT", f"{type(exc_info.value).__name__}: {exc_info.value}")
        _report("PASS", "Correctly refused to execute on disconnected manager")


class TestReconnection:
    """Connect → disconnect → reconnect cycle."""

    def test_reconnect(self, ssh_servers: tuple) -> None:
        srv1, _ = ssh_servers
        _report("TEST", f"Reconnection cycle on server 1 (port {srv1.port})")

        mgr = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        )

        try:
            # First connection
            mgr.connect()
            _report("STEP", f"1st connect: is_connected() = {mgr.is_connected()}")
            assert mgr.is_connected()

            # Disconnect
            mgr.disconnect()
            _report("STEP", f"disconnect:   is_connected() = {mgr.is_connected()}")
            assert not mgr.is_connected()

            # Reconnect
            mgr.connect()
            _report("STEP", f"reconnect:    is_connected() = {mgr.is_connected()}")
            assert mgr.is_connected()

            # Verify the reconnected session actually works
            executor = SSHCommandExecutor(mgr)
            rc, out, _ = executor.execute_command("echo reconnected")
            _report("RESULT", f"rc={rc}, stdout={out.strip()!r}")
            assert rc == 0
            assert "reconnected" in out
            _report("PASS", "Reconnection verified with successful command")
        finally:
            mgr.disconnect()
            _report("CLEANUP", "Final disconnect")


class TestRetryLogic:
    """Command execution with retry."""

    def test_successful_retry(self, ssh_servers: tuple) -> None:
        srv1, _ = ssh_servers
        _report("TEST", f"execute_with_retry on server 1 (port {srv1.port})")

        with SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        ) as mgr:
            executor = SSHCommandExecutor(mgr)
            rc, stdout, stderr = executor.execute_with_retry(
                "echo 'retry test'", max_retries=3,
            )
            _report("RESULT", "Command output:")
            _report_result(rc, stdout, stderr)

            assert rc == 0
            assert "retry test" in stdout
        _report("PASS", "execute_with_retry completed successfully")


class TestConcurrentCommands:
    """Execute a batch of commands sequentially on one connection."""

    def test_command_batch(self, ssh_servers: tuple) -> None:
        srv1, _ = ssh_servers
        _report("TEST", f"Running 3 sequential commands on server 1 (port {srv1.port})")

        with SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        ) as mgr:
            executor = SSHCommandExecutor(mgr)

            commands = [
                "echo 'Command 1'",
                "echo 'Command 2'",
                "echo 'Command 3'",
            ]

            for i, cmd in enumerate(commands, 1):
                rc, stdout, stderr = executor.execute_command(cmd)
                _report(f"CMD {i}", f"rc={rc}, stdout={stdout.strip()!r}")
                assert rc == 0
                assert f"Command {i}" in stdout

        _report("PASS", "All 3 sequential commands succeeded")


class TestDisconnectReporting:
    """Verify that disconnect is properly reported and idempotent."""

    def test_disconnect_reports(self, ssh_servers: tuple) -> None:
        srv1, _ = ssh_servers
        _report("TEST", f"Verify disconnect reporting on server 1 (port {srv1.port})")

        mgr = SSHConnectionManager(
            hostname=TEST_HOST, username=TEST_USER, password=TEST_PASS,
            port=srv1.port,
        )
        mgr.connect()
        _report("STEP", f"Connected: is_connected() = {mgr.is_connected()}")
        assert mgr.is_connected()

        # First disconnect
        mgr.disconnect()
        _report("STEP", f"After 1st disconnect: is_connected() = {mgr.is_connected()}")
        assert not mgr.is_connected()
        assert mgr.ssh_client is None

        # Second disconnect (idempotent — should not raise)
        mgr.disconnect()
        _report("STEP", "2nd disconnect: no error raised (idempotent)")
        _report("PASS", "Disconnect reported and is idempotent")


# ---------------------------------------------------------------------------
#  Entry point for running outside pytest
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
