import json
import urllib.error
import urllib.request

from ai_agent.config import (
    ANTHROPIC_API_URL,
    ANTHROPIC_KEY,
    ANTHROPIC_MODEL,
    ANTHROPIC_VERSION,
    COMMAND_TIMEOUT_SECONDS,
)
from ai_agent.model_errors import is_model_not_found, model_error_message


def anthropic_limit_headers() -> tuple[int, dict[str, str], str]:
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "Reply with OK."}],
    }
    headers = {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "x-api-key": ANTHROPIC_KEY,
    }
    request = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=COMMAND_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, response_headers, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, response_headers, body


def format_limit_row(headers: dict[str, str], key: str, label: str) -> str | None:
    prefix = f"anthropic-ratelimit-{key}"
    limit = headers.get(f"{prefix}-limit")
    remaining = headers.get(f"{prefix}-remaining")
    reset = headers.get(f"{prefix}-reset")
    if not any((limit, remaining, reset)):
        return None

    parts = [label]
    if remaining is not None and limit is not None:
        parts.append(f"{remaining}/{limit} remaining")
    elif remaining is not None:
        parts.append(f"{remaining} remaining")
    elif limit is not None:
        parts.append(f"limit {limit}")
    if reset is not None:
        parts.append(f"resets {reset}")
    return "- " + ", ".join(parts)


def format_anthropic_limits(status: int, headers: dict[str, str], body: str) -> str:
    rows = [
        format_limit_row(headers, "requests", "Requests"),
        format_limit_row(headers, "input-tokens", "Input tokens"),
        format_limit_row(headers, "output-tokens", "Output tokens"),
        format_limit_row(headers, "tokens", "Tokens"),
    ]
    rows = [row for row in rows if row]

    message = [
        f"Claude API limits for {ANTHROPIC_MODEL}:",
        *(rows or ["No Anthropic rate-limit headers were returned."]),
        "",
        "This check consumes one tiny Claude API request.",
    ]
    if status >= 400:
        if is_model_not_found(status, body):
            message.extend(["", model_error_message()])
        else:
            message.extend(["", f"Anthropic returned HTTP {status}:", body[:1000]])
    return "\n".join(message)


def get_anthropic_limits() -> str:
    status, headers, body = anthropic_limit_headers()
    return format_anthropic_limits(status, headers, body)
