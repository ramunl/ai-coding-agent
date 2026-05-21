import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ai_agent.config import COMMAND_TIMEOUT_SECONDS, GITHUB_API_URL, GITHUB_REPOSITORY, GITHUB_TOKEN


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    head_sha: str


def ensure_github_configured() -> None:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured in the agent environment")
    if "/" not in GITHUB_REPOSITORY:
        raise RuntimeError("GITHUB_REPOSITORY must use owner/repo format")


def github_request(method: str, path: str, data: dict | None = None, query: dict | None = None) -> dict:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured")

    url = f"{GITHUB_API_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "channel-cast-ai-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=COMMAND_TIMEOUT_SECONDS) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API failed ({exc.code}) {method} {path}: {error_body}") from exc

    if not response_body:
        return {}
    return json.loads(response_body)
