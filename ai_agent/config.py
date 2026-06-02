import os
from pathlib import Path


TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["YOUR_CHAT_ID"])
REPO_PATH = Path(os.environ.get("REPO_PATH", "~/your-android-repo")).expanduser()
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "ramunl/com.randrgames.channelcast")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")
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


def redact_sensitive(text: str) -> str:
    redacted = text
    for secret in (TELEGRAM_TOKEN, ANTHROPIC_KEY, GITHUB_TOKEN):
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
