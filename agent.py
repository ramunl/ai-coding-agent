import asyncio
import logging
import os

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from ai_agent.config import (
    ANTHROPIC_MODEL,
    CHAT_ID,
    IMPLEMENTATION_AGENT,
    PLANNING_AGENT,
    REPO_PATH,
    validate_required_config,
)
from ai_agent.telegram_bot import build_application
from ai_agent.version import get_runtime_version


logger = logging.getLogger(__name__)


async def require_owner(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID and update.message)


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_owner(update):
        return

    result = await asyncio.to_thread(get_runtime_version)
    await update.message.reply_text(result)


def main() -> None:
    validate_required_config()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build_application()
    app.add_handler(CommandHandler("version", version))
    logger.info(
        "Agent running with repo_path=%s model=%s planning_agent=%s implementation_agent=%s",
        REPO_PATH,
        ANTHROPIC_MODEL,
        PLANNING_AGENT,
        IMPLEMENTATION_AGENT,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
