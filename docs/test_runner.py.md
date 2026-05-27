# test_runner.py

**Purpose**: Execute agent unit tests with proper environment setup.

## Overview
Provides a function to run the agent's unit tests with mock/test credentials to avoid hitting real APIs during testing.

## Key Functions

### `run_unit_tests() -> str`
Runs all unit tests in the repository and returns results.

**Process**:
1. Creates a copy of current environment
2. Sets test credentials:
   - `TELEGRAM_BOT_TOKEN`: "test-telegram-token"
   - `YOUR_CHAT_ID`: "1"
   - `ANTHROPIC_API_KEY`: "test-anthropic-key"
   - `GITHUB_TOKEN`: "test-github-token"
3. Runs: `python -m unittest discover -v`
4. Captures stdout and stderr
5. Returns formatted results

**Returns**: Formatted string with:
- Test status: "passed" or "failed"
- Exit code
- Full test output

**Output Format**:
```
Tests passed (0):
<unittest discover -v output>

or

Tests failed (1):
<unittest discover -v output>
```

**Configuration**:
- Timeout: `COMMAND_TIMEOUT_SECONDS` from config

**Error Handling**:
- Runs with `check=False` so failures don't raise exceptions
- Exit code is included in output
- Full stdout and stderr captured regardless of result

## Test Environment

The test credentials are placeholders to prevent real API calls:
- **Telegram**: Won't connect to Telegram bot API
- **Anthropic**: Won't connect to Claude API
- **GitHub**: Won't connect to GitHub API
- **Chat ID**: Dummy value "1"

Unittests should mock these APIs or work with test doubles.

## Usage Example

```python
from ai_agent.test_runner import run_unit_tests

results = run_unit_tests()
print(results)
# Output:
# Tests passed (0):
# test_something (module.TestClass) ... ok
# test_another (module.TestClass) ... ok
# ...
```

## Configuration Dependencies

- `COMMAND_TIMEOUT_SECONDS`: Max time to wait for tests

## Notes

- Uses Python's built-in `unittest` module
- Discovers and runs all test files in the repository
- `-v` flag provides verbose output with individual test names
- Environment variables are isolated to test subprocess (doesn't affect parent process)
