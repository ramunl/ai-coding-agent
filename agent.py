import asyncio
import logging
import os

from telegram import Update
from telegram.ext import ApplicationHandlerStop, CommandHandler, ContextTypes

from ai_agent.config import ANTHROPIC_MODEL, CHAT_ID, REPO_PATH
from ai_agent.telegram_bot import build_application
from ai_agent.version import get_runtime_version


logger = logging.getLogger(__name__)


HELP_TEXT = """Channel Cast Agent ready.

Commands:
/plan <feature> - plan only, no implementation
/implement <feature> - plan and wait for /confirm before Codex, PR, and CI watch
/bugfix <bug> - clarify only when product behavior is missing, then wait for /confirm
/answer <details> - answer pending bugfix clarification questions
/confirm - run the pending implementation, open PR, and poll CI
/cancel - discard the pending implementation
/ci <pr-number> - show current GitHub Actions result for a PR
/limits - show remaining Claude API rate limits
/codex - show Codex CLI/login status
/version - show running bot version, branch, and commit
/test - run agent unit tests
/branches - list branches
/status - git status
/logs [lines] - recent service logs
/help - show this help"""


async def require_owner(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID and update.message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        raise ApplicationHandlerStop

    await update.message.reply_text(HELP_TEXT)
    raise ApplicationHandlerStop


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return

    result = await asyncio.to_thread(get_runtime_version)
    await update.message.reply_text(result)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build_application()
    app.add_handler(CommandHandler("help", help_command), group=-1)
    app.add_handler(CommandHandler("version", version))
    logger.info("Agent running with repo_path=%s model=%s", REPO_PATH, ANTHROPIC_MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()
