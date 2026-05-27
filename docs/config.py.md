# config.py

**Purpose**: Centralized configuration management using environment variables.

## Overview
Defines all configuration constants for the AI agent, reading from environment variables with sensible defaults where appropriate.

## Configuration Constants

### Telegram Configuration
- **`TELEGRAM_TOKEN`** (required): Telegram bot token for authentication
- **`CHAT_ID`** (required): Authorized chat ID (converted to int)

### Repository Configuration
- **`REPO_PATH`** (default: ~/your-android-repo): Path to target repository
  - Expanded with `expanduser()` to handle ~ paths
  - Expected to be an Android project repository

### Anthropic/Claude Configuration
- **`ANTHROPIC_KEY`** (required): API key for Claude AI
- **`ANTHROPIC_MODEL`** (default: claude-sonnet-4-20250514): Claude model version
- **`ANTHROPIC_API_URL`**: Fixed to `https://api.anthropic.com/v1/messages`
- **`ANTHROPIC_VERSION`**: Fixed to `2023-06-01` (API version)

### GitHub Configuration
- **`GITHUB_TOKEN`** (optional, default: ""): Personal access token for GitHub API
- **`GITHUB_REPOSITORY`** (default: ramunl/com.randrgames.channelcast): Target repository in owner/repo format
- **`GITHUB_BASE_BRANCH`** (default: main): Base branch for pull requests
- **`GITHUB_API_URL`**: Fixed to `https://api.github.com`

### Timeout Configuration
- **`COMMAND_TIMEOUT_SECONDS`** (default: 120): Max seconds for shell commands
- **`CODEX_TIMEOUT_SECONDS`** (default: 1800): Max seconds for Codex runs (30 minutes)
- **`CI_POLL_INTERVAL_SECONDS`** (default: 30): Seconds between CI status polls
- **`CI_TIMEOUT_SECONDS`** (default: 1800): Max seconds to wait for CI (30 minutes)

### Message Configuration
- **`MAX_TELEGRAM_MESSAGE_LENGTH`**: 3900 characters (Telegram limit is 4096)
- **`MAX_LOG_LINES`**: 120 lines of logs to display

### Link Configuration
- **`LINK_ALLOWED_DOMAINS`**: Tuple of allowed web domains for link processing
  - Default: developer.android.com, docs.github.com, kotlinlang.org, stackoverflow.com
  - Set via comma-separated `LINK_ALLOWED_DOMAINS` env var
  - Strips whitespace and lowercases all domains

## Security Functions

### `redact_sensitive(text: str) -> str`
Removes sensitive credentials from text output (logs, errors).

**Redacts**:
- `TELEGRAM_TOKEN`
- `ANTHROPIC_KEY`
- `GITHUB_TOKEN`

**Returns**: Text with secrets replaced by "[redacted]"

## Environment Variable Reference

```bash
# Required
export TELEGRAM_BOT_TOKEN="your-token"
export YOUR_CHAT_ID="12345"
export ANTHROPIC_API_KEY="sk-ant-..."

# Optional with defaults
export REPO_PATH="~/my-repo"
export ANTHROPIC_MODEL="claude-sonnet-4-20250514"
export GITHUB_TOKEN="ghp_..."
export GITHUB_REPOSITORY="owner/repo"
export GITHUB_BASE_BRANCH="main"
export COMMAND_TIMEOUT_SECONDS="120"
export CODEX_TIMEOUT_SECONDS="1800"
export CI_POLL_INTERVAL_SECONDS="30"
export CI_TIMEOUT_SECONDS="1800"
export LINK_ALLOWED_DOMAINS="developer.android.com,docs.github.com,kotlinlang.org,stackoverflow.com"
```

## Usage Example
```python
from ai_agent.config import (
    TELEGRAM_TOKEN,
    GITHUB_REPOSITORY,
    REPO_PATH,
    redact_sensitive
)

print(f"Target repo: {GITHUB_REPOSITORY}")
print(f"Working in: {REPO_PATH}")

error_message = f"Error with token {TELEGRAM_TOKEN}"
print(redact_sensitive(error_message))  # Shows [redacted]
```
