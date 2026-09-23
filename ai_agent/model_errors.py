"""Turn Anthropic 'model not found' errors into an actionable message.

A retired or mistyped model string returns HTTP 404 with a not_found_error.
Raw, that reads as a cryptic failure on every /plan, /implement, and /bugfix.
This module recognizes that specific case and explains the real cause and fix,
so a model retirement presents as a clear instruction rather than a mystery.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager

from ai_agent.config import ANTHROPIC_MODEL

CODEX_CAPACITY_MARKER = "Selected model is at capacity"


def is_model_not_found(status: int, body: str) -> bool:
    """True when the response is specifically a missing/invalid model error."""
    is_not_found_status = status == 404
    if is_not_found_status:
        try:
            error_type = json.loads(body).get("error", {}).get("type", "")
        except (json.JSONDecodeError, AttributeError):
            error_type = ""
        return error_type == "not_found_error" and "model" in body
    return False


def model_error_message(model: str = ANTHROPIC_MODEL) -> str:
    """A clear explanation shown when the configured model is unavailable."""
    return (
        f"The configured Claude model '{model}' is unavailable — it is most "
        "likely retired or misspelled, so the API rejected it.\n\n"
        "Fix: set ANTHROPIC_MODEL to a current model (for example "
        "claude-sonnet-4-6) and restart, or use /model to switch.\n\n"
        "Current model strings: https://docs.claude.com/en/docs/about-claude/models/overview"
    )


class CodexCapacityError(RuntimeError):
    """The Codex CLI refused a run because its model is temporarily overloaded."""


def is_codex_at_capacity(output: str) -> bool:
    """True when the Codex CLI refused the run because its model is overloaded."""
    return CODEX_CAPACITY_MARKER in output


def codex_capacity_message() -> str:
    """A clear explanation shown instead of the raw, prompt-sized Codex failure."""
    return (
        "Codex's model is at capacity right now (a temporary OpenAI-side limit), "
        "so nothing was changed.\n\n"
        "Fix: retry in a few minutes, switch agent with /planner claude or "
        "/agent claude, or pick another model via `model = ...` in "
        "~/.codex/config.toml."
    )


@contextmanager
def codex_capacity_explained() -> Iterator[None]:
    """Replace a Codex 'at capacity' failure with a short, actionable error.

    The raw command failure echoes the whole prompt, which buries the real
    cause once the Telegram error message is truncated.
    """
    try:
        yield
    except RuntimeError as error:
        if is_codex_at_capacity(str(error)):
            raise CodexCapacityError(codex_capacity_message()) from error
        raise
