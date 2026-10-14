"""The project brief: what the repository is, for every planning prompt.

Each repository can keep an AGENTS.md (Codex reads it by itself; a CLAUDE.md
that points to it does the same for Claude Code). The planner only sees what
is in its prompt, so this module puts that brief there too: AGENTS.md first,
then CLAUDE.md, else the start of the README. Bounded and redacted.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ai_agent.config import redact_sensitive
from ai_agent.projects import active_project

logger = logging.getLogger(__name__)

BRIEF_FILES = ("AGENTS.md", "CLAUDE.md")
README_FILES = ("README.md", "README.rst", "README.txt", "README")
BRIEF_LIMIT = 8000
README_LIMIT = 3000
# A CLAUDE.md that only points elsewhere ("@AGENTS.md", "See AGENTS.md").
_POINTER_ONLY = re.compile(r"^\W*(?:see\s+|read\s+)?@?AGENTS\.md\W*$", re.IGNORECASE)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as error:
        logger.warning("Could not read %s: %s", path, error)
        return ""


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[… cut; read the file for the rest]"


def project_brief(repo_path: Path) -> tuple[str, str]:
    """(file name, text) of the repository's brief; ("", "") if it has none."""
    for name in BRIEF_FILES:
        text = _read(repo_path / name)
        if text and not _POINTER_ONLY.match(text):
            return name, _bounded(redact_sensitive(text), BRIEF_LIMIT)
    for name in README_FILES:
        text = _read(repo_path / name)
        if text:
            return name, _bounded(redact_sensitive(text), README_LIMIT)
    return "", ""


def project_brief_block() -> str:
    """A ready-to-embed prompt block, or "" when the repository has no brief."""
    try:
        repo_path = active_project().repo_path
    except Exception as error:  # no active project: plan without a brief
        logger.warning("No project brief: %s", error)
        return ""
    name, text = project_brief(repo_path)
    if not text:
        return ""
    if name in BRIEF_FILES:
        intro = f"PROJECT BRIEF (the repository's {name})"
    else:
        intro = f"PROJECT BACKGROUND (start of {name}; the repository has no AGENTS.md)"
    return (
        f"\n{intro} - what this project is and how it is built. Plan within "
        "it; the coding rules below take priority:\n"
        f"{text}\n"
    )
