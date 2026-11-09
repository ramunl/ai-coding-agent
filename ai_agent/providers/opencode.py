"""OpenCode provider: an optional DeepSeek *executor*, owner-gated.

OpenCode (npm ``opencode-ai``) can implement changes through the DeepSeek API.
It is **not** enabled in production: ``enabled`` stays ``False`` until the owner
approves a reviewed pin, so the worker can never route an implementation run
through it on its own. The module still declares the executor contract
(roles/adapters/model/auth/limits/errors/usage) so that, once the owner flips
``enabled``, the registry, dashboard snapshot and worker pick up the provider
without a code change.

``scripts/opencode_characterize.sh`` verifies the release provenance and Node
requirement, installs the reviewed pin in a disposable checkout, captures the
sanitized JSON-event fixtures in ``tests/fixtures/opencode/`` and records peak
RAM. The pin and Node requirement below are the reviewed values that script
records; they stay empty/unverified until the owner's trial confirms a release.

The event contract and the pure ``classify_*``/``parse_*``/``sanitize_*``
helpers below are characterization artifacts replayed by
``tests/test_opencode_contract.py`` and consumed by
``ai_agent.opencode_runner``. They are plain module functions — **not**
registry capabilities — so ``provider_registry.capability()`` still answers
"unsupported" for every one of them and the dashboard snapshot never publishes
them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from ai_agent.config import OPENCODE_MODEL
from ai_agent.provider_registry import ErrorAdapter, ModelControl
from ai_agent.tool_auth_providers import Reading, check

name = "opencode"
# Owner-gated: flips to True only after scripts/opencode_characterize.sh has
# verified a reviewed pin and the owner approves the trial. The worker never
# routes through a disabled provider.
enabled = False
roles = ("executor",)
images = False
structured_output = False

# Reviewed pin and Node requirement, recorded by scripts/opencode_characterize.sh
# after verifying npm provenance and the VPS Node runtime. Empty PINNED_VERSION
# means "not yet reviewed"; the script refuses to run without an exact version.
PINNED_VERSION = ""
NODE_REQUIREMENT = ">=22"

# The read-only model dial: OpenCode runs the DeepSeek API model configured by
# OPENCODE_MODEL, and this agent never switches it.
MODEL_ENV_VAR = "OPENCODE_MODEL"
MODEL_DEFAULT = "deepseek/deepseek-chat"
NOTE = "Set by OPENCODE_MODEL; not switched by this agent."

# OpenCode JSON-event types this repo's contract recognizes. Events are plain
# objects with ``type`` plus a ``properties`` object; anything else is ignored.
EVENT_MESSAGE = "message"
EVENT_RESULT = "result"
EVENT_ERROR = "error"
EVENT_USAGE = "usage"
EVENT_CANCELLED = "cancelled"
EVENT_TYPES = (
    EVENT_MESSAGE,
    EVENT_RESULT,
    EVENT_ERROR,
    EVENT_USAGE,
    EVENT_CANCELLED,
)

# Secret-looking substrings the sanitizer redacts from captured fixtures. Key
# material, auth headers and balances must never leak into tests/fixtures.
_SENSITIVE_VALUE = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{4,}|bearer\s+[A-Za-z0-9._-]+)")

# Keys that are dropped wholesale. "token" alone is deliberately not enough:
# the usage contract's input_tokens/output_tokens/reasoning_tokens are
# legitimate non-secret counts and must survive sanitization.
_SENSITIVE_KEYS = {
    "key",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "secret",
    "password",
    "credential",
    "credentials",
    "balance",
    "credit",
    "credits",
    "billing",
    "token",
    "tokens",
    "access_token",
    "api_token",
    "auth_token",
    "refresh_token",
}


def _is_sensitive_key(name: str) -> bool:
    """Whether a JSON key is credential-like and must be dropped."""
    lowered = name.strip().lower()
    if lowered in _SENSITIVE_KEYS:
        return True
    if any(part in lowered for part in ("secret", "password", "credential")):
        return True
    if any(part in lowered for part in ("balance", "credit", "billing")):
        return True
    if lowered.endswith("_key") or "apikey" in lowered:
        return True
    return "token" in lowered and any(
        part in lowered for part in ("access", "auth", "api", "refresh")
    )


def _is_count(value: object) -> bool:
    """Whether a value is a usable non-negative token count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def sanitize_text(text: object) -> str:
    """Redact secret-looking substrings from one line of captured text."""
    return _SENSITIVE_VALUE.sub("<redacted>", "" if text is None else str(text))


def sanitize_event(value: object) -> object:
    """Recursively strip secret keys and redact secret values from an event.

    Keys whose name contains ``key``/``token``/``secret``/``auth``/``balance``/
    ``credit``/``password`` are dropped outright, and values matching the
    secret pattern are replaced. The result is the sanitized, JSON-safe form
    the characterization script writes into ``tests/fixtures/opencode/``.
    """
    if isinstance(value, dict):
        cleaned: dict = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                continue
            cleaned[key] = sanitize_event(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_event(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def parse_events(text: str) -> list[dict]:
    """Parse captured output into a list of event objects.

    Accepts a JSON array, a wrapper object with an ``events`` list (the checked
    fixture shape), or JSONL (one object per line). Unparseable or non-object
    entries are skipped so a stray log line never breaks the contract.
    """
    stripped = "" if not text else text.strip()
    if not stripped:
        return []
    try:
        value = json.loads(stripped)
    except ValueError:
        value = None
    if isinstance(value, list):
        return [event for event in value if isinstance(event, dict)]
    if isinstance(value, dict):
        events = value.get("events")
        if isinstance(events, list):
            return [event for event in events if isinstance(event, dict)]
    events: list[dict] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def usage_fields(props: Mapping[str, object]) -> dict | None:
    """Normalize a ``usage`` event's properties, or None when counts are bad.

    The total is input plus output; reasoning tokens stay separate so they are
    never double-counted, matching the DeepSeek and Claude usage contracts.
    """
    input_tokens = props.get("input_tokens")
    output_tokens = props.get("output_tokens")
    if not _is_count(input_tokens) or not _is_count(output_tokens):
        return None
    total = props.get("total_tokens")
    if not _is_count(total):
        total = input_tokens + output_tokens
    reasoning = props.get("reasoning_tokens")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning if _is_count(reasoning) else 0,
        "total_tokens": total,
    }


def error_kind(code: object, message: object) -> str:
    """Classify an OpenCode error event into auth/funds/transient/execution.

    This is the fixture contract only. The worker's own failure vocabulary
    lives in ``ai_agent.provider_availability`` and classifies the run's
    failure text separately.
    """
    text = " ".join(
        ["" if code is None else str(code), "" if message is None else str(message)]
    ).lower()
    if any(word in text for word in ("auth", "api_key", "unauthorized", "401")):
        return "auth"
    if any(
        word in text
        for word in ("fund", "balance", "billing", "402", "insufficient", "credit")
    ):
        return "funds"
    if any(
        word in text
        for word in ("rate", "limit", "429", "timeout", "timed out", "retry", "network")
    ):
        return "transient"
    return "execution"


def classify_run(events: object, exit_code: object) -> dict:
    """Classify an OpenCode run's events and exit code into the documented
    exit/error/usage/cancel contract.

    Returns a dict with ``exit_code``, ``cancelled``, ``error`` (first error
    message or a synthetic exit-status message), ``error_kind`` and ``usage``
    (the first usable usage event). Non-zero exit without an error event is an
    execution error; a ``cancelled`` event suppresses that synthetic error.
    """
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = 0
    cancelled = False
    error: str | None = None
    kind: str | None = None
    usage: dict | None = None
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        props = event.get("properties")
        props = props if isinstance(props, dict) else {}
        etype = event.get("type")
        if etype == EVENT_CANCELLED:
            cancelled = True
        elif etype == EVENT_ERROR and error is None:
            raw = str(props.get("message") or props.get("error") or "")
            error = raw.strip() or None
            kind = error_kind(props.get("code"), raw)
        elif etype == EVENT_USAGE and usage is None:
            usage = usage_fields(props)
    if error is None and not cancelled and exit_code != 0:
        error = f"opencode exited with status {exit_code}"
        kind = "execution"
    return {
        "exit_code": exit_code,
        "cancelled": cancelled,
        "error": error,
        "error_kind": kind,
        "usage": usage,
    }


def result_text(events: object) -> str:
    """The final result event's prose, or '' when the stream has none.

    OpenCode reports its completion prose under ``properties.text`` (with
    ``result``/``message`` accepted as fallbacks for contract drift); the last
    result event wins.
    """
    text = ""
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("type") != EVENT_RESULT:
            continue
        props = event.get("properties")
        if not isinstance(props, dict):
            continue
        value = props.get("text") or props.get("result") or props.get("message")
        if isinstance(value, str) and value.strip():
            text = value.strip()
    return text


# ---------------------------------------------------------------- provider contract


def _current_model() -> str:
    """Return the configured DeepSeek model OpenCode runs."""
    return OPENCODE_MODEL


def _read_only_verify(model: str) -> tuple[bool, str]:
    """Reject switching: OpenCode's model is owned by OPENCODE_MODEL."""
    return False, NOTE


def _read_only_list() -> tuple[bool, str]:
    """Report that models are not listable from here."""
    return False, NOTE


model = ModelControl(
    manageable=False,
    note=NOTE,
    current=_current_model,
    args=lambda: [],
    verify=_read_only_verify,
    list_models=_read_only_list,
    set=None,
)


async def auth() -> Reading:
    """Observe the DeepSeek API-key presence without making a request."""
    return await check("opencode")


def limits() -> dict:
    """Return the last recorded DeepSeek balance reading (OpenCode's backend)."""
    from ai_agent.provider_limits import limits_snapshot

    return limits_snapshot()["deepseek"]


def usage(reported: object) -> int | None:
    """Count the total tokens from a parsed OpenCode usage event, or None."""
    if not isinstance(reported, dict):
        return None
    fields = usage_fields(reported)
    return fields["total_tokens"] if fields is not None else None


def _is_opencode_error(error: BaseException) -> bool:
    """Whether an exception is a bounded OpenCode runner failure."""
    return isinstance(error, RuntimeError) and not isinstance(
        error, (KeyboardInterrupt, SystemExit)
    )


def _error_message(error: BaseException) -> str:
    """A secret-free explanation for an OpenCode runner failure."""
    return str(error) if str(error) else "OpenCode implementation failed."


errors = ErrorAdapter(classify=_is_opencode_error, message=_error_message)


def _executor(prompt: str) -> object:
    """Implement with OpenCode through the shared executor dispatch."""
    from ai_agent.workflow import run_implementation_agent

    return run_implementation_agent(prompt, "opencode")


adapters = {"executor": _executor}
