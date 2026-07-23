import os
import shlex
from pathlib import Path


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("YOUR_CHAT_ID", "0"))
REPO_PATH = Path(os.environ.get("REPO_PATH", "~/your-android-repo")).expanduser()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
PLANNING_AGENT = os.environ.get("PLANNING_AGENT", "codex").strip().lower()
IMPLEMENTATION_AGENT = os.environ.get("IMPLEMENTATION_AGENT", "codex").strip().lower()
CLAUDE_CODE_ARGS = tuple(shlex.split(os.environ.get("CLAUDE_CODE_ARGS", "--permission-mode acceptEdits")))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "owner/repo")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")
PROJECTS_FILE = Path(os.environ.get("PROJECTS_FILE", "/etc/ai-agent-projects.json")).expanduser()
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", "/opt/projects")).expanduser()
RULES_ENABLED = os.environ.get("RULES_ENABLED", "true").strip().lower() not in {"false", "0", "no"}
RULES_REPO_PATH = Path(os.environ.get("RULES_REPO_PATH", "/opt/ai-rules")).expanduser()
RULES_REPO_URL = os.environ.get("RULES_REPO_URL", "git@github.com:ramunl/ai-rules.git")
RULES_PROJECT_NAME = os.environ.get("RULES_PROJECT_NAME", "channel-cast")
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("COMMAND_TIMEOUT_SECONDS", "120"))
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1800"))
CI_POLL_INTERVAL_SECONDS = int(os.environ.get("CI_POLL_INTERVAL_SECONDS", "30"))
CI_TIMEOUT_SECONDS = int(os.environ.get("CI_TIMEOUT_SECONDS", "1800"))
CI_FIX_ATTEMPTS = int(os.environ.get("CI_FIX_ATTEMPTS", "3"))
MAX_TELEGRAM_MESSAGE_LENGTH = 3900
MAX_LOG_LINES = 120
GITHUB_API_URL = "https://api.github.com"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
LINK_ALLOWED_DOMAINS = tuple(
    domain.strip().lower()
    for domain in os.environ.get(
        "LINK_ALLOWED_DOMAINS",
        "developer.android.com,docs.github.com,kotlinlang.org,stackoverflow.com",
    ).split(",")
    if domain.strip()
)


def validate_required_config() -> None:
    missing = [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "YOUR_CHAT_ID")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    implementation_agent = os.environ.get("IMPLEMENTATION_AGENT", "codex").strip().lower()
    if implementation_agent not in {"codex", "claude"}:
        raise RuntimeError("IMPLEMENTATION_AGENT must be codex or claude")
    planning_agent = os.environ.get("PLANNING_AGENT", "codex").strip().lower()
    if planning_agent not in {"codex", "claude"}:
        raise RuntimeError("PLANNING_AGENT must be codex or claude")
    if planning_agent == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required when PLANNING_AGENT=claude")


def redact_sensitive(text: str) -> str:
    redacted = text
    for secret in (TELEGRAM_TOKEN, ANTHROPIC_KEY, GITHUB_TOKEN):
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
