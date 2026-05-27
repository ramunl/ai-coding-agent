# github.py

**Purpose**: Low-level GitHub API interactions and authentication.

## Overview
Provides utilities for making authenticated GitHub API requests and represents pull request data.

## Data Structures

### `PullRequest` (dataclass)
Represents a GitHub pull request with essential metadata.

**Fields**:
- `number: int` - PR number
- `url: str` - HTML URL to the PR
- `head_sha: str` - Commit SHA of the PR head branch

## Key Functions

### `ensure_github_configured() -> None`
Validates that GitHub is properly configured before operations.

**Checks**:
- `GITHUB_TOKEN` is set and not empty
- `GITHUB_REPOSITORY` contains a forward slash (owner/repo format)

**Raises**: `RuntimeError` if configuration is incomplete

### `github_request(method: str, path: str, data: dict | None = None, query: dict | None = None) -> dict`
Makes authenticated requests to GitHub REST API.

**Parameters**:
- `method`: HTTP method (GET, POST, PATCH, DELETE, etc)
- `path`: API path (e.g., "/repos/owner/repo/pulls")
- `data`: Request body as dict (optional, auto-JSON encoded)
- `query`: URL query parameters as dict (optional, auto-URL encoded)

**Returns**: Parsed JSON response as dict (or empty dict if no response body)

**Headers Set**:
- `Accept`: application/vnd.github+json
- `Authorization`: Bearer {GITHUB_TOKEN}
- `User-Agent`: channel-cast-ai-agent
- `X-GitHub-Api-Version`: 2022-11-28
- `Content-Type`: application/json (if data provided)

**Error Handling**:
- Catches `urllib.error.HTTPError` exceptions
- Raises `RuntimeError` with:
  - HTTP status code
  - Request method and path
  - Error response body

**Timeout**: Controlled by `COMMAND_TIMEOUT_SECONDS`

## Configuration Dependencies
- `GITHUB_TOKEN`: Authentication token
- `GITHUB_API_URL`: Base API URL (https://api.github.com)
- `GITHUB_REPOSITORY`: Target repository
- `COMMAND_TIMEOUT_SECONDS`: Request timeout

## API Examples

### Get a Pull Request
```python
from ai_agent.github import github_request

pr = github_request("GET", "/repos/owner/repo/pulls/123")
print(pr['title'])
```

### Create a Pull Request
```python
from ai_agent.github import github_request

data = {
    "title": "Add new feature",
    "head": "feature/my-branch",
    "base": "main",
    "body": "Description here"
}
pr = github_request("POST", "/repos/owner/repo/pulls", data)
print(pr['html_url'])
```

### List Workflow Runs
```python
from ai_agent.github import github_request

query = {"head_sha": "abc123", "per_page": "20"}
runs = github_request("GET", "/repos/owner/repo/actions/runs", query=query)
for run in runs['workflow_runs']:
    print(run['name'])
```

## Usage Pattern
```python
from ai_agent.github import ensure_github_configured, github_request

# Always check configuration first
ensure_github_configured()

# Then make requests
result = github_request("GET", "/user")
print(result['login'])
```
