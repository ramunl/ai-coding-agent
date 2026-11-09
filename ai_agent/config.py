"""Load agent settings and validate required environment values."""

import os
import re
import shlex
from pathlib import Path

_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "kb": 1_000,
    "mb": 1_000_000,
    "gb": 1_000_000_000,
    "kib": 1_024,
    "mib": 1_024**2,
    "gib": 1_024**3,
}
_SIZE = re.compile(r"([0-9]+)\s*([a-z]*)")


def parse_size(text: str) -> int:
    """Return a byte count from plain digits or a unit such as 5GB or 500MiB."""
    match = _SIZE.fullmatch(text.strip().lower())
    if not match or match.group(2) not in _SIZE_UNITS:
        raise ValueError(f"Invalid size {text!r}; use bytes or a unit like 5GB.")
    return int(match.group(1)) * _SIZE_UNITS[match.group(2)]


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("YOUR_CHAT_ID", "0"))
REPO_PATH = Path(os.environ.get("REPO_PATH", "~/your-android-repo")).expanduser()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
PLANNING_AGENT = os.environ.get("PLANNING_AGENT", "codex").strip().lower()
# How the Claude planner runs: "code" = Claude Code on the Claude subscription
# (no API key needed), "api" = the Anthropic API with ANTHROPIC_API_KEY.
CLAUDE_PLANNER_VIA = os.environ.get("CLAUDE_PLANNER_VIA", "code").strip().lower()
# Claude Code's model for planning; empty = Claude Code's own default.
CLAUDE_PLANNER_MODEL = os.environ.get("CLAUDE_PLANNER_MODEL", "").strip()
# "subscription" (default): Claude Code runs on the claude.ai login, with the
# API key kept out of its environment (claude_env). "api_key": it may use it.
CLAUDE_CODE_AUTH = os.environ.get("CLAUDE_CODE_AUTH", "subscription").strip().lower()
# Native DeepSeek API planner (OpenAI-compatible Chat Completions). The
# autonomous worker gate (AUTONOMOUS_DEEPSEEK_ENABLED) stays false until the
# owner switches it on; manual /plan deepseek is unaffected.
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
).rstrip("/")
# deepseek-v4-pro is the default; DEEPSEEK_FLASH_MODEL is the cheaper option.
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()
DEEPSEEK_FLASH_MODEL = os.environ.get(
    "DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash"
).strip()
DEEPSEEK_TIMEOUT_SECONDS = int(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "120"))
DEEPSEEK_MAX_OUTPUT_TOKENS = int(os.environ.get("DEEPSEEK_MAX_OUTPUT_TOKENS", "8192"))
DEEPSEEK_MAX_OUTPUT_CHARS = int(os.environ.get("DEEPSEEK_MAX_OUTPUT_CHARS", "20000"))
if DEEPSEEK_TIMEOUT_SECONDS <= 0:
    raise ValueError("DEEPSEEK_TIMEOUT_SECONDS must be positive")
if DEEPSEEK_MAX_OUTPUT_TOKENS <= 0:
    raise ValueError("DEEPSEEK_MAX_OUTPUT_TOKENS must be positive")
if DEEPSEEK_MAX_OUTPUT_CHARS <= 0:
    raise ValueError("DEEPSEEK_MAX_OUTPUT_CHARS must be positive")
# OpenCode CLI executor (owner-gated). The provider stays disabled in the
# registry until the owner approves a pinned release (recorded by
# scripts/opencode_characterize.sh); the DeepSeek model is read-only here and
# the runner's captured JSON output is bounded by OPENCODE_MAX_OUTPUT_CHARS.
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "deepseek/deepseek-chat").strip()
OPENCODE_MAX_OUTPUT_CHARS = int(os.environ.get("OPENCODE_MAX_OUTPUT_CHARS", "200000"))
if OPENCODE_MAX_OUTPUT_CHARS <= 0:
    raise ValueError("OPENCODE_MAX_OUTPUT_CHARS must be positive")
# Bounded live repository context for the tool-free API planner. When the
# repository has no usable evidence, PLANNER_CONTEXT_ESCALATION="codex" routes
# the plan to the Codex CLI (which has repository tools) instead of failing;
# the default "off" surfaces the explicit context error.
PLANNER_CONTEXT_MAX_CHARS = int(os.environ.get("PLANNER_CONTEXT_MAX_CHARS", "12000"))
PLANNER_CONTEXT_SCAN_SECONDS = int(os.environ.get("PLANNER_CONTEXT_SCAN_SECONDS", "3"))
PLANNER_CONTEXT_ESCALATION = (
    os.environ.get("PLANNER_CONTEXT_ESCALATION", "off").strip().lower()
)
IMPLEMENTATION_AGENT = os.environ.get("IMPLEMENTATION_AGENT", "codex").strip().lower()
CLAUDE_CODE_ARGS = tuple(
    shlex.split(os.environ.get("CLAUDE_CODE_ARGS", "--permission-mode acceptEdits"))
)
# Shell commands Claude Code may run without asking. In `claude -p` nothing can
# answer a permission prompt, so anything not listed here is refused: tests,
# lint and read-only CI/PR lookups, never deploys, pushes or package installs.
# The pytest/ruff/baseline entries are the documented verification commands;
# keep them in sync with ai_agent.preflight.VERIFICATION_COMMANDS or a new
# required command will false-block dispatch there.
DEFAULT_CLAUDE_CODE_ALLOWED_TOOLS = (
    "Bash(pytest:*)",
    "Bash(python -m pytest:*)",
    "Bash(python3 -m pytest:*)",
    "Bash(ruff check:*)",
    "Bash(ruff format:*)",
    "Bash(scripts/baseline.sh:*)",
    "Bash(npm test:*)",
    "Bash(node --test:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(gh run list:*)",
    "Bash(gh run view:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr checks:*)",
)
CLAUDE_CODE_ALLOWED_TOOLS = tuple(
    tool.strip()
    for tool in os.environ.get(
        "CLAUDE_CODE_ALLOWED_TOOLS", ",".join(DEFAULT_CLAUDE_CODE_ALLOWED_TOOLS)
    ).split(",")
    if tool.strip()
)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "owner/repo")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")
PROJECTS_FILE = Path(
    os.environ.get("PROJECTS_FILE", "/etc/ai-agent-projects.json")
).expanduser()
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", "/opt/projects")).expanduser()
# Restart-safe bot state (task queue, pending plan, provider choices).
STATE_FILE = Path(
    os.environ.get("AGENT_STATE_FILE", "/var/lib/ai-coding-agent/state.json")
).expanduser()
# Durable context archive, separate from the pruned task queue (CTX01).
CONTEXT_ROOT = Path(
    os.environ.get("CONTEXT_ROOT", "/var/lib/ai-coding-agent/context")
).expanduser()
# Read model for the dashboard service (a separate process). Secret-free.
SNAPSHOT_FILE = Path(
    os.environ.get("AGENT_SNAPSHOT_FILE", "/var/lib/ai-coding-agent/snapshot.json")
).expanduser()
# Daily token counts by role and tool; counts only, 90 days (see token_usage.py).
TOKEN_USAGE_PATH = Path(
    os.environ.get("TOKEN_USAGE_PATH", "/var/lib/ai-coding-agent/token-usage.json")
).expanduser()
# Versioned per-call usage details (see usage_details.py). Secret-free in the
# snapshot; the store is bounded by USAGE_DETAILS_MAX_RECORDS and the
# configurable USAGE_DETAILS_RETENTION_DAYS UTC window.
USAGE_DETAILS_PATH = Path(
    os.environ.get("USAGE_DETAILS_PATH", "/var/lib/ai-coding-agent/usage-details.json")
).expanduser()
_usage_details_retention = int(os.environ.get("USAGE_DETAILS_RETENTION_DAYS", "90"))
if _usage_details_retention < 1:
    raise ValueError("USAGE_DETAILS_RETENTION_DAYS must be at least 1")
USAGE_DETAILS_RETENTION_DAYS = _usage_details_retention
_usage_details_max = int(os.environ.get("USAGE_DETAILS_MAX_RECORDS", "5000"))
if _usage_details_max < 1:
    raise ValueError("USAGE_DETAILS_MAX_RECORDS must be at least 1")
USAGE_DETAILS_MAX_RECORDS = _usage_details_max
# Setup requests from the dashboard, and their results (see ai_agent/inbox.py).
INBOX_DIR = Path(
    os.environ.get("AGENT_INBOX_DIR", "/var/lib/ai-coding-agent/inbox")
).expanduser()
# Private attachments must never be stored in a repository.
IMAGE_DIR = Path(
    os.environ.get("IMAGE_DIR", "/var/lib/ai-coding-agent/images")
).expanduser()
# Sizes accept plain bytes or units (e.g. 5GB, 500MiB); see parse_size.
IMAGE_STORAGE_CAP_BYTES = parse_size(os.environ.get("IMAGE_STORAGE_CAP_BYTES", "5GB"))
IMAGE_MAX_BYTES = parse_size(os.environ.get("IMAGE_MAX_BYTES", "10MB"))
IMAGE_MAX_PER_TASK = int(os.environ.get("IMAGE_MAX_PER_TASK", "5"))
# DS12: disposable worktrees stay separate from the production checkout.
# After explicit Ops activation, provider subprocesses drop to the dedicated
# worker, which owns its sandbox and HOME/cache. The controller is not migrated.
# Account, tool and credential migration is an explicit Ops rollout, not a
# side effect of upgrading application code on an existing root service.
WORKER_ISOLATION_ENABLED = os.environ.get(
    "WORKER_ISOLATION_ENABLED", "false"
).strip().lower() in {"true", "1", "yes"}
WORKER_USER = os.environ.get("WORKER_USER", "ai-coding-runner").strip()
WORKER_GROUP = os.environ.get("WORKER_GROUP", WORKER_USER).strip()
WORK_ROOT = Path(
    os.environ.get("WORK_ROOT", "/var/lib/ai-coding-agent/work")
).expanduser()
WORKER_HOME = Path(
    os.environ.get("WORKER_HOME", "/var/lib/ai-coding-agent/home")
).expanduser()

_WORKER_NAME = re.compile(r"[a-z_][a-z0-9_-]*\$?")


def _outside_git(path: Path) -> bool:
    """Whether a path (and none of its parents) sits inside a repository."""
    resolved = path.resolve()
    return not any(
        (parent / ".git").exists() for parent in (resolved, *resolved.parents)
    )


def validate_work_config() -> None:
    """Reject unsafe worker identities and sandbox roots."""
    invalid_user = (
        not WORKER_USER
        or WORKER_USER == "root"
        or not _WORKER_NAME.fullmatch(WORKER_USER)
    )
    if invalid_user:
        raise RuntimeError("WORKER_USER must be a non-root POSIX username")
    if not WORKER_GROUP or not _WORKER_NAME.fullmatch(WORKER_GROUP):
        raise RuntimeError("WORKER_GROUP must be a POSIX group name")
    if not WORK_ROOT.is_absolute() or not _outside_git(WORK_ROOT):
        raise RuntimeError("WORK_ROOT must be absolute and outside git")
    if not WORKER_HOME.is_absolute() or not _outside_git(WORKER_HOME):
        raise RuntimeError("WORKER_HOME must be absolute and outside git")
    if WORKER_ISOLATION_ENABLED:
        import grp
        import pwd

        try:
            worker = pwd.getpwnam(WORKER_USER)
            group = grp.getgrnam(WORKER_GROUP)
        except KeyError as error:
            raise RuntimeError(
                "Worker isolation enabled but worker account/group is missing; "
                "complete the Ops migration before enabling it"
            ) from error
        if worker.pw_uid == 0 or group.gr_gid == 0:
            raise RuntimeError("Worker isolation requires non-root user and group IDs")


def validate_image_config() -> None:
    """Reject unsafe directories and unbounded image intake settings."""
    if not IMAGE_DIR.is_absolute():
        raise RuntimeError("IMAGE_DIR must be absolute and outside git")
    resolved = IMAGE_DIR.resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise RuntimeError("IMAGE_DIR must be outside git")
    if not 0 < IMAGE_MAX_BYTES <= 10_000_000:
        raise RuntimeError("IMAGE_MAX_BYTES must be between 1 and 10000000")
    if not 0 < IMAGE_MAX_PER_TASK <= 5:
        raise RuntimeError("IMAGE_MAX_PER_TASK must be between 1 and 5")
    if IMAGE_STORAGE_CAP_BYTES < IMAGE_MAX_BYTES:
        raise RuntimeError("IMAGE_STORAGE_CAP_BYTES must cover IMAGE_MAX_BYTES")


ACTION_RESULTS_FILE = Path(
    os.environ.get(
        "AGENT_ACTION_RESULTS_FILE", "/var/lib/ai-coding-agent/action-results.json"
    )
).expanduser()
CLAUDE_CODE_LIMITS_FILE = Path(
    os.environ.get(
        "CLAUDE_CODE_LIMITS_FILE",
        str(SNAPSHOT_FILE.with_name("claude-code-limits.json")),
    )
).expanduser()
RULES_ENABLED = os.environ.get("RULES_ENABLED", "true").strip().lower() not in {
    "false",
    "0",
    "no",
}
# Off by default: the autonomous worker may pick DeepSeek only after the CTX07
# continuity trial and the CTX08 real backup/isolated-restore gate pass. Manual
# /plan with DeepSeek is unaffected; this is consulted only in ai_agent.worker.
AUTONOMOUS_DEEPSEEK_ENABLED = os.environ.get(
    "AUTONOMOUS_DEEPSEEK_ENABLED", "false"
).strip().lower() not in {"false", "0", "no"}
# Off by default: the autonomous worker may pick the OpenCode executor only
# after the owner approves a pinned release and enables the provider. Manual
# runs are unaffected; this is consulted only in ai_agent.worker.
AUTONOMOUS_OPENCODE_ENABLED = os.environ.get(
    "AUTONOMOUS_OPENCODE_ENABLED", "false"
).strip().lower() not in {"false", "0", "no"}
RULES_REPO_PATH = Path(os.environ.get("RULES_REPO_PATH", "/opt/ai-rules")).expanduser()
RULES_REPO_URL = os.environ.get("RULES_REPO_URL", "git@github.com:ramunl/ai-rules.git")
RULES_PROJECT_NAME = os.environ.get("RULES_PROJECT_NAME", "channel-cast")
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("COMMAND_TIMEOUT_SECONDS", "120"))
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1800"))
_implementation_timeout = int(
    os.environ.get("IMPLEMENTATION_TIMEOUT_SECONDS", str(CODEX_TIMEOUT_SECONDS))
)
if _implementation_timeout < 0:
    raise ValueError("IMPLEMENTATION_TIMEOUT_SECONDS must be zero or positive")
IMPLEMENTATION_TIMEOUT_SECONDS = _implementation_timeout or None
# Identical structured permission denials tolerated inside one attempt before a
# denial-loop blocker stops the paid run. 0 disables detection; a negative value
# is a configuration error.
_denial_loop_max = int(os.environ.get("DENIAL_LOOP_MAX_PER_ATTEMPT", "3"))
if _denial_loop_max < 0:
    raise ValueError("DENIAL_LOOP_MAX_PER_ATTEMPT must be zero or positive")
DENIAL_LOOP_MAX_PER_ATTEMPT = _denial_loop_max
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
    """Reject missing settings needed to start the bot."""
    validate_image_config()
    validate_work_config()
    missing = [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "YOUR_CHAT_ID")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    from ai_agent.ai_tools import tools_for_role

    implementation_agent = (
        os.environ.get("IMPLEMENTATION_AGENT", "codex").strip().lower()
    )
    if implementation_agent not in tools_for_role("executor"):
        raise RuntimeError(
            f"IMPLEMENTATION_AGENT must be {' or '.join(tools_for_role('executor'))}"
        )
    planning_agent = os.environ.get("PLANNING_AGENT", "codex").strip().lower()
    if planning_agent not in tools_for_role("planner"):
        raise RuntimeError(
            f"PLANNING_AGENT must be {' or '.join(tools_for_role('planner'))}"
        )
    if planning_agent == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required when PLANNING_AGENT=claude")
    if planning_agent == "deepseek" and not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY is required when PLANNING_AGENT=deepseek")


def redact_sensitive(text: str) -> str:
    """Replace configured secrets in text before displaying it."""
    redacted = text
    for secret in (TELEGRAM_TOKEN, ANTHROPIC_KEY, GITHUB_TOKEN, DEEPSEEK_API_KEY):
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
