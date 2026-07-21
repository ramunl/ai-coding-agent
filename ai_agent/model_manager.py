"""View and switch the Claude model from Telegram.

Switching writes ANTHROPIC_MODEL to the env file and restarts the service.
The footgun is obvious: a bad string persisted to the boot env file would
crash every start. The guardrail: a candidate model is probed against the
live API and only written if it responds. An invalid string is rejected at
the command and never reaches disk.
"""

import logging
import os
import re
from pathlib import Path

from ai_agent.anthropic_limits import anthropic_limit_headers
from ai_agent.config import ANTHROPIC_MODEL
from ai_agent.model_errors import is_model_not_found

logger = logging.getLogger(__name__)

ENV_FILE = Path(os.environ.get("AGENT_ENV_FILE", "/etc/ai-agent/ai-agent.env"))

# Conservative: matches Anthropic model strings without allowing shell-unsafe
# characters into a file the service sources at boot.
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def active_model() -> str:
    return ANTHROPIC_MODEL


def verify_model(model: str) -> tuple[bool, str]:
    """Probe a model with one tiny request. Returns (reachable, detail)."""
    is_well_formed = bool(_MODEL_PATTERN.match(model))
    if not is_well_formed:
        return False, "Model name has invalid characters."

    status, _headers, body = _probe(model)
    reachable = status < 400
    if reachable:
        return True, "reachable"
    if is_model_not_found(status, body):
        return False, "the API does not recognize this model (retired or misspelled)"
    return False, f"HTTP {status}: {body[:200]}"


def _probe(model: str) -> tuple[int, dict, str]:
    # anthropic_limit_headers reads ANTHROPIC_MODEL from config, so probe a
    # candidate by temporarily overriding the module-level value.
    import ai_agent.anthropic_limits as limits_module

    original = limits_module.ANTHROPIC_MODEL
    limits_module.ANTHROPIC_MODEL = model
    try:
        return anthropic_limit_headers()
    finally:
        limits_module.ANTHROPIC_MODEL = original


def set_model_in_env(model: str) -> None:
    """Write ANTHROPIC_MODEL to the env file, replacing any existing line."""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.is_file() else ""

    lines = existing.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        is_model_line = line.startswith("ANTHROPIC_MODEL=")
        if is_model_line:
            lines[index] = f"ANTHROPIC_MODEL={model}"
            replaced = True
    if not replaced:
        lines.append(f"ANTHROPIC_MODEL={model}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Persisted ANTHROPIC_MODEL=%s to %s", model, ENV_FILE)
