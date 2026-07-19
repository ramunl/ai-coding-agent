import logging
from pathlib import Path

from ai_agent.config import (
    RULES_ENABLED,
    RULES_REPO_PATH,
    RULES_REPO_URL,
)
from ai_agent.projects import active_project
from ai_agent.shell import run


logger = logging.getLogger(__name__)


def sync_rules() -> bool:
    """Ensure the rules repo is present and up to date.

    Returns True when rules are available locally after the call.
    Never raises: a rules-sync problem must not break planning.
    """
    if not RULES_ENABLED:
        logger.info("Rules injection disabled; skipping sync")
        return False

    repo_present = (RULES_REPO_PATH / ".git").is_dir()
    if repo_present:
        try:
            run(["git", "pull", "--ff-only", "origin", "main"], cwd=RULES_REPO_PATH)
            return True
        except RuntimeError as error:
            logger.warning("Could not pull rules repo, using cached copy: %s", error)
            return RULES_REPO_PATH.is_dir()

    parent = RULES_REPO_PATH.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["git", "clone", RULES_REPO_URL, str(RULES_REPO_PATH)], cwd=parent)
        return True
    except RuntimeError as error:
        logger.warning("Could not clone rules repo; proceeding without rules: %s", error)
        return False


def _markdown_files() -> list[Path]:
    """Global rule files plus the current project's rule files."""
    globals_dir = RULES_REPO_PATH / "global"
    project_dir = RULES_REPO_PATH / "projects" / active_project().rules_project

    files: list[Path] = []
    for directory in (globals_dir, project_dir):
        directory_present = directory.is_dir()
        if directory_present:
            files.extend(sorted(directory.rglob("*.md")))
        else:
            logger.info("Rules directory absent (skipped): %s", directory)
    return files


def load_rules_text() -> str:
    """Return all applicable rules as a single markdown string.

    Empty string means "no rules to inject" - callers should treat that as
    a normal, non-error condition.
    """
    if not RULES_ENABLED:
        return ""

    available = sync_rules()
    if not available:
        return ""

    sections: list[str] = []
    for path in _markdown_files():
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            logger.warning("Could not read rule file %s: %s", path, error)
            continue
        has_content = bool(content)
        if has_content:
            relative = path.relative_to(RULES_REPO_PATH)
            sections.append(f"# From {relative}\n{content}")

    joined = "\n\n".join(sections)
    logger.info("Loaded %d rule file(s), %d chars", len(sections), len(joined))
    return joined


def rules_prompt_block() -> str:
    """A ready-to-embed prompt block, or empty string when there are no rules."""
    rules_text = load_rules_text()
    has_rules = bool(rules_text)
    if has_rules:
        return (
            "\nMANDATORY CODING RULES - the plan and all code must follow these. "
            "If a rule cannot be followed, note why in 'risks':\n"
            f"{rules_text}\n"
        )
    return ""
