"""The agent's setup as the dashboard sees it: projects, AI tools, models.

Published in the snapshot so the dashboard's pickers show exactly what the
agent accepts. Changes go the other way, through ai_agent.inbox.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path

from ai_agent.actions import busy_reason
from ai_agent.ai_tools import all_info, get_tool
from ai_agent.planner import SUPPORTED_PLANNING_AGENTS, normalize_planning_agent
from ai_agent.projects import active_project, list_projects
from ai_agent.workflow import (
    SUPPORTED_IMPLEMENTATION_AGENTS,
    normalize_implementation_agent,
)

logger = logging.getLogger(__name__)

CHOICES_REFRESH_SECONDS = 1800


class ModelChoices:
    """Models each manageable tool can switch to, fetched rarely (network)."""

    def __init__(self) -> None:
        """Start empty; refresh() fills it."""
        self.choices: dict[str, list[str]] = {}
        self.errors: dict[str, str] = {}

    def refresh(self) -> None:
        """Ask every manageable tool for its models; keep the last good list."""
        for info in all_info():
            if not info.manageable:
                continue
            tool = get_tool(info.tool)
            try:
                ok, models = tool.list_models() if tool else (False, "unknown tool")
            except (OSError, RuntimeError, ValueError):
                logger.exception("Model choices refresh failed for %s", info.tool)
                ok, models = False, "Could not refresh this tool; check server logs."
            if ok:
                self.choices[info.tool] = [
                    model["id"] for model in models if model.get("id")
                ]
                self.errors.pop(info.tool, None)
            else:
                self.errors[info.tool] = str(models)

    async def refresh_forever(self, interval: float = CHOICES_REFRESH_SECONDS) -> None:
        """Refresh until cancelled; failures keep the previous list."""
        while True:
            try:
                await asyncio.to_thread(self.refresh)
            except Exception as error:
                logger.warning("Could not list models (will retry): %s", error)
            await asyncio.sleep(interval)


def files_part(choices: ModelChoices, results_file: Path) -> dict:
    """The file- and config-backed part of the setup (call off the event loop)."""
    active = active_project().name
    try:
        results = json.loads(results_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        results = []
    return {
        "projects": [
            {
                "name": project.name,
                "repository": project.github_repository,
                "active": project.name == active,
            }
            for project in list_projects()
        ],
        "models": [
            {
                "tool": info.tool,
                "model": info.model,
                "manageable": info.manageable,
                "note": info.note,
                "choices": choices.choices.get(info.tool, []),
                "choices_error": choices.errors.get(info.tool),
            }
            for info in all_info()
        ],
        "actions": results if isinstance(results, list) else [],
        "checked_at": time.time(),
    }


def state_part(user_data: Mapping) -> dict:
    """The in-memory part of the setup (call on the event loop, where it changes)."""
    return {
        "planner": normalize_planning_agent(user_data.get("planning_agent")),
        "implementer": normalize_implementation_agent(
            user_data.get("implementation_agent")
        ),
        "planner_options": list(SUPPORTED_PLANNING_AGENTS),
        "implementer_options": list(SUPPORTED_IMPLEMENTATION_AGENTS),
        "busy": busy_reason(user_data),
    }
