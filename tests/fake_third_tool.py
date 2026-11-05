"""A test-only third tool proving new providers need no central dispatch edits.

Registering this tool routes planning, implementation, image delivery, limits
and auth through the ``ai_tools`` registry. No dispatch list in
``planning_provider``, ``workflow``, ``image_delivery``, ``provider_limits``,
``tool_auth`` or ``inbox`` has to know it exists.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from ai_agent.ai_tools import AITool, register_tool, unregister_tool
from ai_agent.tool_auth_providers import Reading

NAME = "fake-tool"
LIMITS_KEY = "fake_tool"


async def fake_auth() -> Reading:
    """Probe auth without running a subprocess."""
    return Reading("authenticated", "oauth")


class FakeTool(AITool):
    """A complete third tool: planner, executor, images, limits and auth."""

    name = NAME
    label = "Fake"
    manageable = False
    roles = ("planner", "executor")
    images = True
    order = 3
    limits_key = LIMITS_KEY
    # partial, not a plain function: a class-level callable would otherwise bind
    # as a method and receive ``self`` when read through an instance.
    auth = functools.partial(fake_auth)
    tokens = None

    def planner(self, prompt: str, schema_name: str, max_tokens: int) -> str:
        """Return a marker proving the registry dispatched here."""
        return f"fake-plan:{schema_name}"

    def command(self, prompt: str) -> list[str]:
        """Return a marker command proving the registry dispatched here."""
        return [NAME, "run", prompt]

    def image_run(
        self,
        command: list[str],
        run: Callable,
        *,
        images: list[tuple[str, bytes]],
        missing: int,
        **kwargs: Any,
    ) -> Any:
        """Return a marker proving the registry dispatched here."""
        return ("fake-image", len(images), missing)

    def limits(self) -> dict:
        """Return a marker reading proving the registry dispatched here."""
        return {"status": "ok", "windows": []}


def install() -> None:
    """Register the fake tool under its canonical name."""
    register_tool(NAME, FakeTool())


def remove() -> None:
    """Forget the fake tool so the shared registry stays clean."""
    unregister_tool(NAME)
