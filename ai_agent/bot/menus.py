"""Render help and command menus."""

from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.constants import BOT_COMMANDS
from ai_agent.bot.transport import require_authorized
from ai_agent.projects import active_project
from ai_agent_common import keyboard


def _cmd(text: str) -> dict:
    """Return a Telegram command entity, which clients render as a tappable link."""
    # Usage placeholders are display text, never arguments to execute.
    command = text.split()[0]
    if text.startswith(("/core update", "/core release")):
        command = " ".join(text.split()[:2])
    return {"type": "bot_command", "text": text, "bot_command": command}


def _menu_text(value: Any) -> str:
    """Render menu copy as ordinary text so slash commands remain detectable."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_menu_text(item) for item in value)
    return _menu_text(value.get("text", ""))


async def send_command_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, blocks: list[dict[str, Any]]
) -> None:
    """Send command reference text with a grid of executable buttons."""
    options = [
        (f"/{command.command}", f"command:{command.command}")
        for command in BOT_COMMANDS
    ]
    options.extend(
        [
            ("/core update", "command:core update"),
            ("/core release", "command:core release"),
        ]
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n\n".join(_menu_text(block) for block in blocks),
        reply_markup=keyboard(options, columns=3),
        parse_mode=None,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the concise help menu."""
    if not require_authorized(update):
        return
    project = active_project()

    blocks = [
        {
            "type": "paragraph",
            "text": [
                {"type": "bold", "text": "Coding AI Agent"},
                "\nProject: ",
                {"type": "bold", "text": project.name},
                "  ",
                {
                    "type": "code",
                    "text": f"{project.github_repository} [{project.base_branch}]",
                },
            ],
        },
        {"type": "heading", "size": 2, "text": "Build something"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/implement <feature>"),
                " - plan it, then ",
                _cmd("/confirm"),
                " to run\n",
                _cmd("/bugfix <bug>"),
                " - same flow, on a bugfix branch\n",
                _cmd("/plan <feature>"),
                " - plan only, no implementation\n",
                _cmd("/discuss <feedback>"),
                " - revise the plan  ",
                _cmd("/approve"),
                " - accept it\n",
                _cmd("/queue"),
                " - what is running and pending",
            ],
        },
        {"type": "heading", "size": 2, "text": "Projects"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/repo_list"),
                " - projects, active marked *\n",
                _cmd("/repo_use <name>"),
                " - switch active project\n",
                _cmd("/status"),
                "  ",
                _cmd("/branches"),
                "  ",
                _cmd("/pull"),
                " - act on the active project\n",
                _cmd("/pr"),
                "  ",
                _cmd("/ci <pr>"),
                "  ",
                _cmd("/fixpr <pr>"),
                " - pull requests and CI",
            ],
        },
        {"type": "heading", "size": 2, "text": "Agent itself"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/version"),
                "  ",
                _cmd("/core"),
                " - versions  ",
                _cmd("/deploy <branch>"),
                " - go live\n",
                _cmd("/model"),
                " - AI tools and models  ",
                _cmd("/limits"),
                " - quota",
            ],
        },
        {"type": "heading", "size": 2, "text": "More"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/more"),
                " - full command list, provider setup, and details",
            ],
        },
        {"type": "heading", "size": 2, "text": "All commands"},
        {
            "type": "paragraph",
            "text": [
                item
                for index, command in enumerate(BOT_COMMANDS)
                for item in (("   " if index else ""), _cmd(f"/{command.command}"))
                if item
            ],
        },
    ]
    await send_command_menu(update, context, blocks)


async def more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full reference: everything not in the short /help."""
    if not require_authorized(update):
        return
    blocks = [
        {
            "type": "paragraph",
            "text": [
                _cmd("/start"),
                "  ",
                _cmd("/help"),
                " - short help   ",
                _cmd("/more"),
                " - this reference\n",
                _cmd("/version"),
                " - running agent version",
            ],
        },
        {"type": "heading", "size": 2, "text": "Planning, step by step"},
        {
            "type": "paragraph",
            "text": [
                "1. ",
                _cmd("/plan <feature>"),
                "  2. ",
                _cmd("/discuss <feedback>"),
                "  3. ",
                _cmd("/approve"),
                "  4. ",
                _cmd("/confirm"),
                "\n",
                _cmd("/implement <feature>"),
                " is the shortcut for 1-3.\n",
                _cmd("/bugfix <bug>"),
                " - prepare a bugfix plan\n",
                _cmd("/showplan"),
                " - current plan   ",
                _cmd("/history"),
                " - revisions\n",
                _cmd("/answer <details>"),
                " - answer bugfix clarifications\n",
                _cmd("/cancel [task-id]"),
                " - discard pending or queued work",
            ],
        },
        {"type": "heading", "size": 2, "text": "Choosing the AI"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/planner [codex|claude]"),
                " - who writes plans and triages bugs\n",
                _cmd("/agent [codex|claude]"),
                " - who implements and repairs CI\n",
                "Either without an option shows the current choice.\n",
                _cmd("/model <tool> list"),
                " - options   ",
                _cmd("/model <tool> set <name>"),
                " - switch\n",
                _cmd("/codex"),
                " - Codex CLI/login status",
            ],
        },
        {"type": "heading", "size": 2, "text": "Projects"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/repo_add <owner/repo> [path]"),
                " - register and clone\n",
                _cmd("/repo_remove <name>"),
                " - unregister\n",
                _cmd("/branch [name]"),
                " - show or switch branch\n",
                _cmd("/ci <pr-number>"),
                " - GitHub Actions result for a PR\n",
                _cmd("/fixpr <pr-number>"),
                " - repair failed CI on an existing same-repository PR branch\n",
                "Git commands act on the ",
                {"type": "bold", "text": "active project"},
                " - never on this agent's own code.",
            ],
        },
        {"type": "heading", "size": 2, "text": "Core (shared code)"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/core"),
                " - this bot's core version\n",
                _cmd("/core update <bot>"),
                " - adopt the latest core (coding|pm|ops)\n",
                _cmd("/core release <version> <note>"),
                " - publish a new core release",
            ],
        },
        {"type": "heading", "size": 2, "text": "Output and diagnostics"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/verbosity concise|normal|debug"),
                " - output detail\n",
                _cmd("/diff"),
                " - changed files from the last run   ",
                _cmd("/show <file-number>"),
                " - one file's diff\n",
                _cmd("/logs [lines]"),
                " - last run logs, or service logs when idle\n",
                _cmd("/test"),
                " - run this agent's unit tests",
            ],
        },
        {"type": "heading", "size": 2, "text": "Everyday status"},
        {
            "type": "paragraph",
            "text": [
                _cmd("/queue"),
                "  ",
                _cmd("/status"),
                "  ",
                _cmd("/branches"),
                "  ",
                _cmd("/pull"),
                "\n",
                _cmd("/repo_list"),
                "  ",
                _cmd("/repo_use"),
                "  ",
                _cmd("/pr"),
                "  ",
                _cmd("/ci"),
                "  ",
                _cmd("/limits"),
                "\n",
                _cmd("/deploy"),
                "  ",
                _cmd("/logs"),
            ],
        },
    ]
    await send_command_menu(update, context, blocks)
