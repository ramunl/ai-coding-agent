"""Discover Codex models and supply an optional agent-owned CLI override."""

from __future__ import annotations

import asyncio
import os
import re

from ai_agent.codex_limits import read_codex_response

DEFAULT_MODEL = "default"
_MODEL = re.compile(r"[A-Za-z0-9._-]{1,100}")


def current_model() -> str:
    """Return the configured override; default leaves CLI configuration intact."""
    return os.environ.get("CODEX_MODEL", "").strip() or DEFAULT_MODEL


def model_args() -> list[str]:
    """Pass the override explicitly to both planning and implementation."""
    model = current_model()
    return [] if model == DEFAULT_MODEL else ["--model", model]


async def _catalog() -> list[dict[str, str]]:
    models = [{"id": DEFAULT_MODEL, "display_name": "CLI default"}]
    cursor = None
    # Bound pagination even if a broken CLI repeats a cursor forever.
    for _ in range(10):
        page = await read_codex_response(
            "model/list", {"limit": 100, "includeHidden": False, "cursor": cursor}
        )
        for entry in page["data"]:
            model = entry.get("model") or entry.get("id")
            if (
                isinstance(model, str)
                and _MODEL.fullmatch(model)
                and not entry.get("hidden")
            ):
                models.append(
                    {"id": model, "display_name": entry.get("displayName") or model}
                )
        cursor = page.get("nextCursor")
        if not cursor:
            return list({model["id"]: model for model in models}.values())
    raise ValueError("Too many model pages")


def list_models() -> tuple[bool, list[dict[str, str]] | str]:
    """Read a bounded catalog without sending prompts or publishing raw errors."""
    try:
        return True, asyncio.run(asyncio.wait_for(_catalog(), timeout=25))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False, "Codex model list unavailable; check the server CLI and login."


def verify_model(model: str) -> tuple[bool, str]:
    """Validate against the catalog, without a paid inference probe."""
    if model == DEFAULT_MODEL:
        return True, "CLI default"
    if not _MODEL.fullmatch(model):
        return False, "an invalid model identifier"
    ok, models = list_models()
    if not ok:
        return False, str(models)
    if any(entry["id"] == model for entry in models):
        return True, "listed by the server's Codex CLI"
    return False, "not listed by the server's Codex CLI"
