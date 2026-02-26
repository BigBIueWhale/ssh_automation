# Linux SSH Tools

Robust Python library for programmatically controlling Linux devices over SSH
and serial from Windows 10 or Ubuntu 24.04.  Bypasses SSH fingerprint
verification for stateless hardware devices on internal networks.

## Install

```bash
pip install -e linux_ssh_tools/
```

## Use cases

Every example below is complete — copy-paste into a `.py` file and run.
Credentials are passed directly; for the preconfigured device list, set the
`SSH_DEVICE_*` / `SERIAL_DEVICE_*` environment variables described in
`linux_ssh_tools/src/linux_ssh_tools/__init__.py`.

---

### 1. Run a command via SSH and get the returned string

The call blocks until the remote command finishes (not based on a timeout).
The timeout is only a failsafe to prevent an infinite hang.

```python
from linux_ssh_tools.connection import SSHConnectionManager, SSHCommandExecutor

conn = SSHConnectionManager(
    hostname="192.168.1.100",
    username="user",
    password="password",
)

with conn:
    executor = SSHCommandExecutor(conn)

    # Blocks until `uname -a` exits, then returns stdout/stderr.
    # Raises SSHCommandError on non-zero exit code.
    rc, stdout, stderr = executor.execute_command(
        "uname -a",
        context="fetch kernel version",
    )
    print(stdout)
```

If the command fails (non-zero exit code) it raises automatically:

```python
from linux_ssh_tools.connection import SSHConnectionManager, SSHCommandExecutor
from linux_ssh_tools.exceptions import SSHCommandError

conn = SSHConnectionManager(
    hostname="192.168.1.100",
    username="user",
    password="password",
)

with conn:
    executor = SSHCommandExecutor(conn)
    try:
        rc, stdout, stderr = executor.execute_command(
            "grep PATTERN /nonexistent",
            context="search config",
        )
    except SSHCommandError as e:
        print(f"Command failed (exit {e.return_code}): {e.stderr}")
```

Commands with flags, pipes, and shell constructs work as expected — the
entire string is executed by the remote shell:

```python
from linux_ssh_tools.connection import SSHConnectionManager, SSHCommandExecutor

conn = SSHConnectionManager(
    hostname="192.168.1.100",
    username="user",
    password="password",
)

with conn:
    executor = SSHCommandExecutor(conn)

    # Flags and multiple arguments
    rc, stdout, stderr = executor.execute_command(
        "ls -la /tmp",
        context="list tmp directory",
    )
    print(stdout)

    # Pipes
    rc, stdout, stderr = executor.execute_command(
        "ps aux | grep nginx | head -5",
        context="find nginx processes",
    )
    print(stdout)

    # Chained commands (AND / OR)
    rc, stdout, stderr = executor.execute_command(
        "cd /opt/myapp && git pull && systemctl restart myapp",
        context="deploy latest",
    )
    print(stdout)

    # Environment variables and substitution
    rc, stdout, stderr = executor.execute_command(
        "LANG=C df -h / | tail -1",
        context="check disk usage",
    )
    print(stdout)
```

Retry on transient connection errors (command errors are never retried):

```python
from linux_ssh_tools.connection import SSHConnectionManager, SSHCommandExecutor

conn = SSHConnectionManager(
    hostname="192.168.1.100",
    username="user",
    password="password",
)

with conn:
    executor = SSHCommandExecutor(conn)
    rc, stdout, stderr = executor.execute_with_retry(
        "systemctl status myservice",
        context="check service health",
        max_retries=3,
        retry_delay=2.0,
    )
    print(stdout)
```

---

### 2. Copy files to/from a Linux machine (with transfer speed)

```python
from linux_ssh_tools.connection import SSHConnectionManager
from linux_ssh_tools.file_transfer import SSHFileTransfer

conn = SSHConnectionManager(
    hostname="192.168.1.100",
    username="user",
    password="password",
)

with conn:
    xfer = SSHFileTransfer(conn)

    # Upload — shows a tqdm progress bar with live speed
    bytes_up, speed_up = xfer.upload_file(
        "firmware.bin",
        "/tmp/firmware.bin",
        context="deploy firmware",
    )
    print(f"Uploaded {bytes_up} bytes at {xfer._format_speed(speed_up)}")

    # Download
    bytes_down, speed_down = xfer.download_file(
        "/var/log/syslog",
        "syslog_local.txt",
        context="retrieve syslog",
    )
    print(f"Downloaded {bytes_down} bytes at {xfer._format_speed(speed_down)}")

    # Upload an entire directory recursively
    xfer.upload_directory(
        "config_bundle/",
        "/etc/myapp/",
        context="deploy config bundle",
    )
```

---

### 3. Launch an interactive terminal window with an initial command

Opens a real terminal window (Windows Terminal / cmd on Windows,
gnome-terminal / xterm on Linux) with an SSH session.  The initial command
runs first, then the session stays open for the user to interact with.

```python
from linux_ssh_tools.connection import SSHConnectionManager
from linux_ssh_tools.terminal import SSHTerminalLauncher

conn = SSHConnectionManager(
    hostname="192.168.1.100",
    username="user",
    password="password",
)

launcher = SSHTerminalLauncher(conn)

# Opens a terminal, runs `top`, and after the user quits top
# they are left in an interactive shell.
process = launcher.launch_terminal(
    context="interactive debug session",
    initial_command="top",
    window_title="Device 1 — Debug",
)
```

The initial command can include flags, arguments, and pipes — the entire
string is passed to the remote shell:

```python
# Monitor logs with filters — terminal stays open after Ctrl-C
process = launcher.launch_terminal(
    context="filtered journal",
    initial_command="journalctl -f -u myservice --no-pager",
    window_title="Device 1 — Service Logs",
)

# Edit a config file interactively
process = launcher.launch_terminal(
    context="edit config",
    initial_command="vim /etc/myapp/config.yaml",
    window_title="Device 1 — Edit Config",
)

# Run a pipeline — the user sees live output in the terminal
process = launcher.launch_terminal(
    context="live network monitor",
    initial_command="tcpdump -i eth0 -n port 80 | head -50",
    window_title="Device 1 — Traffic",
)
```

Without an initial command it opens a plain interactive SSH session:

```python
process = launcher.launch_terminal(
    context="interactive shell",
    window_title="Device 1",
)
```

With automatic retry if the terminal fails to launch:

```python
process = launcher.launch_with_auto_reconnect(
    context="resilient terminal",
    initial_command="dmesg -w",
    window_title="Device 1 — Kernel Log",
    max_attempts=3,
    retry_delay=2.0,
)
```

---

### 4. Run an SSH command and capture the serial output it produces

The serial port must be **open before** the SSH command runs so the OS
receive buffer collects incoming bytes while the SSH call blocks.  No async
needed — the OS serial buffer handles the timing.  Use the separate
`flush()` and `read_for_duration()` methods (not `flush_and_read()`) so the
SSH command can run between the flush and the read.

```python
from linux_ssh_tools.connection import SSHConnectionManager, SSHCommandExecutor
from linux_ssh_tools.serial_comm import SerialConnectionManager, SerialReader

ssh = SSHConnectionManager(
    hostname="192.168.1.100",
    username="user",
    password="password",
)

# 1. Open the serial port FIRST — the OS starts buffering incoming bytes.
with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    reader = SerialReader(serial_mgr)

    # 2. Flush stale data so we only capture fresh output.
    reader.flush(context="clear before SSH trigger")

    # 3. Run the SSH command that produces serial output.
    #    This blocks until the remote command finishes.
    #    Any serial data that arrives during this time is held
    #    in the OS receive buffer.
    with ssh:
        executor = SSHCommandExecutor(ssh)
        executor.execute_command(
            "echo hello > /dev/ttyS0",
            context="trigger serial output",
        )

    # 4. Read the serial data that arrived (already in the OS buffer)
    #    plus anything that arrives in the next 3 seconds.
    text, nbytes, elapsed = reader.read_for_duration(
        context="capture serial response",
        duration_ms=3000,
    )
    print(f"Got {nbytes} bytes in {elapsed:.2f}s: {text!r}")
```

### 5. Serial: flush then read for N milliseconds

When you just want to capture whatever is coming out of the serial line
(no SSH trigger), use `flush_and_read()` for convenience, or the separate
methods for finer control.

```python
from linux_ssh_tools.serial_comm import SerialConnectionManager, SerialReader

with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    reader = SerialReader(serial_mgr)

    # One-liner: flush stale data, then read for 3 seconds
    data = reader.flush_and_read(
        context="capture boot log",
        duration_ms=3000,
    )
    print(f"Serial output: {data!r}")
```

Lower-level control (separate flush, read, write-and-read):

```python
from linux_ssh_tools.serial_comm import SerialConnectionManager, SerialReader

with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    reader = SerialReader(serial_mgr)

    # Flush stale data
    discarded = reader.flush(context="clear buffer")
    print(f"Discarded {discarded} stale bytes")

    # Read for a duration (returns decoded string, byte count, elapsed time)
    text, nbytes, elapsed = reader.read_for_duration(
        context="listen for boot log",
        duration_ms=5000,
    )
    print(f"Got {nbytes} bytes in {elapsed:.2f}s: {text!r}")

    # Write a string then read the response
    response = reader.write_and_read(
        "AT+INFO\r\n",
        context="query AT modem",
        duration_ms=2000,
    )
    print(f"Modem response: {response!r}")
```

Custom serial port settings (defaults: 115200 8N1, no flow control):

```python
from linux_ssh_tools.serial_comm import SerialConnectionManager, SerialReader

# Custom serial settings (defaults: 115200 8N1, write_timeout=10)
with SerialConnectionManager(
    "/dev/ttyUSB0",
    baud_rate=9600,
    bytesize=7,
    parity="E",
    stopbits=2,
    write_timeout=10,
) as serial_mgr:
    reader = SerialReader(serial_mgr)
    data = reader.flush_and_read(context="custom settings read", duration_ms=2000)
    print(data)
```

---

### 6. Serial: read until a condition is met

Read from the serial port until a pattern appears or the timeout expires,
without sending any command. Useful for waiting on asynchronous device output.

```python
from linux_ssh_tools.serial_comm import SerialConnectionManager, SerialReader

with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    reader = SerialReader(serial_mgr)

    # Wait until "ready" appears (or 10s timeout)
    text, nbytes, elapsed, matched = reader.read_until(
        "ready",
        context="wait for boot",
        timeout_ms=10000,
    )

    # With a callable condition
    text, nbytes, elapsed, matched = reader.read_until(
        lambda text: "login:" in text or "# " in text,
        context="wait for prompt",
        timeout_ms=30000,
    )
```

---

### 7. Serial: execute a command over the serial console

Sends ENTER (wake-up) → flushes → sends the command + ENTER → blocks while
reading the response.  Stopping is **not** based on timeout alone — you
provide a condition based on the content of the returned data, and the
timeout is only a failsafe.

```python
from linux_ssh_tools.serial_comm import (
    SerialConnectionManager,
    SerialCommandExecutor,
)

with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    executor = SerialCommandExecutor(serial_mgr)

    # Run 'ls /' and stop when the shell prompt reappears
    result = executor.execute_command(
        "ls /",
        context="list root filesystem",
        timeout_ms=10000,
        stop_condition=lambda text: "# " in text.rsplit("\n", 1)[-1],
    )

    print(result.output)
    print(f"Took {result.elapsed_seconds:.2f}s, "
          f"stopped by condition: {result.stopped_by_condition}")
```

With real-time streaming to the console:

```python
from linux_ssh_tools.serial_comm import (
    SerialConnectionManager,
    SerialCommandExecutor,
)

with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    executor = SerialCommandExecutor(serial_mgr)

    result = executor.execute_command(
        "dmesg",
        context="stream kernel log",
        timeout_ms=15000,
        stop_condition=lambda text: "# " in text.rsplit("\n", 1)[-1],
        on_data=lambda chunk: print(chunk, end="", flush=True),
    )
```

Works fine when no data comes back (returns empty output, does not raise):

```python
from linux_ssh_tools.serial_comm import (
    SerialConnectionManager,
    SerialCommandExecutor,
)

with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    executor = SerialCommandExecutor(serial_mgr)

    # No stop_condition → runs for the full timeout, returns normally
    result = executor.execute_command(
        "silent_command",
        context="fire and forget",
        timeout_ms=2000,
    )
    print(f"Got {result.bytes_received} bytes (may be 0)")
```

With multiple independent stop strings — stop as soon as **any** of them
appears in the output:

```python
from linux_ssh_tools.serial_comm import (
    SerialConnectionManager,
    SerialCommandExecutor,
)

STOP_STRINGS = ["# ", "ERROR", "PANIC", "login:"]

with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    executor = SerialCommandExecutor(serial_mgr)

    result = executor.execute_command(
        "reboot",
        context="reboot and wait for prompt",
        timeout_ms=60000,
        stop_condition=lambda text: any(s in text for s in STOP_STRINGS),
    )

    # Determine which string actually matched
    matched = next((s for s in STOP_STRINGS if s in result.output), None)
    print(f"Stopped on: {matched!r}")
    print(result.output)
```

When a stop condition is provided but never matches, `SerialTimeoutError` is
raised with the partial result attached:

```python
from linux_ssh_tools.serial_comm import (
    SerialConnectionManager,
    SerialCommandExecutor,
)
from linux_ssh_tools.exceptions import SerialTimeoutError

with SerialConnectionManager("/dev/ttyUSB0") as serial_mgr:
    executor = SerialCommandExecutor(serial_mgr)

    try:
        result = executor.execute_command(
            "long_running_command",
            context="wait for completion",
            timeout_ms=5000,
            stop_condition=lambda text: "DONE" in text,
        )
    except SerialTimeoutError as e:
        print(f"Timed out. Partial output: {e.result.output!r}")
```

---

### 8. Error handling

Every exception inherits from `LinuxSSHToolsError`, so you can catch
everything with a single base class or be specific:

```python
from linux_ssh_tools.exceptions import (
    LinuxSSHToolsError,      # catch-all base for the entire library
    SSHConnectionError,      # connection/network failures
    SSHTimeoutError,         # connection timeout (subclass of SSHConnectionError)
    SSHCommandError,         # non-zero exit code (NOT under SSHConnectionError)
    FileTransferError,       # upload/download failures
    TerminalLaunchError,     # terminal window failed to open
    SerialCommunicationError,  # serial I/O errors
    SerialTimeoutError,      # serial timeout (subclass of SerialCommunicationError)
)

# Catch-all for any library error
try:
    ...
except LinuxSSHToolsError as e:
    print(f"Something went wrong: {e}")

# Targeted handling — SSHCommandError is a sibling of SSHConnectionError,
# NOT a subclass.  This matters for retry logic: you retry connection
# failures but not command failures.
try:
    rc, stdout, stderr = executor.execute_command(
        "apt-get update",
        context="update packages",
    )
except SSHCommandError as e:
    print(f"Command {e.command!r} exited {e.return_code}")
    print(f"stderr: {e.stderr}")
except SSHConnectionError as e:
    print(f"Connection problem: {e}")
```

Every public method requires a `context: str` parameter.  The context string
is embedded into all error messages as `[context]` so you always know
*which operation* failed and *why it was being performed*.

---

### 9. List available serial ports

```python
from linux_ssh_tools.serial_comm import SerialConnectionManager

ports = SerialConnectionManager.list_available_ports()
for port in ports:
    print(port)
```

---

### 10. Troubleshooting unreliable serial at 115200 baud

If you're losing lines or seeing garbled data at 115200 — especially on
Windows — try these settings in order.  Each example is independent; combine
flags as needed.

**Start here — enlarge the OS receive buffer and poll faster:**

```python
with SerialConnectionManager(
    "COM3",
    baud_rate=115200,
    rx_buffer_size=65536,    # Windows default is often only 4 KB
    poll_interval_s=0.002,   # drain buffer every 2 ms instead of 10 ms
) as serial_mgr:
    reader = SerialReader(serial_mgr)
    text, nbytes, elapsed = reader.read_for_duration(
        context="capture response", duration_ms=3000,
    )
```

This doesn't alter the electrical behaviour of the link — it just gives
the OS more room to absorb bursts and makes Python drain that room faster.
Try this first.

**If you have a full cable (TX, RX, RTS, CTS, GND) — enable hardware flow
control:**

```python
with SerialConnectionManager(
    "COM3",
    baud_rate=115200,
    rtscts=True,             # hardware RTS/CTS flow control
    rx_buffer_size=65536,
    poll_interval_s=0.002,
) as serial_mgr:
    ...
```

RTS/CTS lets the receiver tell the sender "stop, my buffer is full".  This
is the most reliable option when wiring supports it.  **Warning:** if your
cable only has three wires (TX, RX, GND), enabling `rtscts=True` will hang
on write — pyserial waits for CTS to assert and it never does.  If writes
block forever, this is why.  Disable it and try something else.

**If you only have three wires (TX, RX, GND) — try software flow control:**

```python
with SerialConnectionManager(
    "COM3",
    baud_rate=115200,
    xonxoff=True,            # software XON/XOFF flow control
    rx_buffer_size=65536,
    poll_interval_s=0.002,
) as serial_mgr:
    ...
```

XON/XOFF works over a 3-pin cable because it sends control characters
in-band (no extra wires).  The catch: your target firmware must also support
it.  If the target ignores XON/XOFF, this has no effect.  If your data
stream contains raw bytes 0x11 or 0x13, enabling this will corrupt data
because those are the XON/XOFF characters.

**If you're seeing garbled data (not just lost lines) — try 2 stop bits:**

```python
with SerialConnectionManager(
    "COM3",
    baud_rate=115200,
    stopbits=2,              # extra bit time between frames
    rx_buffer_size=65536,
    poll_interval_s=0.002,
) as serial_mgr:
    ...
```

Two stop bits add timing margin between frames.  This helps when the
transmitter's clock is slightly off (common with cheap oscillators on
embedded boards).  The target does not need to match — a receiver configured
for 2 stop bits will happily receive from a sender using 1.

**Keep the serial port open across multiple reads:**

Opening and closing the port between reads can cause data loss — the OS
drops any buffered bytes on close, and the re-open may toggle DTR/RTS which
can reset or confuse some targets.  If your workflow involves multiple SSH
triggers with serial captures, keep the connection open for the whole
session:

```python
with SerialConnectionManager("COM3", rx_buffer_size=65536) as serial_mgr:
    reader = SerialReader(serial_mgr)

    for cmd in ["test1", "test2", "test3"]:
        reader.flush(context=f"clear before {cmd}")
        ssh_executor.execute_command(cmd, context=f"run {cmd}")
        text, nbytes, elapsed = reader.read_for_duration(
            context=f"capture {cmd}", duration_ms=3000,
        )
        print(text)
```

This is especially important with `rtscts=True` — some targets only
negotiate flow control on the initial connection.

---

## CLI

The package also installs a `linux-ssh` command-line tool:

```bash
# Run a command on device 0
linux-ssh exec "df -h"

# Upload a file
linux-ssh upload firmware.bin /tmp/firmware.bin

# Download a file
linux-ssh download /var/log/syslog ./syslog.txt

# Open an interactive terminal
linux-ssh terminal --command "top" --title "Debug"

# Read from serial for 3 seconds
linux-ssh serial-read --duration 3000 --serial-port /dev/ttyUSB0

# Execute a command over serial, stop when prompt appears
linux-ssh serial-exec "ls /" --stop-on "# " --stream --timeout 10000

# Serial with custom line settings
linux-ssh serial-read --serial-port /dev/ttyUSB0 --baud-rate 9600 --parity E
linux-ssh serial-exec "AT" --serial-port COM3 --bytesize 7 --stopbits 2
linux-ssh serial-exec "AT" --serial-port COM3 --write-timeout 5

# Serial reliability tuning (see section 10)
linux-ssh serial-read --serial-port COM3 --rx-buffer-size 65536 --poll-interval 0.002
linux-ssh serial-exec "ls /" --serial-port COM3 --rtscts --rx-buffer-size 65536
linux-ssh serial-exec "ls /" --serial-port COM3 --xonxoff
linux-ssh serial-exec "ls /" --serial-port COM3 --stopbits 2

# List serial ports
linux-ssh serial-list
```

---

## Security warning

This library **bypasses SSH fingerprint verification** and is designed for
**stateless hardware devices on trusted internal networks only**.  Do not use
it over untrusted networks.
