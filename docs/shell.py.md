# shell.py

**Purpose**: Safe execution of shell commands with error handling and output capture.

## Overview
Provides a wrapper around subprocess to execute commands with timeout protection and unified error handling.

## Data Structures

### `CommandResult` (dataclass)
Encapsulates the result of a command execution.

**Fields**:
- `args: list[str]` - Command arguments that were executed
- `returncode: int` - Exit code (should be 0 for success)
- `output: str` - Combined stdout and stderr as text

## Key Functions

### `run(args: list[str], cwd: Path = REPO_PATH, timeout: int = COMMAND_TIMEOUT_SECONDS) -> CommandResult`
Executes a shell command and returns output.

**Parameters**:
- `args`: Command as list (e.g., ["git", "status"])
- `cwd`: Working directory (defaults to repository path)
- `timeout`: Max seconds to wait for completion

**Returns**: `CommandResult` with command output and exit code

**Error Handling**:
- Captures both stdout and stderr into single output string
- Runs command with `check=False` to not raise on non-zero exit
- Catches `subprocess.TimeoutExpired` exceptions
  - Combines partial stdout/stderr
  - Raises `RuntimeError` with timeout message
- Raises `RuntimeError` if return code != 0:
  - Includes command, exit code, and full output

**Timeout Behavior**:
- On timeout, captures any partial output
- Raises: `RuntimeError(f"Command timed out after {timeout}s: {command}\n{partial_output}")`

**Logging**:
- Logs all executed commands with parameters:
  - Command args
  - Working directory
  - Timeout value

## Configuration Dependencies

- `COMMAND_TIMEOUT_SECONDS`: Default timeout (from config)
- `REPO_PATH`: Default working directory (from config)

## Usage Examples

### Basic Command
```python
from ai_agent.shell import run

result = run(["git", "status"])
print(result.output)
print(f"Exit code: {result.returncode}")
```

### Custom Working Directory
```python
from pathlib import Path
from ai_agent.shell import run

result = run(["ls", "-la"], cwd=Path("/tmp"))
print(result.output)
```

### Custom Timeout
```python
from ai_agent.shell import run

result = run(
    ["npm", "test"],
    timeout=300  # 5 minutes
)
```

### Error Handling
```python
from ai_agent.shell import run

try:
    result = run(["git", "commit", "-m", "My message"])
except RuntimeError as e:
    print(f"Command failed: {e}")
    # e contains command, exit code, and output
```

## Common Commands Used

**Git Operations**:
```python
run(["git", "checkout", "main"])
run(["git", "pull", "origin", "main"])
run(["git", "checkout", "-b", "feature/name"])
run(["git", "add", "."])
run(["git", "commit", "-m", "message"])
run(["git", "push", "origin", "branch"])
run(["git", "rev-parse", "HEAD"])  # Get current commit
run(["git", "status", "--porcelain"])  # Check for changes
run(["git", "branch", "-a"])  # List branches
```

**Codex Operations**:
```python
run(["codex", "--version"])
run(["codex", "login", "status"])
run(["codex", "prompt"])  # Run Codex with prompt
```

**System Operations**:
```python
run(["journalctl", "-u", "service-name", "-n", "100"])
```

## Implementation Details

- Uses `subprocess.run()` with:
  - `capture_output=True` - Captures stdout and stderr
  - `check=False` - Doesn't raise on non-zero exit
  - `text=True` - Text mode (not binary)
  - `timeout` parameter - Enforces time limit

- Combines output: `output = result.stdout + result.stderr`

- Logs with Python's standard logging module

## Error Messages

On failure, error message format:
```
Command timed out after 120s: git pull origin main
<partial stdout/stderr if available>

-- or --

Command failed (1): npm test
<stdout and stderr combined>
```
