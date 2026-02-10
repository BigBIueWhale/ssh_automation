# Linux SSH Tools

## Overview

Please write a robust Python script for Windows that I will run on a Windows 10 PC that is connected via ethernet to two Linux boxes with ssh server enabled where username is hard-coded "user" and password hard-coded "password". This script will use paramiko and whatever other capabilities to provide an ability to robustly copy files into a linux machine. On this Windows machine also "ssh" is available, but the fingerprints and stuff need to be cleared before using. In general, the python script should not trust the environment. Anything unexpected should become a detailed error, not fallback. The idea is that within this script there needs to be also capability to run commands on the linux machine and get response output. Kind of like a dedicated connection run running an individual command on the remote machine and getting its output programmatically. Obviously any error should include the full command and ssh response. You **must** ignore the whole certificate fingerprint security mechanism- we're talking about stateless hardware devices on an internal network. The idea is that in addition to being able to robustly and verifiably copying-in files (while printing transfer speed), and in addition to running commands programmaticaly in dedicated SSH connection (with robust cleanup, and response), the script should also have a tool to launch a terminal for the Windows PC user to see! A window that's launched where the user can actually interact with the SSH session, but within this window you should somehow be able to run a command. Kind of like launching a cmd window with ssh auto approve (clearing the certificates in the current user folder first), and auto run a command within the opened ssh window but in a way that the user will actually monitor and interact with and good window title (appropriate). Please create python structure that provides all these facilities in high code quality, opinionated manner, checking and validating everything that can be checked, and set up in a way with a nice interface that the final actual script logic will just be a few lines, for whatever I'll ask you to do in terms off automation regarding these two linux boxes. The python should be statically typed, and created in a directory structure that allows the functionality to be modular and import-based. Go!

Add windows 10 / Ubuntu 24.04 functionality of serial communication. Imagine that these linux boxes also each have 1 or 2 serial lines, so add a "library" in the same style and engineering robustness that allows me to get a string from the serial communication (and obviously to flush beforehand) for a duration of x milliseconds. This way I can, for example- run a command via SSH and read a "response" that comes over the serial communication line. Should use by default 115200 and standard settings. Good error handling and very verbose error messages.

Add ability to run command via serial communication- enter then write command then ENTER. And importantly it should be possible to block while waiting for that operation to complete, but do offer ability to stream any bytes received on that serial line during the command execution (if any) and don't base the stopping on timeout only. Should work fine also if no data is returned on serial line, and ability to set a condition for stopping based on contents of returned data. The cleanup mechanism in this operation and the flushing (write and read) before this operation should be immeculate.

## Strategic Advantages

This implementation delivers **operational robustness** through:

### 1. **SSH Connection Management**
- **Automatic fingerprint bypass** for internal networks eliminates manual approval bottlenecks
- **Connection timeout handling** (30 seconds) prevents hanging operations
- **Automatic cleanup** ensures no resource leaks in long-running processes
- **Context manager support** guarantees proper connection lifecycle management
- **Fingerprint clearing** from `~/.ssh/known_hosts` ensures stateless operation

### 2. **Robust File Transfer**
- **Progress bars with speed monitoring** provide real-time operational visibility
- **Download capabilities** complement upload functionality for bidirectional operations
- **Directory upload** enables bulk configuration deployment
- **Transfer speed metrics** (B/s, KB/s, MB/s) support capacity planning
- **Comprehensive error handling** with detailed messages enables rapid troubleshooting

### 3. **Command Execution**
- **Dedicated SSH connections** ensure command isolation and repeatability
- **Retry logic** (3 attempts) improves reliability in unstable network conditions
- **Full command context in errors** accelerates root cause analysis
- **600-second timeout** balances responsiveness with long-running operation support
- **(return_code, stdout, stderr) tuples** enable programmatic decision making

### 4. **Interactive Terminal**
- **Windows Terminal/CMD integration** provides native user experience
- **Auto-approval of fingerprints** eliminates manual intervention
- **Custom window titles** improve operational awareness
- **Initial command execution** enables automated workflow initiation
- **Automatic reconnect attempts** reduce downtime

### 5. **Error Handling Framework**
- **Custom exception hierarchy** enables targeted error handling strategies
- **Detailed error messages** with full context reduce mean time to resolution
- **No silent failures** ensures problems are never hidden
- **Strict type checking** with mypy prevents class of programming errors

## Implementation Benefits

### Operational Efficiency
- **Hardcoded credentials** (user/password) eliminate credential management overhead
- **Automatic fingerprint handling** removes manual approval steps
- **Comprehensive validation** catches errors early in the workflow
- **Modular architecture** allows targeted upgrades and maintenance

### Strategic Capabilities
- **Bidirectional file operations** support both deployment and retrieval
- **Command execution** enables remote configuration and troubleshooting
- **Interactive terminal** provides fallback for complex manual operations
- **Progress monitoring** supports operational visibility and reporting

### Technical Robustness
- **Context managers** ensure proper resource cleanup
- **Retry mechanisms** improve reliability in unstable environments
- **Type safety** reduces runtime errors and improves maintainability
- **Detailed error reporting** accelerates troubleshooting and resolution

## Usage Patterns

### Command Line Interface

```bash
# Execute diagnostic commands
linux-ssh exec --device 0 "df -h"

# Deploy configuration files
linux-ssh upload --device 0 ./config.yaml /etc/app/config.yaml

# Retrieve logs for analysis
linux-ssh download --device 0 /var/log/app.log ./logs/app.log

# Launch interactive troubleshooting session
linux-ssh terminal --device 0 --title "Device 1 Debug"
```

### Programmatic Integration

```python
from linux_ssh_tools import (
    SSHConnectionManager,
    SSHCommandExecutor,
    SSHFileTransfer,
    SSHTerminalLauncher,
)

# Establish connection with automatic fingerprint handling
connection = SSHConnectionManager(
    hostname="192.168.1.100",
    username="user",
    password="password"
)

# Execute commands with full output capture
with connection:
    executor = SSHCommandExecutor(connection)
    return_code, stdout, stderr = executor.execute_command("ls -la")
    if return_code != 0:
        raise RuntimeError(f"Command failed: {stderr}")

# Transfer files with progress monitoring
with connection:
    file_transfer = SSHFileTransfer(connection)
    bytes_transferred, speed = file_transfer.upload_file(
        "./deployment.tar.gz", "/tmp/deployment.tar.gz"
    )
    print(f"Deployment package transferred at {speed:.2f} KB/s")

# Launch interactive sessions when automation is insufficient
launcher = SSHTerminalLauncher(connection)
process = launcher.launch_terminal(
    initial_command="bash",
    window_title="Device Console"
)
```

## Configuration

The package includes **pre-configured device profiles** for rapid deployment:

```python
DEFAULT_LINUX_DEVICES = [
    {"hostname": "192.168.1.100", "username": "user", "password": "password"},
    {"hostname": "192.168.1.101", "username": "user", "password": "password"},
]
```

Select devices via the `--device` parameter (0 or 1) for streamlined operations.

## Error Handling Strategy

### Exception Hierarchy
- **SSHConnectionError**: Base class for all connection-related failures
- **SSHTimeoutError**: Specific timeout scenarios for targeted handling
- **FileTransferError**: File operation failures with transfer context
- **TerminalLaunchError**: Terminal session initialization problems

### Error Context
All exceptions include:
- **Full command context** when applicable
- **SSH response details** for diagnostic purposes
- **Operational state** at time of failure
- **Suggested remediation** where identifiable

## Security Model

This implementation is optimized for **internal network operations** with stateless devices:

- **Fingerprint bypass** eliminates manual approval for known devices
- **Automatic fingerprint clearing** ensures stateless operation
- **Hardcoded credentials** simplify deployment in controlled environments
- **Comprehensive validation** prevents unintended operations

**Warning**: This package bypasses standard security mechanisms. Use only on **trusted internal networks** with **stateless hardware devices**.

## Development Considerations

### Type Safety
- **Strict mypy configuration** enforces type correctness
- **Runtime type checking** with typeguard validates inputs
- **Comprehensive type annotations** improve maintainability

### Testing Strategy
- **Connection functionality** tests validate SSH operations
- **Command execution** tests verify remote command handling
- **File transfer** tests confirm bidirectional operations
- **Error handling** tests ensure proper failure modes

## Architecture

```
linux_ssh_tools/
├── pyproject.toml          # Build configuration with strict mypy settings
├── setup.py                # Installation and packaging
├── requirements.txt        # Production dependencies
├── README.md               # This strategic overview
└── src/
    └── linux_ssh_tools/
        ├── __init__.py       # Package initialization and constants
        ├── connection.py     # SSH connection management with fingerprint bypass
        ├── file_transfer.py  # Robust file transfer with progress monitoring
        ├── terminal.py      # Cross-platform terminal launcher
        ├── cli.py           # Command-line interface for operations
        ├── exceptions.py    # Comprehensive error handling framework
        └── types.py         # Type definitions for consistency
```

## Operational Workflows

### Deployment
1. **Package transfer**: Upload deployment artifacts
2. **Configuration deployment**: Transfer config files
3. **Command execution**: Run deployment scripts
4. **Verification**: Execute validation commands

### Troubleshooting
1. **Diagnostic commands**: Execute diagnostic scripts
2. **Log retrieval**: Download system logs
3. **Interactive session**: Launch terminal for manual inspection
4. **Configuration review**: Execute config validation commands

### Maintenance
1. **Status monitoring**: Execute health check commands
2. **Patch deployment**: Transfer and install updates
3. **Configuration updates**: Modify config files remotely
4. **Verification**: Confirm changes via commands

## Strategic Value Proposition

This implementation provides:
- **Operational efficiency** through automation
- **Reliability** via retry mechanisms and validation
- **Visibility** through progress monitoring and metrics
- **Flexibility** supporting both automated and manual workflows
- **Maintainability** through modular design and type safety

The package is engineered to support **high-volume internal network operations** with minimal manual intervention while maintaining operational visibility and control.
