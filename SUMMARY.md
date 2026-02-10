## Summary of Changes

### 1. Made SSH Port Customizable

**Changes to CLI (linux_ssh_tools/src/linux_ssh_tools/cli.py):**
- Added `--port` argument to the argument parser (default: 22)
- Updated `create_connection_manager()` function to accept `port` parameter
- Updated all command functions (`command_execute`, `command_upload`, `command_download`, `command_terminal`) to pass the port parameter

**Result:** Users can now specify custom SSH ports via command line:
```bash
linux-ssh-tools exec --port 2222 "ls -la"
```

### 2. Created Ultimate Test Setup

**New test file (tests/test_ssh_connection.py):**
- Comprehensive test suite with 12 test functions
- SSHServer class that manages local SSH servers as context managers
- Tests connection functionality, command execution, timeouts, and error handling
- Designed to run two local SSH servers on different ports (2222 and 2223)

**Test coverage includes:**
- Connection manager creation with custom ports
- Successful connections to both servers
- Context manager usage
- Command execution
- Multiple simultaneous connections
- Connection timeouts
- Invalid ports
- Reconnection after disconnect
- Command execution with retry logic
- Concurrent command execution

### 3. Updated Requirements

**Changes to requirements.txt:**
- Added psutil as an optional development dependency for process management in tests

## Files Modified

1. `linux_ssh_tools/src/linux_ssh_tools/cli.py` - Added port customization
2. `linux_ssh_tools/requirements.txt` - Added psutil for testing

## Files Created

1. `tests/test_ssh_connection.py` - Comprehensive test suite
2. `test_basic_functionality.py` - Basic functionality test
3. `test_debug.py` - Debugging script

## Next Steps

To run the tests:
1. Install dependencies: `pip install pytest psutil`
2. Run tests: `pytest tests/test_ssh_connection.py -v`

The test suite is designed to create real SSH servers on localhost, allowing for comprehensive integration testing of the SSH connection functionality.
