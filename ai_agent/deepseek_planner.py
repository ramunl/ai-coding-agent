"""Native DeepSeek API planner over OpenAI-compatible Chat Completions.

DeepSeek plans through ``POST {base}/chat/completions`` with JSON-object output,
bounded ``max_tokens`` and a bounded response read so a 1 GB VPS cannot hang or
grow memory on a slow or oversized reply. ``finish_reason`` is checked and a
``length`` (truncated) reply gets exactly one accounted repair call at the
configured output bound; a second truncation fails instead of looping.

The API key and the model's ``reasoning_content`` are never logged or echoed.
HTTP 401/402/429 are classified into auth/funds/transient failures, and every
inference call is recorded through the shared token-usage and usage-detail
stores. Model and balance discovery use non-inference GETs that never consume
quota.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from ai_agent.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_MAX_OUTPUT_CHARS,
    DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
    redact_sensitive,
)
from ai_agent.token_usage import UsageRecord, record_usage
from ai_agent.usage_details import (
    UsageDetail,
    bounded_output_chars,
    current_attempt_id,
    new_call_id,
    record_detail,
)

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/chat/completions"
MODELS_PATH = "/models"
BALANCE_PATH = "/user/balance"

# Error bodies are read only for classification; never echo them in full.
ERROR_BODY_BYTES = 4000
# Discovered identifiers/balances are display-only and truncated hard.
SHORT_FIELD_LENGTH = 60


class DeepSeekError(RuntimeError):
    """Base error for the native DeepSeek planner."""


class DeepSeekAuthError(DeepSeekError):
    """The API key was rejected."""


class DeepSeekFundsError(DeepSeekError):
    """The account has no spendable balance."""


class DeepSeekTransientError(DeepSeekError):
    """A rate limit, timeout or provider-side outage."""


class DeepSeekTruncatedError(DeepSeekError):
    """The reply was cut off and the single repair did not recover it."""


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _headers(response: Any) -> dict[str, str]:
    """Response headers lower-cased, tolerating mocks without headers."""
    headers = getattr(response, "headers", None) or {}
    try:
        return {str(key).lower(): str(value) for key, value in headers.items()}
    except AttributeError:
        return {}


def _read_bounded(stream: Any, limit: int) -> bytes:
    """Read at most ``limit`` bytes and refuse an oversized reply."""
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise DeepSeekError("DeepSeek response exceeded the output bound.")
    return data


def _read_error_body(stream: Any) -> bytes:
    """Read a short error body for classification; truncate the rest."""
    return stream.read(ERROR_BODY_BYTES)


def _request(
    method: str, path: str, payload: dict | None = None
) -> tuple[int, dict[str, str], bytes]:
    """Make one authenticated request and return status, headers and body."""
    if not DEEPSEEK_API_KEY:
        raise DeepSeekAuthError("DEEPSEEK_API_KEY is not configured.")
    url = f"{DEEPSEEK_BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=DEEPSEEK_TIMEOUT_SECONDS
        ) as response:
            status = getattr(response, "status", 200)
            body = _read_bounded(response, DEEPSEEK_MAX_OUTPUT_CHARS * 4)
            return status, _headers(response), body
    except urllib.error.HTTPError as error:
        return error.code, _headers(error), _read_error_body(error)
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", None) or type(error).__name__
        detail = redact_sensitive(str(reason))[:200]
        raise DeepSeekTransientError(f"DeepSeek request failed: {detail}") from error


def classify_http_error(status: int, body: str) -> DeepSeekError:
    """Classify a non-2xx response into an actionable, secret-free error."""
    if status == 401:
        return DeepSeekAuthError("DeepSeek rejected the API key (HTTP 401).")
    if status == 402:
        return DeepSeekFundsError("DeepSeek account has no funds (HTTP 402).")
    if status == 429:
        return DeepSeekTransientError("DeepSeek rate limit reached (HTTP 429).")
    if status in (408, 500, 502, 503, 504):
        return DeepSeekTransientError(
            f"DeepSeek is temporarily unavailable (HTTP {status})."
        )
    detail = redact_sensitive(" ".join(body.split()))[:200]
    return DeepSeekError(f"DeepSeek returned HTTP {status}: {detail}")


def _json_body(body: bytes) -> dict:
    """Parse a JSON body into a dict, or raise a clear planner error."""
    try:
        value = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as error:
        raise DeepSeekError("DeepSeek returned a non-JSON response.") from error
    if not isinstance(value, dict):
        raise DeepSeekError("DeepSeek returned an unexpected response shape.")
    return value


def _chat_completion(messages: list[dict], max_tokens: int, model: str) -> dict:
    """One Chat Completions call with JSON-object output."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    status, _headers, body = _request("POST", CHAT_COMPLETIONS_PATH, payload)
    if status >= 400:
        raise classify_http_error(status, body.decode("utf-8", errors="replace"))
    return _json_body(body)


def _message_text(choice: dict) -> str:
    """The text of one choice, ignoring ``reasoning_content`` entirely."""
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        content = "".join(parts)
    if not isinstance(content, str):
        return ""
    return content.strip()


def _extract_plan(data: dict) -> tuple[str, str, dict]:
    """Return (text, finish_reason, usage) from a Chat Completions response."""
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise DeepSeekError("DeepSeek returned no choices.")
    choice = choices[0]
    finish = str(choice.get("finish_reason") or "")
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return _message_text(choice), finish, usage


def _reasoning_tokens(usage: dict) -> int | None:
    """The model's reported reasoning tokens, kept separate and never totaled."""
    details = usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        return None
    value = details.get("reasoning_tokens")
    return value if _is_count(value) else None


def deepseek_usage_fields(usage: object) -> dict | None:
    """Return DeepSeek usage fields, or None when the counts are unusable.

    The total is input plus output only; reasoning tokens stay separate so they
    are never double-counted, matching the Claude usage contract.
    """
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if not _is_count(input_tokens) or not _is_count(output_tokens):
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": _reasoning_tokens(usage),
        "total_tokens": input_tokens + output_tokens,
    }


def deepseek_total_tokens(usage: object) -> int | None:
    """Return input plus output tokens from a DeepSeek usage object, or None."""
    fields = deepseek_usage_fields(usage)
    return None if fields is None else fields["total_tokens"]


def _bounded_tokens(requested: int) -> int:
    """Clamp the caller's max_tokens to the configured output bound."""
    if not isinstance(requested, int) or requested <= 0:
        return DEEPSEEK_MAX_OUTPUT_TOKENS
    return min(requested, DEEPSEEK_MAX_OUTPUT_TOKENS)


def _accounted_call(
    messages: list[dict], max_tokens: int, model: str
) -> tuple[str, str]:
    """One inference call with usage recorded; returns (text, finish_reason)."""
    call_id = new_call_id()
    data = _chat_completion(messages, max_tokens, model)
    text, finish, usage = _extract_plan(data)
    fields = deepseek_usage_fields(usage)
    record_usage(UsageRecord("planner", "deepseek", deepseek_total_tokens(usage)))
    record_detail(
        UsageDetail(
            role="planner",
            runner="deepseek-api",
            provider="deepseek",
            model=model,
            attempt_id=current_attempt_id(),
            call_id=call_id,
            input_tokens=(fields or {}).get("input_tokens"),
            output_tokens=(fields or {}).get("output_tokens"),
            reasoning_tokens=(fields or {}).get("reasoning_tokens"),
            total_tokens=(fields or {}).get("total_tokens"),
            output_chars=bounded_output_chars(text),
        )
    )
    return text, finish


def _validate_plan_text(text: str) -> str:
    """Require a non-empty, bounded JSON object; return it unchanged."""
    if not text:
        raise DeepSeekError("DeepSeek returned no text for the plan. Please retry.")
    if len(text) > DEEPSEEK_MAX_OUTPUT_CHARS:
        raise DeepSeekTruncatedError("DeepSeek output exceeded the configured bound.")
    try:
        parsed = json.loads(text)
    except ValueError as error:
        raise DeepSeekError("DeepSeek returned invalid JSON for the plan.") from error
    if not isinstance(parsed, dict):
        raise DeepSeekError("DeepSeek returned a non-object JSON plan.")
    return text


def _repair(messages: list[dict]) -> str:
    """The single accounted repair after a truncated first reply."""
    text, finish = _accounted_call(messages, DEEPSEEK_MAX_OUTPUT_TOKENS, DEEPSEEK_MODEL)
    if finish == "length":
        raise DeepSeekTruncatedError(
            "DeepSeek returned a truncated plan twice; raise the output bound "
            "or switch planner."
        )
    return _validate_plan_text(text)


def planner_message(prompt: str, schema_name: str, max_tokens: int) -> str:
    """Plan with DeepSeek and return the JSON reply as text.

    ``schema_name`` is accepted for adapter uniformity; the JSON shape is
    already embedded in ``prompt`` and ``response_format`` asks for an object.
    """
    from ai_agent.image_delivery import attachments

    images, _missing = attachments()
    if images:
        raise DeepSeekError(
            "DeepSeek does not support image input. Switch the planner to "
            "codex or claude, or attach no screenshots."
        )
    messages = [
        {
            "role": "system",
            "content": "You are a planning assistant. Reply with one JSON object only.",
        },
        {"role": "user", "content": prompt},
    ]
    text, finish = _accounted_call(
        messages, _bounded_tokens(max_tokens), DEEPSEEK_MODEL
    )
    if finish == "length":
        return _repair(messages)
    return _validate_plan_text(text)


def _short_text(value: object) -> str:
    """A display-only, single-line truncation of a provider string."""
    if value is None:
        return ""
    return " ".join(str(value).split())[:SHORT_FIELD_LENGTH]


def discover_models() -> list[dict[str, str]]:
    """List models through the non-inference ``GET /models`` endpoint.

    The configured pro and flash models are always listed first, so a broken or
    empty catalog can never hide the models this agent is told to use.
    """
    status, _headers, body = _request("GET", MODELS_PATH)
    if status >= 400:
        raise classify_http_error(status, body.decode("utf-8", errors="replace"))
    data = _json_body(body)
    models: dict[str, dict[str, str]] = {}
    for model in (DEEPSEEK_MODEL, DEEPSEEK_FLASH_MODEL):
        if model:
            models[model] = {"id": model, "display_name": model}
    for entry in data.get("data") or []:
        if not isinstance(entry, dict):
            continue
        model = entry.get("id")
        if not isinstance(model, str) or not model:
            continue
        models.setdefault(
            model,
            {"id": model, "display_name": entry.get("display_name") or model},
        )
    return list(models.values())


def discover_balance() -> dict:
    """Read account balance through the non-inference ``GET /user/balance``.

    Returns only the whitelisted display fields; the raw provider payload is
    never published.
    """
    status, _headers, body = _request("GET", BALANCE_PATH)
    if status >= 400:
        raise classify_http_error(status, body.decode("utf-8", errors="replace"))
    data = _json_body(body)
    infos = data.get("balance_infos") or []
    first = (
        infos[0]
        if isinstance(infos, list) and infos and isinstance(infos[0], dict)
        else {}
    )
    return {
        "is_available": data.get("is_available") is True,
        "currency": _short_text(first.get("currency", data.get("currency"))),
        "total_balance": _short_text(
            first.get("total_balance", data.get("total_balance"))
        ),
        "granted_balance": _short_text(
            first.get("granted_balance", data.get("granted_balance"))
        ),
        "topped_up_balance": _short_text(
            first.get("topped_up_balance", data.get("topped_up_balance"))
        ),
    }
