"""Wire Telegram commands, callbacks, and startup hooks."""

from __future__ import annotations

import logging
from copy import copy

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_agent.bot.ci_monitor import ci
from ai_agent.bot.constants import BOT_COMMANDS
from ai_agent.bot.execution import confirm, fixpr
from ai_agent.bot.inspection import (
    _on_show_tap,
    _on_verbosity_tap,
    cancel,
    diff,
    logs,
    pr,
    queue_cmd,
    show,
    status,
    verbosity,
)
from ai_agent.bot.maintenance import (
    _notify_core_drift_on_startup,
    _on_core_update_tap,
    core,
    deploy,
    test,
    version,
)
from ai_agent.bot.maintenance import core_version_line as core_version_line
from ai_agent.bot.menus import more, start
from ai_agent.bot.planning import (
    answer,
    approve,
    bugfix_cmd,
    discuss,
    history,
    implement_cmd,
    plan,
    showplan,
)
from ai_agent.bot.providers import (
    _on_agent_tap,
    _on_planner_tap,
    agent_cmd,
    codex_status,
    limits,
    model,
    planner_cmd,
)
from ai_agent.bot.repositories import (
    _on_repo_remove_tap,
    _on_repo_use_tap,
    branch,
    branches,
    pull,
    repo_add,
    repo_list,
    repo_remove,
    repo_use,
)
from ai_agent.bot.state import task_queue
from ai_agent.bot.transport import error_handler, require_authorized
from ai_agent.config import (
    CHAT_ID,
    STATE_FILE,
    TELEGRAM_TOKEN,
    WEBAPP_HOST,
    WEBAPP_PORT,
    WEBAPP_URL,
)
from ai_agent_common import CallbackRouter

logger = logging.getLogger(__name__)


async def configure_bot_commands(app: Application) -> None:
    """Register autocomplete commands and check for shared-core drift."""
    await app.bot.set_my_commands(BOT_COMMANDS)
    await _notify_core_drift_on_startup(app)
    await _notify_restored_queue(app)
    await _start_dashboard(app)


async def _start_dashboard(app: Application) -> None:
    """Serve the Mini App and point this chat's menu button at it.

    Runs only when WEBAPP_URL is configured. Every failure is logged, never
    raised: the dashboard is optional, the bot is not.
    """
    if not WEBAPP_URL:
        return
    if not WEBAPP_URL.startswith("https://"):
        logger.error(
            "Dashboard disabled: WEBAPP_URL must be https:// (got %s)", WEBAPP_URL
        )
        return
    if not 0 < WEBAPP_PORT < 65536:
        logger.error("Dashboard disabled: WEBAPP_PORT must be 1-65535")
        return
    # initData identifies a user, so the owner check compares against CHAT_ID;
    # that only holds for a private chat, where chat id == user id.
    if CHAT_ID <= 0:
        logger.error(
            "Dashboard disabled: YOUR_CHAT_ID must be your private chat (user) id"
        )
        return
    try:
        from ai_agent.bot.webapp import start_dashboard
    except ImportError as error:
        logger.error(
            "Dashboard disabled, dependency missing (pip install -r requirements.txt): %s",
            error,
        )
        return
    if not await start_dashboard(
        app, TELEGRAM_TOKEN, CHAT_ID, WEBAPP_HOST, WEBAPP_PORT
    ):
        return
    try:
        from telegram import MenuButtonWebApp, WebAppInfo

        await app.bot.set_chat_menu_button(
            chat_id=CHAT_ID,
            menu_button=MenuButtonWebApp(
                text="Dashboard", web_app=WebAppInfo(url=WEBAPP_URL)
            ),
        )
    except Exception as error:
        logger.warning("Could not set the dashboard menu button (ignored): %s", error)


async def shutdown_hooks(app: Application) -> None:
    """Release the dashboard port on a clean stop."""
    if not WEBAPP_URL:
        return
    try:
        from ai_agent.bot.webapp import stop_dashboard
    except ImportError:
        return
    await stop_dashboard()


async def _notify_restored_queue(app: Application) -> None:
    """Tell the owner when queued work survived a restart.

    The queue is not resumed automatically: there is no update to reply to at
    boot, and auto-running work on startup could turn a crash into a restart
    loop. /confirm resumes it.
    """
    try:
        restored = task_queue(app.user_data.get(CHAT_ID, {}))
        if restored:
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"Restored {len(restored)} queued task(s) after restart.\n"
                    "Send /confirm to resume, /queue to review, or /cancel <id> to drop one."
                ),
            )
    except Exception as error:  # a notice must never block startup
        logger.warning("Could not report restored queue (ignored): %s", error)


async def _on_command_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, selection: str
) -> None:
    if not require_authorized(update):
        return
    command, *args = selection.split()
    handler = command_handlers().get(command)
    if handler is None or (args and selection not in ("core update", "core release")):
        return
    invocation = copy(context)
    invocation.args = args
    message_update = Update(update.update_id, message=update.callback_query.message)
    await handler(message_update, invocation)


async def argument_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch a matching reply to the command that requested arguments."""
    if not require_authorized(update):
        return
    pending = context.user_data.get("argument_prompt")
    reply = update.message.reply_to_message
    if not pending or not reply or reply.message_id != pending[0]:
        return
    text = update.message.text.strip()
    if not text:
        return
    context.user_data.pop("argument_prompt", None)
    command = pending[1]
    handlers = {
        "plan": plan,
        "implement": implement_cmd,
        "bugfix": bugfix_cmd,
        "discuss": discuss,
        "answer": answer,
        "repo_add": repo_add,
        "core release": core,
    }
    handler = handlers.get(command)
    if handler:
        invocation = copy(context)
        invocation.args = (
            ["release"] if command == "core release" else []
        ) + text.split()
        await handler(update, invocation)


async def _on_pull_request_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, selection: str
) -> None:
    if not require_authorized(update):
        return
    command = update.callback_query.data.split(":", 1)[0]
    if command not in ("ci", "fixpr") or not selection.isdigit():
        return
    # Reuse the command handlers, including their execution guards.
    invocation = copy(context)
    invocation.args = [selection]
    message_update = Update(update.update_id, message=update.callback_query.message)
    await {"ci": ci, "fixpr": fixpr}[command](message_update, invocation)


_callback_router = CallbackRouter()


_callback_router.register("command", _on_command_tap)


_callback_router.register("ci", _on_pull_request_tap)


_callback_router.register("fixpr", _on_pull_request_tap)


_callback_router.register("repo_use", _on_repo_use_tap)


_callback_router.register("repo_remove", _on_repo_remove_tap)


_callback_router.register("planner", _on_planner_tap)


_callback_router.register("agent", _on_agent_tap)


_callback_router.register("verbosity", _on_verbosity_tap)


_callback_router.register("show", _on_show_tap)


_callback_router.register("core_update", _on_core_update_tap)


def command_handlers() -> dict:
    """Map each typed command to its handler."""
    return {
        "start": start,
        "help": start,
        "more": more,
        "version": version,
        "plan": plan,
        "discuss": discuss,
        "approve": approve,
        "showplan": showplan,
        "history": history,
        "verbosity": verbosity,
        "implement": implement_cmd,
        "bugfix": bugfix_cmd,
        "answer": answer,
        "confirm": confirm,
        "queue": queue_cmd,
        "planner": planner_cmd,
        "agent": agent_cmd,
        "cancel": cancel,
        "ci": ci,
        "fixpr": fixpr,
        "diff": diff,
        "show": show,
        "pr": pr,
        "limits": limits,
        "model": model,
        "core": core,
        "codex": codex_status,
        "test": test,
        "pull": pull,
        "repo_list": repo_list,
        "repo_add": repo_add,
        "repo_use": repo_use,
        "repo_remove": repo_remove,
        "branches": branches,
        "branch": branch,
        "deploy": deploy,
        "status": status,
        "logs": logs,
    }


def build_application() -> Application:
    """Build the application with commands, callback routing, and error handling."""
    builder = Application.builder().token(TELEGRAM_TOKEN)
    if hasattr(builder, "concurrent_updates"):
        builder = builder.concurrent_updates(True)
    if hasattr(builder, "post_init"):
        builder = builder.post_init(configure_bot_commands)
    if hasattr(builder, "post_shutdown"):
        builder = builder.post_shutdown(shutdown_hooks)
    if hasattr(builder, "persistence"):
        # Imported here: only a real Application needs the PTB persistence base.
        from ai_agent.bot.persistence import JsonStatePersistence

        builder = builder.persistence(JsonStatePersistence(STATE_FILE))
    app = builder.build()
    for name, handler in command_handlers().items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(_callback_router.dispatch))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, argument_reply))
    app.add_error_handler(error_handler)
    return app
