import asyncio
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ai_agent.anthropic_limits import get_anthropic_limits
from ai_agent.ci import evaluate_ci
from ai_agent.codex_status import get_codex_status
from ai_agent.config import (
    CHAT_ID,
    CI_POLL_INTERVAL_SECONDS,
    CI_TIMEOUT_SECONDS,
    COMMAND_TIMEOUT_SECONDS,
    GITHUB_REPOSITORY,
    MAX_LOG_LINES,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    TELEGRAM_TOKEN,
    redact_sensitive,
)
from ai_agent.github import ensure_github_configured, github_request
from ai_agent.planner import build_bugfix_prompt, plan_feature
from ai_agent.shell import run
from ai_agent.test_runner import run_unit_tests
from ai_agent.workflow import create_pull_request, implement, push, slugify_branch_name


logger = logging.getLogger(__name__)


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    await reply_chunks(
        update,
        "Channel Cast Agent ready.\n\n"
        "Commands:\n"
        "/plan <feature> - plan only, no implementation\n"
        "/implement <feature> - plan and wait for /confirm before Codex, PR, and CI watch\n"
        "/bugfix <bug> - wait for /confirm, then fix a bug on a bugfix branch\n"
        "/confirm - run the pending implementation, open PR, and poll CI\n"
        "/cancel - discard the pending implementation\n"
        "/ci <pr-number> - show current GitHub Actions result for a PR\n"
        "/limits - show remaining Claude API rate limits\n"
        "/codex - show Codex CLI/login status\n"
        "/test - run agent unit tests\n"
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


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    await reply_chunks(update, "Running agent unit tests...")
    result = await asyncio.to_thread(run_unit_tests)
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
        "change": feature,
        "codex_prompt": plan_text,
        "branch_name": branch_name,
        "commit_type": "feat",
        "pr_body_label": "Plan",
        "confirmation_label": "implementation",
    }

    await reply_chunks(
        update,
        f"Plan:\n{plan_text}\n\n"
        f"Pending branch: {branch_name}\n"
        "Send /confirm to run Codex and push, or /cancel to discard this request.",
    )


async def bugfix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    bug = " ".join(context.args).strip()
    if not bug:
        await reply_chunks(update, "Usage: /bugfix <bug description>")
        return

    await reply_chunks(update, "Preparing bug fix prompt...")
    bugfix_prompt = await asyncio.to_thread(build_bugfix_prompt, bug)
    branch_name = slugify_branch_name(bug, "bugfix")

    context.user_data["pending_implementation"] = {
        "change": bug,
        "codex_prompt": bugfix_prompt,
        "branch_name": branch_name,
        "commit_type": "fix",
        "pr_body_label": "Bug fix prompt",
        "confirmation_label": "bug fix",
    }

    await reply_chunks(
        update,
        f"Pending bug fix branch: {branch_name}\n"
        "Send /confirm to run Codex and push, or /cancel to discard this request.",
    )


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    pending = context.user_data.get("pending_implementation")
    if not pending:
        await reply_chunks(update, "No pending implementation. Use /implement <feature> or /bugfix <bug> first.")
        return

    change = pending["change"]
    codex_prompt = pending["codex_prompt"]
    branch_name = pending["branch_name"]
    commit_type = pending.get("commit_type", "feat")
    pr_body_label = pending.get("pr_body_label", "Plan")
    confirmation_label = pending.get("confirmation_label", "implementation")

    await asyncio.to_thread(ensure_github_configured)

    await reply_chunks(update, f"Running Codex on {branch_name}...")
    await asyncio.to_thread(implement, codex_prompt, branch_name)

    await reply_chunks(update, "Committing and pushing branch...")
    commit_sha = await asyncio.to_thread(push, branch_name, change, commit_type)

    await reply_chunks(update, "Opening GitHub PR...")
    pull_request = await asyncio.to_thread(
        create_pull_request,
        branch_name,
        change,
        codex_prompt,
        commit_type,
        pr_body_label,
    )
    await reply_chunks(update, f"PR opened: {pull_request.url}\nHead: {pull_request.head_sha or commit_sha}")

    await watch_ci(update, pull_request.head_sha or commit_sha)

    context.user_data.pop("pending_implementation", None)
    await reply_chunks(update, f"Done with {confirmation_label}.\nBranch: {branch_name}\nPR: {pull_request.url}")


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


def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("implement", implement_cmd))
    app.add_handler(CommandHandler("bugfix", bugfix_cmd))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("ci", ci))
    app.add_handler(CommandHandler("limits", limits))
    app.add_handler(CommandHandler("codex", codex_status))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CommandHandler("branches", branches))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_error_handler(error_handler)
    return app
