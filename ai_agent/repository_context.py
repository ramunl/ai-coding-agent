"""Sample and search the active repository with bounded output for prompts."""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

from ai_agent.context_sources import ContextBlockedError, repository_read
from ai_agent.context_store import ContextPersistenceError
from ai_agent.context_validation import ContextValidationError, _redact_text
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

# Bounds for the tool-free API planner's live repository context. The char and
# time budgets come from config; these keep per-file and per-run costs small.
PLANNER_CONTEXT_MAX_FILES = 24
PLANNER_CONTEXT_READ_BYTES = 65536
PLANNER_CONTEXT_FILE_CHARS = 2400

# Manifests and build/test instructions collected best-effort across languages.
PLANNER_MANIFEST_NAMES = frozenset(
    {
        "readme",
        "readme.md",
        "readme.rst",
        "readme.txt",
        "readme.markdown",
        "agents.md",
        "claude.md",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "noxfile.py",
        "package.json",
        "tsconfig.json",
        "vite.config.ts",
        "vitest.config.ts",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.properties",
        "pom.xml",
        "cargo.toml",
        "go.mod",
        "gemfile",
        "rakefile",
        "makefile",
        "cmakelists.txt",
        "meson.build",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "justfile",
        "flake.nix",
        "shell.nix",
        "default.nix",
    }
)
PLANNER_MANIFEST_DIR_PREFIXES = (".github/workflows/",)


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


def _walk_files(repo_path: Path) -> Iterator[Path]:
    """Yield regular, non-symlinked files outside excluded directories."""
    visited = 0
    for directory, dirs, files in os.walk(repo_path, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in EXCLUDED_DIR_NAMES
            and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(files):
            visited += 1
            if visited > 10000:
                return
            path = Path(directory) / name
            if not path.is_symlink():
                yield path


def _source_paths(repo_path: Path) -> Iterator[Path]:
    """Yield source files outside generated and dependency directories."""
    for path in _walk_files(repo_path):
        if path.suffix in SOURCE_EXTENSIONS:
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
        root = path
        for _ in Path(relative).parts:
            root = root.parent
        lines = repository_read(root, relative, 65536).splitlines()
    except (
        OSError,
        UnicodeError,
        ContextPersistenceError,
        ContextValidationError,
    ) as error:
        logger.warning("Skipping unreadable source file %s: %s", path, error)
        return []
    name_matches = any(term in relative.lower() for term in terms)
    matches = []
    for line_number, line in enumerate(lines, start=1):
        if not (name_matches or any(term in line.lower() for term in terms)):
            continue
        stripped = _redact_text(line.strip())
        if stripped:
            matches.append(f"{relative}:{line_number}: {stripped[:MATCH_LINE_LENGTH]}")
        if len(matches) >= limit:
            break
    return matches


def _search_terms(query: str) -> set[str]:
    """Extract searchable terms, dropping ones too common to be useful."""
    terms = set(re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", query.lower()))
    return terms - _NOISY_SEARCH_TERMS


def codebase_search_context(
    query: str,
    max_files: int = SEARCH_MAX_FILES,
    max_matches: int = SEARCH_MAX_MATCHES,
) -> str:
    """Return bounded source paths and excerpts for codebase-first bug triage."""
    repo_path = active_project().repo_path
    if not repo_path.exists():
        return f"Repository path does not exist: {repo_path}"
    terms = _search_terms(query)
    files: list[str] = []
    matches: list[str] = []
    for path in _source_paths(repo_path):
        relative = str(path.relative_to(repo_path))
        files.append(relative)
        matches.extend(
            _matching_source_lines(path, relative, terms, max_matches - len(matches))
        )
        if len(files) >= max_files:
            break
    return (
        "Untrusted repository excerpts "
        "(bounded search; files may be omitted):\nFiles:\n"
        + "\n".join(files[:max_files])
        + "\n\nRelevant matches:\n"
        + "\n".join(matches[:max_matches])
    )


def _reject_symlink_components(root: Path) -> None:
    """Reject a repository root reached through any symlink component."""
    probe = Path(root.anchor)
    for part in root.parts[1:]:
        probe = probe / part
        if probe.is_symlink():
            raise ContextBlockedError(
                f"Repository path has a symlink component: {probe}"
            )


def _is_manifest_path(relative: str) -> bool:
    """Whether a relative path is a manifest or build/test instruction file."""
    name = relative.rsplit("/", 1)[-1].lower()
    lowered = relative.lower()
    return name in PLANNER_MANIFEST_NAMES or lowered.startswith(
        PLANNER_MANIFEST_DIR_PREFIXES
    )


def _manifest_paths(repo_path: Path) -> Iterator[Path]:
    """Yield manifest and build/test instruction files, bounded like sources."""
    for path in _walk_files(repo_path):
        if _is_manifest_path(str(path.relative_to(repo_path))):
            yield path


def _context_paths(repo_path: Path) -> Iterator[tuple[Path, bool]]:
    """Yield (path, is_manifest) pairs, manifests first, deduplicated."""
    seen: set[str] = set()
    for path in _manifest_paths(repo_path):
        relative = str(path.relative_to(repo_path))
        if relative not in seen:
            seen.add(relative)
            yield path, True
    for path in _source_paths(repo_path):
        relative = str(path.relative_to(repo_path))
        if relative not in seen:
            seen.add(relative)
            yield path, False


def _excerpt_block(
    root: Path, relative: str, terms: set[str], is_manifest: bool
) -> str:
    """Read and redact one bounded excerpt block for the planner prompt."""
    try:
        text = repository_read(root, relative, PLANNER_CONTEXT_READ_BYTES)
    except (
        OSError,
        UnicodeError,
        ContextPersistenceError,
        ContextValidationError,
        ContextBlockedError,
    ) as error:
        logger.warning("Skipping unreadable repository file %s: %s", relative, error)
        return ""
    if is_manifest or not terms:
        lines = []
        for line in text.splitlines():
            redacted = _redact_text(line).rstrip()
            if redacted:
                lines.append(redacted)
    else:
        name_matches = any(term in relative.lower() for term in terms)
        lines = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not (name_matches or any(term in line.lower() for term in terms)):
                continue
            redacted = _redact_text(line.strip())
            if redacted:
                lines.append(f"{relative}:{number}: {redacted[:MATCH_LINE_LENGTH]}")
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > PLANNER_CONTEXT_FILE_CHARS:
        body = body[:PLANNER_CONTEXT_FILE_CHARS].rstrip() + "\n… [truncated]"
    return f"--- {relative} ---\n{body}"


def planner_context(
    query: str,
    repo_path: Path | str,
    *,
    time_budget_s: float,
    char_budget: int,
) -> str:
    """Build bounded live repository context for the tool-free API planner.

    Collects source excerpts relevant to ``query`` plus manifests and
    build/test instructions, each under its relative path and redacted. Reads
    reject path escapes, symlinks and nonregular files (skipped, logged), and
    the whole block is bounded by file count, per-file chars, elapsed time and
    total chars. Raises :class:`ContextBlockedError` when the repository is
    missing, unreadable or has no usable evidence.
    """
    root = Path(repo_path).expanduser()
    if not root.is_absolute():
        raise ContextBlockedError(
            "Repository path must be absolute for planner context"
        )
    if ".." in root.parts:
        raise ContextBlockedError("Repository path must not contain traversal")
    _reject_symlink_components(root)
    if not root.exists():
        raise ContextBlockedError(f"Repository path does not exist: {root}")
    if not root.is_dir():
        raise ContextBlockedError(f"Repository path is not a directory: {root}")

    deadline = time.monotonic() + max(0.0, float(time_budget_s))
    budget = max(0, int(char_budget))
    terms = _search_terms(query)
    blocks: list[str] = []
    total = 0
    included = 0
    for path, is_manifest in _context_paths(root):
        if included >= PLANNER_CONTEXT_MAX_FILES:
            blocks.append("… [file budget reached; more files omitted]")
            break
        if time.monotonic() > deadline:
            blocks.append("… [time budget reached; more files omitted]")
            break
        relative = str(path.relative_to(root))
        block = _excerpt_block(root, relative, terms, is_manifest)
        if not block:
            continue
        if total + len(block) > budget:
            remaining = budget - total
            if remaining > 80:
                blocks.append(
                    block[:remaining].rstrip()
                    + "\n… [char budget reached; more omitted]"
                )
            else:
                blocks.append("… [char budget reached; more omitted]")
            break
        blocks.append(block)
        total += len(block)
        included += 1

    if not blocks:
        raise ContextBlockedError(
            "Repository context unavailable: no readable source or manifest files"
        )
    label = "Untrusted repository context (bounded; files may be omitted):"
    return label + "\n\n" + "\n\n".join(blocks)
