import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ai_agent.config import (
    GITHUB_BASE_BRANCH,
    GITHUB_REPOSITORY,
    PROJECTS_FILE,
    PROJECTS_ROOT,
    REPO_PATH,
    RULES_PROJECT_NAME,
)


logger = logging.getLogger(__name__)


DEFAULT_PROJECT_NAME = "default"


@dataclass(frozen=True)
class Project:
    name: str
    repo_path: Path
    github_repository: str
    base_branch: str
    rules_project: str


class ProjectError(RuntimeError):
    """Raised for user-correctable project problems."""


def normalize_repository(value: str) -> str:
    """Accept owner/repo, an SSH remote, or an HTTPS URL; return owner/repo."""
    text = value.strip()
    text = re.sub(r"^git@github\.com:", "", text)
    text = re.sub(r"^https?://github\.com/", "", text)
    text = re.sub(r"\.git$", "", text)
    text = text.strip("/")
    is_valid = bool(re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", text))
    if is_valid:
        return text
    raise ProjectError(f"Could not read owner/repo from: {value}")


def project_name_from_repository(repository: str) -> str:
    return repository.split("/", 1)[1]


def clone_url(repository: str) -> str:
    return f"git@github.com:{repository}.git"


def _fallback_registry() -> dict:
    """Registry synthesized from env vars when no projects file exists.

    This keeps single-project installs working with zero configuration.
    """
    name = DEFAULT_PROJECT_NAME
    has_repository = "/" in GITHUB_REPOSITORY
    if has_repository:
        name = project_name_from_repository(GITHUB_REPOSITORY)
    return {
        "active": name,
        "projects": {
            name: {
                "repo_path": str(REPO_PATH),
                "github_repository": GITHUB_REPOSITORY,
                "base_branch": GITHUB_BASE_BRANCH,
                "rules_project": RULES_PROJECT_NAME,
            }
        },
    }


def load_registry() -> dict:
    file_present = PROJECTS_FILE.is_file()
    if file_present:
        try:
            data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
            has_projects = isinstance(data, dict) and bool(data.get("projects"))
            if has_projects:
                return data
            logger.warning("Projects file has no projects; using env fallback: %s", PROJECTS_FILE)
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Could not read projects file, using env fallback: %s", error)
    else:
        logger.info("No projects file at %s; using env fallback", PROJECTS_FILE)
    return _fallback_registry()


def save_registry(registry: dict) -> None:
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECTS_FILE.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    logger.info("Saved projects registry to %s", PROJECTS_FILE)


def _to_project(name: str, entry: dict) -> Project:
    return Project(
        name=name,
        repo_path=Path(entry["repo_path"]).expanduser(),
        github_repository=entry["github_repository"],
        base_branch=entry.get("base_branch", GITHUB_BASE_BRANCH),
        # Rules folder defaults to the project name: ai-rules/projects/<name>/
        rules_project=entry.get("rules_project") or name,
    )


def list_projects() -> list[Project]:
    registry = load_registry()
    return [_to_project(name, entry) for name, entry in sorted(registry["projects"].items())]


def active_project() -> Project:
    registry = load_registry()
    name = registry.get("active", "")
    entries = registry["projects"]
    is_known = name in entries
    if not is_known:
        name = sorted(entries)[0]
        logger.warning("Active project missing from registry; falling back to %s", name)
    return _to_project(name, entries[name])


def get_project(name: str) -> Project:
    registry = load_registry()
    entries = registry["projects"]
    is_known = name in entries
    if is_known:
        return _to_project(name, entries[name])
    known = ", ".join(sorted(entries)) or "(none)"
    raise ProjectError(f"Unknown project '{name}'. Known: {known}")


def set_active(name: str) -> Project:
    registry = load_registry()
    is_known = name in registry["projects"]
    if not is_known:
        known = ", ".join(sorted(registry["projects"])) or "(none)"
        raise ProjectError(f"Unknown project '{name}'. Known: {known}")
    registry["active"] = name
    save_registry(registry)
    logger.info("Active project switched to %s", name)
    return _to_project(name, registry["projects"][name])


def add_project(repository: str, repo_path: str | None = None, base_branch: str | None = None) -> tuple[Project, bool]:
    """Register a project. Returns (project, needs_clone).

    needs_clone is True when the local path is absent, so the caller can clone
    it with the shell layer. This module stays free of shell dependencies.
    """
    normalized = normalize_repository(repository)
    name = project_name_from_repository(normalized)
    path = Path(repo_path).expanduser() if repo_path else PROJECTS_ROOT / name

    registry = load_registry()
    file_present = PROJECTS_FILE.is_file()
    is_first_real_entry = not file_present
    if is_first_real_entry:
        # Keep the env-derived project so nothing is lost on upgrade.
        logger.info("Creating projects file, preserving env-derived project")

    registry["projects"][name] = {
        "repo_path": str(path),
        "github_repository": normalized,
        "base_branch": base_branch or GITHUB_BASE_BRANCH,
        "rules_project": name,
    }
    registry.setdefault("active", name)
    save_registry(registry)

    needs_clone = not (path / ".git").is_dir()
    return _to_project(name, registry["projects"][name]), needs_clone


def remove_project(name: str) -> None:
    registry = load_registry()
    is_known = name in registry["projects"]
    if not is_known:
        raise ProjectError(f"Unknown project '{name}'.")
    is_last = len(registry["projects"]) == 1
    if is_last:
        raise ProjectError("Cannot remove the only project.")
    del registry["projects"][name]
    was_active = registry.get("active") == name
    if was_active:
        registry["active"] = sorted(registry["projects"])[0]
        logger.info("Removed active project; switched to %s", registry["active"])
    save_registry(registry)
