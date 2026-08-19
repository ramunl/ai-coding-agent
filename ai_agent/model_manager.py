"""View and switch the Claude model from Telegram.

Switching writes ANTHROPIC_MODEL to the env file and restarts the service.
The footgun is obvious: a bad string persisted to the boot env file would
crash every start. The guardrail: a candidate model is probed against the
live API and only written if it responds. An invalid string is rejected at
the command and never reaches disk.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from ai_agent.anthropic_limits import anthropic_limit_headers
from ai_agent.config import ANTHROPIC_KEY, ANTHROPIC_MODEL, ANTHROPIC_VERSION, COMMAND_TIMEOUT_SECONDS
from ai_agent.model_errors import is_model_not_found

logger = logging.getLogger(__name__)

ENV_FILE = Path(os.environ.get("AGENT_ENV_FILE", "/etc/ai-agent/ai-agent.env"))
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"

# Conservative: matches Anthropic model strings without allowing shell-unsafe
# characters into a file the service sources at boot.
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def active_model() -> str:
    return ANTHROPIC_MODEL


def list_models() -> tuple[bool, list[dict[str, str]] | str]:
    """List models this API key can use. Returns (True, models) or (False, detail)."""
    headers = {
        "anthropic-version": ANTHROPIC_VERSION,
        "x-api-key": ANTHROPIC_KEY,
    }
    models: list[dict[str, str]] = []
    after_id = None
    while True:
        url = ANTHROPIC_MODELS_URL
        if after_id:
            url += f"?after_id={after_id}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=COMMAND_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            return False, f"HTTP {exc.code}: {detail[:200]}"

        try:
            page = json.loads(body)
        except json.JSONDecodeError:
            return False, "Unexpected response from the API."

        models.extend(
            {"id": item.get("id", ""), "display_name": item.get("display_name") or item.get("id", "")}
            for item in page.get("data", [])
        )

        after_id = page.get("last_id") if page.get("has_more") else None
        if not after_id:
            break

    return True, models


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
