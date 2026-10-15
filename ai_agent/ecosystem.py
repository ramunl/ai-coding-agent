"""The map of the owner's repositories, so a plan lands in the right ones.

A feature often belongs to another repository than the active one, or to
several (a dashboard screen plus the agent that stores its data). The planner
gets every registered repository with a one-line description (the first
paragraph of its AGENTS.md, else its README) and, when present, the hand-
written map `ecosystem.md` at the root of the ai-rules repository, which says
who owns what. It then lists the repositories its plan changes, in order.
"""

from __future__ import annotations

import logging

from ai_agent.config import RULES_REPO_PATH, redact_sensitive
from ai_agent.project_brief import project_brief
from ai_agent.projects import active_project, list_projects

logger = logging.getLogger(__name__)

MAP_FILE = "ecosystem.md"
MAP_LIMIT = 4000
SUMMARY_LIMIT = 300
MAX_REPOSITORIES = 5


def summary_line(text: str) -> str:
    """The first paragraph of a markdown brief, as one bounded line."""
    paragraph: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("<!--"):
            if paragraph:
                break
            continue
        if not stripped:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    summary = " ".join(paragraph)
    if len(summary) > SUMMARY_LIMIT:
        summary = summary[: SUMMARY_LIMIT - 1].rstrip() + "…"
    return summary


def _map_text() -> str:
    path = RULES_REPO_PATH / MAP_FILE
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > MAP_LIMIT:
        text = text[:MAP_LIMIT].rstrip() + "\n[… cut]"
    return redact_sensitive(text)


def ecosystem_block() -> str:
    """A prompt block listing the repositories; "" when there is only one."""
    try:
        projects = list_projects()
        active = active_project().name
    except Exception as error:  # a broken registry must not stop planning
        logger.warning("No repository map: %s", error)
        return ""
    if len(projects) < 2:
        return ""
    lines = [
        "",
        "REPOSITORIES you can plan for (the active one is marked). A feature "
        "belongs to the repository that owns that screen, command or data, "
        "which may not be the active one; it may need several:",
    ]
    for project in projects:
        mark = " (active)" if project.name == active else ""
        summary = summary_line(project_brief(project.repo_path)[1])
        lines.append(f"- {project.name}{mark}: {summary or project.github_repository}")
    owner_map = _map_text()
    if owner_map:
        lines.extend(["", f"Who owns what ({MAP_FILE}):", owner_map])
    return "\n".join(lines) + "\n"


def plan_repositories(value: object) -> list[dict[str, str]]:
    """The plan's "repositories", normalized: [{"name", "changes"}], bounded."""
    if not isinstance(value, list):
        return []
    repositories: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        changes = " ".join(str(item.get("changes") or "").split())
        if not name or name in seen:
            continue
        seen.add(name)
        repositories.append({"name": name[:100], "changes": changes[:500]})
        if len(repositories) == MAX_REPOSITORIES:
            break
    return repositories


def repository_note(repositories: list[dict[str, str]], active: str) -> str:
    """What the owner must know before approving; "" for a one-repository plan."""
    names = [repository["name"] for repository in repositories]
    if not names or names == [active]:
        return ""
    if active not in names and len(names) == 1:
        return (
            f"This plan is for {names[0]}, not {active}, the active project. Move "
            f"it to {names[0]} (/split), where it is planned again, or revise it."
        )
    if active not in names:
        return (
            f"This plan does not change {active}, the active project. Split it "
            "into tasks (/split), which plan each repository in turn, or revise "
            "it."
        )
    others = ", ".join(name for name in names if name != active)
    return (
        f"Approving runs only {active}; {others} would need their own task. "
        "Or split it into one task per repository, in this order: /split "
        "(Split into tasks in the dashboard)."
    )
