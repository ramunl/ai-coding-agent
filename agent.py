import asyncio
import logging
import os
import re
import subprocess
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
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("COMMAND_TIMEOUT_SECONDS", "120"))
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1800"))
MAX_TELEGRAM_MESSAGE_LENGTH = 3900
MAX_LOG_LINES = 120

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    output: str


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


def push(branch_name: str, feature_name: str) -> None:
    validate_branch_name(branch_name)
    run(["git", "add", "."])
    run(["git", "commit", "-m", f"feat: {feature_name}"])
    run(["git", "push", "origin", branch_name])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    await reply_chunks(
        update,
        "Channel Cast Agent ready.\n\n"
        "Commands:\n"
        "/plan <feature> - plan only, no implementation\n"
        "/implement <feature> - plan and wait for /confirm before Codex and push\n"
        "/confirm - run the pending implementation\n"
        "/cancel - discard the pending implementation\n"
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

    await reply_chunks(update, f"Running Codex on {branch_name}...")
    await asyncio.to_thread(implement, plan_text, branch_name)

    await reply_chunks(update, "Pushing to GitHub...")
    await asyncio.to_thread(push, branch_name, feature)

    context.user_data.pop("pending_implementation", None)
    await reply_chunks(update, f"Done.\nBranch: {branch_name}\nPull it locally to test.")


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
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("implement", implement_cmd))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("branches", branches))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_error_handler(error_handler)
    logger.info("Agent running with repo_path=%s model=%s", REPO_PATH, ANTHROPIC_MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()
