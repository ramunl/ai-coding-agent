"""Turn Anthropic 'model not found' errors into an actionable message.

A retired or mistyped model string returns HTTP 404 with a not_found_error.
Raw, that reads as a cryptic failure on every /plan, /implement, and /bugfix.
This module recognizes that specific case and explains the real cause and fix,
so a model retirement presents as a clear instruction rather than a mystery.
"""

import json

from ai_agent.config import ANTHROPIC_MODEL


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
