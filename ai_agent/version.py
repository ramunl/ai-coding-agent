from pathlib import Path

from ai_agent.shell import run


ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT_DIR / "VERSION"


def get_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


def get_git_branch() -> str:
    try:
        result = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT_DIR)
        return result.output.strip()
    except Exception:
        return "unknown"


def get_git_commit() -> str:
    try:
        result = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT_DIR)
        return result.output.strip()
    except Exception:
        return "unknown"


def get_runtime_version() -> str:
    return (
        f"ai_agent v{get_version()}\n"
        f"branch: {get_git_branch()}\n"
        f"commit: {get_git_commit()}"
    )
