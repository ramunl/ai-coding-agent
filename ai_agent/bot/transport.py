"""Authorize updates and send Telegram replies."""

from __future__ import annotations

import logging

from telegram import ForceReply, Update
from telegram.ext import ContextTypes

from ai_agent.config import CHAT_ID, MAX_TELEGRAM_MESSAGE_LENGTH, redact_sensitive
from ai_agent_common import is_authorized as shared_is_authorized

logger = logging.getLogger(__name__)


def is_authorized(update: Update) -> bool:
    """Check whether the update belongs to the configured owner."""
    return shared_is_authorized(update, CHAT_ID)


def require_authorized(update: Update) -> bool:
    """Reject unauthorized chats and record the rejected attempt."""
    if is_authorized(update):
        return True
    logger.warning(
        "Ignoring unauthorized update from chat_id=%s",
        update.effective_chat.id if update.effective_chat else None,
    )
    return False


async def reply_chunks(update: Update, text: str) -> None:
    """Redact secrets and split replies to fit Telegram message limits."""
    if not update.message:
        return

    text = redact_sensitive(text)
    if not text:
        await update.message.reply_text("(no output)")
        return

    for start in range(0, len(text), MAX_TELEGRAM_MESSAGE_LENGTH):
        await update.message.reply_text(
            text[start : start + MAX_TELEGRAM_MESSAGE_LENGTH]
        )


async def send_rich_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, blocks: list[dict]
) -> None:
    """Send structured rich-text blocks without interpolating dynamic markup."""
    if not update.effective_chat:
        return
    await context.bot._post(
        "sendRichMessage",
        data={
            "chat_id": update.effective_chat.id,
            "rich_message": {"blocks": blocks, "skip_entity_detection": True},
        },
    )


async def prompt_for_arguments(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str, prompt: str
) -> None:
    """Record a reply prompt for a command that needs arguments."""
    message = await update.message.reply_text(
        prompt + "\nReply to this message, or use /cancel.",
        reply_markup=ForceReply(selective=True),
    )
    context.user_data["argument_prompt"] = (message.message_id, command)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log a handler failure and send a bounded error to the owner."""
    exc_info = None
    if context.error:
        exc_info = (type(context.error), context.error, context.error.__traceback__)
    logger.error("Unhandled Telegram handler error", exc_info=exc_info)
    if isinstance(update, Update) and is_authorized(update):
        error_text = str(context.error or "unknown error")
        lines = error_text.splitlines()
        if len(lines) > 20:
            error_text = "\n".join(lines[:20]) + (
                "\n... truncated. Use /verbosity debug and /logs when run "
                "logs are available."
            )
        await reply_chunks(update, f"Error:\n{error_text}")
