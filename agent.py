import asyncio
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


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
MAX_TELEGRAM_MESSAGE_LENGTH = 3900
MAX_LOG_LINES = 120
GITHUB_API_URL = "https://api.github.com"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
logger = logging.getLogger(__name__)


def redact_sensitive(text: str) -> str:
    redacted = text
    for secret in (TELEGRAM_TOKEN, ANTHROPIC_KEY, GITHUB_TOKEN):
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    output: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    head_sha: str


@dataclass(frozen=True)
class CiResult:
    state: str
    summary: str
    url: str | None = None


def is_authorized(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID)


def require_authorized(update: Update) -> bool:
    if is_authorized(update):
        return True
    logger.warning("Ignoring unauthorized update from chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    return False


async def reply_chunks(update: Update, text: str) -> None:
    if not update.message:
        return

    text = redact_sensitive(text)
    if not text:
        await update.message.reply_text("(no output)")
        return

    for start in range(0, len(text), MAX_TELEGRAM_MESSAGE_LENGTH):
        await update.message.reply_text(text[start : start + MAX_TELEGRAM_MESSAGE_LENGTH])


def run(args: list[str], cwd: Path = REPO_PATH, timeout: int = COMMAND_TIMEOUT_SECONDS) -> CommandResult:
    logger.info("Running command: %s cwd=%s timeout=%s", args, cwd, timeout)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            cwd=cwd,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(args)}\n{output}") from exc

    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}\n{output}")
    return CommandResult(args=args, returncode=result.returncode, output=output)


def github_request(method: str, path: str, data: dict | None = None, query: dict | None = None) -> dict:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured")

    url = f"{GITHUB_API_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "channel-cast-ai-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=COMMAND_TIMEOUT_SECONDS) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API failed ({exc.code}) {method} {path}: {error_body}") from exc

    if not response_body:
        return {}
    return json.loads(response_body)


def anthropic_limit_headers() -> tuple[int, dict[str, str], str]:
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "Reply with OK."}],
    }
    headers = {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "x-api-key": ANTHROPIC_KEY,
    }
    request = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=COMMAND_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, response_headers, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, response_headers, body


def format_limit_row(headers: dict[str, str], key: str, label: str) -> str | None:
    prefix = f"anthropic-ratelimit-{key}"
    limit = headers.get(f"{prefix}-limit")
    remaining = headers.get(f"{prefix}-remaining")
    reset = headers.get(f"{prefix}-reset")
    if not any((limit, remaining, reset)):
        return None

    parts = [label]
    if remaining is not None and limit is not None:
        parts.append(f"{remaining}/{limit} remaining")
    elif remaining is not None:
        parts.append(f"{remaining} remaining")
    elif limit is not None:
        parts.append(f"limit {limit}")
    if reset is not None:
        parts.append(f"resets {reset}")
    return "- " + ", ".join(parts)


def format_anthropic_limits(status: int, headers: dict[str, str], body: str) -> str:
    rows = [
        format_limit_row(headers, "requests", "Requests"),
        format_limit_row(headers, "input-tokens", "Input tokens"),
        format_limit_row(headers, "output-tokens", "Output tokens"),
        format_limit_row(headers, "tokens", "Tokens"),
    ]
    rows = [row for row in rows if row]

    message = [
        f"Claude API limits for {ANTHROPIC_MODEL}:",
        *(rows or ["No Anthropic rate-limit headers were returned."]),
        "",
        "This check consumes one tiny Claude API request.",
    ]
    if status >= 400:
        message.extend(["", f"Anthropic returned HTTP {status}:", body[:1000]])
    return "\n".join(message)


def get_anthropic_limits() -> str:
    status, headers, body = anthropic_limit_headers()
    return format_anthropic_limits(status, headers, body)


def get_codex_status() -> str:
    version = run(["codex", "--version"], Path("/"), COMMAND_TIMEOUT_SECONDS).output.strip()
    login_status = run(["codex", "login", "status"], Path("/"), COMMAND_TIMEOUT_SECONDS).output.strip()

    return (
        "Codex status:\n"
        f"- CLI: {version or 'installed'}\n"
        f"- Login: {login_status or 'unknown'}\n"
        "- Plan limits remaining: not exposed by the Codex CLI/API\n\n"
        "Check remaining Codex plan usage in the Codex/OpenAI UI when a usage banner appears."
    )


def slugify_branch_name(feature_description: str) -> str:
    slug = feature_description.lower().strip()
    slug = re.sub(r"[^a-z0-9._/-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-./")
    if not slug:
        slug = "change"
    branch_name = f"feature/{slug[:80].strip('-./')}"
    validate_branch_name(branch_name)
    return branch_name


def validate_branch_name(branch_name: str) -> None:
    invalid = (
        branch_name.startswith("/")
        or branch_name.endswith("/")
        or branch_name.endswith(".")
        or ".." in branch_name
        or "@{" in branch_name
        or "\\" in branch_name
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch_name)
    )
    if invalid:
        raise ValueError(f"Invalid branch name: {branch_name}")


def ensure_github_configured() -> None:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured in the agent environment")
    if "/" not in GITHUB_REPOSITORY:
        raise RuntimeError("GITHUB_REPOSITORY must use owner/repo format")


def kotlin_file_sample() -> str:
    files = []
    for path in REPO_PATH.rglob("*.kt"):
        relative = path.relative_to(REPO_PATH)
        if "build" in relative.parts:
            continue
        files.append(str(relative))
        if len(files) >= 50:
            break
    return "\n".join(files)


def plan_feature(feature_description: str) -> str:
    context = kotlin_file_sample()

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": f"""
You are a senior Android architect working on a multi-module IPTV app called Channel Cast.

Project modules:
- app/ - Main app module
- data-* - Data layer (storage, network, repository, prefs)
- ui-* - UI layer (features, core, models)
- channel-health-monitor/
- proxy-health-monitor/

Project files sample:
{context}

Feature request: {feature_description}

Produce:
1. Branch name (feature/xxx)
2. Files to create/modify
3. Step by step implementation plan
4. Ready-to-use Codex prompt
                """,
            }
        ],
    )
    return response.content[0].text


def implement(plan: str, branch_name: str) -> None:
    validate_branch_name(branch_name)
    run(["git", "checkout", "main"])
    run(["git", "pull", "origin", "main"])
    run(["git", "checkout", "-b", branch_name])
    run(["codex", plan], timeout=CODEX_TIMEOUT_SECONDS)


def has_changes() -> bool:
    return bool(run(["git", "status", "--porcelain"]).output.strip())


def push(branch_name: str, feature_name: str) -> str:
    validate_branch_name(branch_name)
    if not has_changes():
        raise RuntimeError("Codex finished, but there are no repository changes to commit")
    run(["git", "add", "."])
    run(["git", "commit", "-m", f"feat: {feature_name}"])
    run(["git", "push", "origin", branch_name])
    return run(["git", "rev-parse", "HEAD"]).output.strip()


def create_pull_request(branch_name: str, feature_name: str, plan_text: str) -> PullRequest:
    ensure_github_configured()
    validate_branch_name(branch_name)
    payload = {
        "title": f"feat: {feature_name}",
        "head": branch_name,
        "base": GITHUB_BASE_BRANCH,
        "body": f"Generated by Channel Cast Agent.\n\nPlan:\n\n{plan_text}",
        "maintainer_can_modify": True,
    }
    response = github_request("POST", f"/repos/{GITHUB_REPOSITORY}/pulls", payload)
    return PullRequest(
        number=int(response["number"]),
        url=response["html_url"],
        head_sha=response["head"]["sha"],
    )


def list_workflow_runs(head_sha: str) -> list[dict]:
    response = github_request(
        "GET",
        f"/repos/{GITHUB_REPOSITORY}/actions/runs",
        query={"head_sha": head_sha, "per_page": "20"},
    )
    return response.get("workflow_runs", [])


def list_workflow_jobs(run_id: int) -> list[dict]:
    response = github_request("GET", f"/repos/{GITHUB_REPOSITORY}/actions/runs/{run_id}/jobs")
    return response.get("jobs", [])


def summarize_failed_jobs(runs: list[dict]) -> str:
    lines = []
    for run_data in runs:
        if run_data.get("conclusion") not in {"failure", "cancelled", "timed_out", "action_required"}:
            continue
        jobs = list_workflow_jobs(int(run_data["id"]))
        failed_jobs = [
            job
            for job in jobs
            if job.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"}
        ]
        if not failed_jobs:
            lines.append(f"- {run_data.get('name', 'workflow')} failed: {run_data.get('html_url')}")
            continue
        for job in failed_jobs:
            failed_steps = [
                step.get("name", "unknown step")
                for step in job.get("steps", [])
                if step.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"}
            ]
            step_text = f" ({', '.join(failed_steps[:3])})" if failed_steps else ""
            lines.append(f"- {job.get('name', 'job')}{step_text}: {job.get('html_url')}")
    return "\n".join(lines)


def evaluate_ci(head_sha: str) -> CiResult:
    runs = list_workflow_runs(head_sha)
    if not runs:
        return CiResult("waiting", "CI has not started yet")

    active = [run_data for run_data in runs if run_data.get("status") in {"queued", "requested", "waiting", "pending", "in_progress"}]
    if active:
        names = ", ".join(run_data.get("name", "workflow") for run_data in active[:5])
        return CiResult("running", f"CI running: {names}", active[0].get("html_url"))

    failed = [
        run_data
        for run_data in runs
        if run_data.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"}
    ]
    if failed:
        details = summarize_failed_jobs(failed)
        summary = "CI failed"
        if details:
            summary = f"{summary}:\n{details}"
        return CiResult("failed", summary, failed[0].get("html_url"))

    successful = [
        run_data
        for run_data in runs
        if run_data.get("conclusion") in {"success", "skipped", "neutral"}
    ]
    if successful:
        names = ", ".join(run_data.get("name", "workflow") for run_data in successful[:5])
        return CiResult("passed", f"CI passed: {names}", successful[0].get("html_url"))

    return CiResult("waiting", "CI status is not final yet")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    await reply_chunks(
        update,
        "Channel Cast Agent ready.\n\n"
        "Commands:\n"
        "/plan <feature> - plan only, no implementation\n"
        "/implement <feature> - plan and wait for /confirm before Codex, PR, and CI watch\n"
        "/confirm - run the pending implementation, open PR, and poll CI\n"
        "/cancel - discard the pending implementation\n"
        "/ci <pr-number> - show current GitHub Actions result for a PR\n"
        "/limits - show remaining Claude API rate limits\n"
        "/codex - show Codex CLI/login status\n"
        "/branches - list branches\n"
        "/status - git status\n"
        "/logs [lines] - recent service logs\n"
        "/help - show this help",
    )


async def branches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    result = await asyncio.to_thread(run, ["git", "branch", "-a"])
    await reply_chunks(update, f"Branches:\n{result.output}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    result = await asyncio.to_thread(run, ["git", "status"])
    await reply_chunks(update, f"Status:\n{result.output}")


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    requested_lines = int(context.args[0]) if context.args and context.args[0].isdigit() else 60
    lines = max(1, min(requested_lines, MAX_LOG_LINES))
    result = await asyncio.to_thread(
        run,
        ["journalctl", "-u", "ai-agent.service", "-n", str(lines), "--no-pager"],
        Path("/"),
        COMMAND_TIMEOUT_SECONDS,
    )
    await reply_chunks(update, f"Logs:\n{result.output}")


async def limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    result = await asyncio.to_thread(get_anthropic_limits)
    await reply_chunks(update, result)


async def codex_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    result = await asyncio.to_thread(get_codex_status)
    await reply_chunks(update, result)


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    feature = " ".join(context.args).strip()
    if not feature:
        await reply_chunks(update, "Usage: /plan <feature description>")
        return

    await reply_chunks(update, "Planning with Claude...")
    plan_text = await asyncio.to_thread(plan_feature, feature)
    await reply_chunks(update, f"Plan:\n{plan_text}")


async def implement_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    feature = " ".join(context.args).strip()
    if not feature:
        await reply_chunks(update, "Usage: /implement <feature description>")
        return

    await reply_chunks(update, "Planning with Claude...")
    plan_text = await asyncio.to_thread(plan_feature, feature)
    branch_name = slugify_branch_name(feature)

    context.user_data["pending_implementation"] = {
        "feature": feature,
        "plan": plan_text,
        "branch_name": branch_name,
    }

    await reply_chunks(
        update,
        f"Plan:\n{plan_text}\n\n"
        f"Pending branch: {branch_name}\n"
        "Send /confirm to run Codex and push, or /cancel to discard this request.",
    )


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    pending = context.user_data.get("pending_implementation")
    if not pending:
        await reply_chunks(update, "No pending implementation. Use /implement <feature> first.")
        return

    feature = pending["feature"]
    plan_text = pending["plan"]
    branch_name = pending["branch_name"]

    await asyncio.to_thread(ensure_github_configured)

    await reply_chunks(update, f"Running Codex on {branch_name}...")
    await asyncio.to_thread(implement, plan_text, branch_name)

    await reply_chunks(update, "Committing and pushing branch...")
    commit_sha = await asyncio.to_thread(push, branch_name, feature)

    await reply_chunks(update, "Opening GitHub PR...")
    pull_request = await asyncio.to_thread(create_pull_request, branch_name, feature, plan_text)
    await reply_chunks(update, f"PR opened: {pull_request.url}\nHead: {pull_request.head_sha or commit_sha}")

    await watch_ci(update, pull_request.head_sha or commit_sha)

    context.user_data.pop("pending_implementation", None)
    await reply_chunks(update, f"Done.\nBranch: {branch_name}\nPR: {pull_request.url}")


async def watch_ci(update: Update, head_sha: str) -> None:
    deadline = asyncio.get_running_loop().time() + CI_TIMEOUT_SECONDS
    last_summary = None

    while True:
        result = await asyncio.to_thread(evaluate_ci, head_sha)
        if result.summary != last_summary:
            message = result.summary
            if result.url:
                message = f"{message}\n{result.url}"
            await reply_chunks(update, message)
            last_summary = result.summary

        if result.state in {"passed", "failed"}:
            return

        if asyncio.get_running_loop().time() >= deadline:
            await reply_chunks(update, f"CI polling timed out after {CI_TIMEOUT_SECONDS}s for {head_sha}")
            return

        await asyncio.sleep(CI_POLL_INTERVAL_SECONDS)


async def ci(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    if not context.args or not context.args[0].isdigit():
        await reply_chunks(update, "Usage: /ci <pr-number>")
        return

    pr_number = int(context.args[0])
    ensure_github_configured()
    pull_data = await asyncio.to_thread(github_request, "GET", f"/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}")
    head_sha = pull_data["head"]["sha"]
    result = await asyncio.to_thread(evaluate_ci, head_sha)
    message = f"PR #{pr_number}: {result.summary}"
    if result.url:
        message = f"{message}\n{result.url}"
    await reply_chunks(update, message)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    context.user_data.pop("pending_implementation", None)
    await reply_chunks(update, "Pending implementation discarded.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc_info = None
    if context.error:
        exc_info = (type(context.error), context.error, context.error.__traceback__)
    logger.error("Unhandled Telegram handler error", exc_info=exc_info)
    if isinstance(update, Update) and is_authorized(update):
        await reply_chunks(update, f"Error:\n{context.error}")


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("implement", implement_cmd))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("ci", ci))
    app.add_handler(CommandHandler("limits", limits))
    app.add_handler(CommandHandler("codex", codex_status))
    app.add_handler(CommandHandler("branches", branches))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_error_handler(error_handler)
    logger.info("Agent running with repo_path=%s model=%s", REPO_PATH, ANTHROPIC_MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()
