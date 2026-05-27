# anthropic_limits.py

**Purpose**: Monitor and display Claude API rate limits and usage information.

## Overview
This module provides utilities to check Anthropic API rate limits by making a minimal test request and extracting rate-limit headers from the response.

## Key Functions

### `anthropic_limit_headers() -> tuple[int, dict[str, str], str]`
Makes a minimal API request to Anthropic to retrieve rate limit headers.

**Returns**:
- HTTP status code
- Response headers dictionary (with lowercase keys)
- Response body

**Details**:
- Sends a tiny completion request with only 1 max token
- Uses urllib to make the request with proper Anthropic headers
- Catches and handles HTTP errors gracefully
- Includes timeout configuration from environment

### `format_limit_row(headers: dict[str, str], key: str, label: str) -> str | None`
Formats a single rate limit metric from response headers.

**Parameters**:
- `headers`: Response headers from API
- `key`: Rate limit type (requests, input-tokens, output-tokens, tokens)
- `label`: Human-readable label for display

**Returns**:
- Formatted string like "- Requests, 95/100 remaining, resets 2025-05-27T12:00:00Z"
- None if metric not found

### `format_anthropic_limits(status: int, headers: dict[str, str], body: str) -> str`
Formats complete rate limit information as a readable message.

**Includes**:
- Model name
- Request limits
- Input token limits
- Output token limits
- Total token limits
- Reset times
- Error information if status >= 400

### `get_anthropic_limits() -> str`
Public entry point - retrieves and formats all rate limit information.

## Rate Limit Headers
Expected headers from Anthropic API:
- `anthropic-ratelimit-requests-limit`
- `anthropic-ratelimit-requests-remaining`
- `anthropic-ratelimit-requests-reset`
- `anthropic-ratelimit-input-tokens-limit`
- `anthropic-ratelimit-input-tokens-remaining`
- `anthropic-ratelimit-input-tokens-reset`
- `anthropic-ratelimit-output-tokens-limit`
- `anthropic-ratelimit-output-tokens-remaining`
- `anthropic-ratelimit-output-tokens-reset`
- `anthropic-ratelimit-tokens-limit`
- `anthropic-ratelimit-tokens-remaining`
- `anthropic-ratelimit-tokens-reset`

## Configuration Dependencies
- `ANTHROPIC_API_URL`: Anthropic API endpoint
- `ANTHROPIC_KEY`: API key for authentication
- `ANTHROPIC_MODEL`: Model being used
- `ANTHROPIC_VERSION`: API version header
- `COMMAND_TIMEOUT_SECONDS`: Request timeout

## Usage Example
```python
from ai_agent.anthropic_limits import get_anthropic_limits

limits = get_anthropic_limits()
print(limits)
```
