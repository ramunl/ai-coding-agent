# codex_status.py

**Purpose**: Query and display Codex CLI/OpenAI integration status.

## Overview
This module retrieves the status of the Codex CLI tool, checking if it's installed and the user is logged in.

## Key Functions

### `get_codex_status() -> str`
Returns formatted status information about the Codex CLI.

**Information Gathered**:
1. **CLI Version**: Runs `codex --version` to check installation
2. **Login Status**: Runs `codex login status` to verify authentication
3. **Plan Limits**: Notes that plan usage limits are not exposed by Codex API

**Returns**: Multi-line formatted string:
```
Codex status:
- CLI: <version or 'installed'>
- Login: <login status or 'unknown'>
- Plan limits remaining: not exposed by the Codex CLI/API

Check remaining Codex plan usage in the Codex/OpenAI UI when a usage banner appears.
```

**Output Examples**:
- **If installed**: Shows version string from `codex --version`
- **If not installed**: Shows "installed" as fallback
- **Login status**: Shows output from `codex login status`

## Shell Commands Used
- `codex --version`: Check Codex CLI version
- `codex login status`: Check authentication status

## Configuration Dependencies
- `COMMAND_TIMEOUT_SECONDS`: Timeout for each shell command

## Exceptions
If Codex CLI is not installed, shell commands will raise `RuntimeError` via the shell module, which propagates to the caller.

## Usage Example
```python
from ai_agent.codex_status import get_codex_status

status = get_codex_status()
print(status)
```

## Notes
- Codex is OpenAI's code generation tool used for implementation
- This is just a status checker, not for running Codex code generation
- Plan limits must be checked through the Codex/OpenAI dashboard
