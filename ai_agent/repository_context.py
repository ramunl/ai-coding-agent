"""Sample and search the active repository with bounded output for prompts."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

from ai_agent.projects import active_project

logger = logging.getLogger(__name__)

SAMPLE_FILE_LIMIT = 50
SEARCH_MAX_FILES = 80
SEARCH_MAX_MATCHES = 80
MATCH_LINE_LENGTH = 220

SOURCE_EXTENSIONS = {
    ".kt",
    ".kts",
    ".java",
    ".xml",
    ".gradle",
    ".properties",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rb",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".php",
    ".swift",
    ".m",
    ".sh",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
}

EXCLUDED_DIR_NAMES = {
    "build",
    ".gradle",
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "dist",
    "target",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}


_NOISY_SEARCH_TERMS = {
    "the",
    "and",
    "for",
    "with",
    "should",
    "current",
    "when",
    "user",
    "new",
    "bug",
    "fix",
    "active",
}


def _source_paths(repo_path: Path) -> Iterator[Path]:
    """Yield source files outside generated and dependency directories."""
    for path in repo_path.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_EXTENSIONS:
            continue
        if not EXCLUDED_DIR_NAMES.intersection(path.relative_to(repo_path).parts):
            yield path


def repo_file_sample() -> str:
    """Sample source paths from the active repository, excluding build artifacts."""
    repo_path = active_project().repo_path
    files = []
    for path in _source_paths(repo_path):
        files.append(str(path.relative_to(repo_path)))
        if len(files) >= SAMPLE_FILE_LIMIT:
            break
    return "\n".join(files)


def _matching_source_lines(
    path: Path, relative: str, terms: set[str], limit: int
) -> list[str]:
    """Collect bounded line excerpts whose path or content matches search terms."""
    if limit <= 0:
        return []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError as error:
        logger.warning("Skipping unreadable source file %s: %s", path, error)
        return []
    name_matches = any(term in relative.lower() for term in terms)
    matches = []
    for line_number, line in enumerate(lines, start=1):
        if not (name_matches or any(term in line.lower() for term in terms)):
            continue
        stripped = line.strip()
        if stripped:
            matches.append(f"{relative}:{line_number}: {stripped[:MATCH_LINE_LENGTH]}")
        if len(matches) >= limit:
            break
    return matches


def codebase_search_context(
    query: str,
    max_files: int = SEARCH_MAX_FILES,
    max_matches: int = SEARCH_MAX_MATCHES,
) -> str:
    """Return bounded source paths and excerpts for codebase-first bug triage."""
    repo_path = active_project().repo_path
    if not repo_path.exists():
        return f"Repository path does not exist: {repo_path}"
    terms = set(re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", query.lower()))
    terms -= _NOISY_SEARCH_TERMS
    files: list[str] = []
    matches: list[str] = []
    for path in _source_paths(repo_path):
        relative = str(path.relative_to(repo_path))
        files.append(relative)
        matches.extend(
            _matching_source_lines(path, relative, terms, max_matches - len(matches))
        )
        if len(files) >= max_files and len(matches) >= max_matches:
            break
    return (
        "Files:\n"
        + "\n".join(files[:max_files])
        + "\n\nRelevant matches:\n"
        + "\n".join(matches[:max_matches])
    )
