"""Select AI providers and configure models."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.ai_tools import all_info, get_tool, known_tools
from ai_agent.anthropic_limits import get_anthropic_limits
from ai_agent.bot.state import current_implementation_agent, current_planning_agent
from ai_agent.bot.transport import reply_chunks, require_authorized
from ai_agent.codex_status import get_codex_status
from ai_agent.config import ANTHROPIC_KEY
from ai_agent.planner import normalize_planning_agent, planning_agent_label
from ai_agent.self_update import schedule_restart
from ai_agent.workflow import implementation_agent_label, normalize_implementation_agent
from ai_agent_common import choice_keyboard


async def limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show provider readiness or available rate-limit information."""
    if not require_authorized(update):
        return

    requested = context.args[0].strip().lower() if context.args else "all"
    if requested == "planner":
        requested = current_planning_agent(context)
    elif requested == "agent":
        requested = current_implementation_agent(context)

    if requested not in {"all", "codex", "claude"}:
        await reply_chunks(update, "Usage: /limits [all|codex|claude|planner|agent]")
        return

    async def claude_limits() -> str:
        if not ANTHROPIC_KEY:
            return "Claude API limits:\n- Unavailable: ANTHROPIC_API_KEY is not configured."
        return await asyncio.to_thread(get_anthropic_limits)

    if requested == "codex":
        result = await asyncio.to_thread(get_codex_status)
    elif requested == "claude":
        result = await claude_limits()
    else:
        codex_result, claude_result = await asyncio.gather(
            asyncio.to_thread(get_codex_status),
            claude_limits(),
        )
        result = f"{codex_result}\n\n{claude_result}"
    await reply_chunks(update, result)


async def codex_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report local Codex CLI readiness."""
    if not require_authorized(update):
        return

    result = await asyncio.to_thread(get_codex_status)
    await reply_chunks(update, result)


async def planner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or change the provider used for planning."""
    if not require_authorized(update):
        return

    if not context.args:
        selected = current_planning_agent(context)
        await update.message.reply_text(
            f"Planning agent: {planning_agent_label(selected)}\n\nTap to choose:",
            reply_markup=choice_keyboard(
                "planner", ["codex", "claude"], active=selected
            ),
        )
        return

    try:
        selected = normalize_planning_agent(context.args[0])
    except ValueError as exc:
        await reply_chunks(update, str(exc))
        return

    context.user_data["planning_agent"] = selected
    await reply_chunks(
        update, f"Planning agent set to {planning_agent_label(selected)}."
    )


async def agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or change the provider used for implementation."""
    if not require_authorized(update):
        return

    if not context.args:
        selected_agent = current_implementation_agent(context)
        await update.message.reply_text(
            f"Implementation agent: {implementation_agent_label(selected_agent)}\n\nTap to choose:",
            reply_markup=choice_keyboard(
                "agent", ["codex", "claude"], active=selected_agent
            ),
        )
        return

    try:
        selected_agent = normalize_implementation_agent(context.args[0])
    except ValueError as exc:
        await reply_chunks(update, str(exc))
        return

    context.user_data["implementation_agent"] = selected_agent
    await reply_chunks(
        update,
        f"Implementation agent set to {implementation_agent_label(selected_agent)}.",
    )


async def model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspect or verify and persist a provider model selection."""
    if not require_authorized(update):
        return

    # /model  -> list every tool and its model
    no_args = not context.args
    if no_args:
        lines = ["AI tools and models:"]
        for entry in all_info():
            suffix = "" if entry.manageable else f"  ({entry.note})"
            lines.append(f"- {entry.tool}: {entry.model}{suffix}")
        lines.extend(
            ["", "Verify/switch a manageable tool: /model <tool> [list | set <name>]"]
        )
        await reply_chunks(update, "\n".join(lines))
        return

    tool_name = context.args[0]
    tool = get_tool(tool_name)
    is_known_tool = tool is not None
    if not is_known_tool:
        await reply_chunks(
            update,
            f"Unknown tool '{tool_name}'. Known: {', '.join(known_tools())}",
        )
        return

    # /model <tool>  -> show that tool; verify if manageable
    wants_show = len(context.args) == 1
    if wants_show:
        if tool.manageable:
            current = tool.current_model()
            await reply_chunks(update, f"{tool.name}: {current}\nVerifying...")
            reachable, detail = await asyncio.to_thread(tool.verify, current)
            status = "reachable" if reachable else f"UNREACHABLE - {detail}"
            await reply_chunks(update, f"{tool.name} {current}: {status}")
        else:
            await reply_chunks(
                update,
                f"{tool.name}: {tool.current_model()}\n{tool.info().note}",
            )
        return

    # /model <tool> list  -> show models available to switch to
    wants_list = context.args[1] == "list"
    if wants_list:
        if not tool.manageable:
            await reply_chunks(
                update, f"{tool.name} is read-only here. {tool.info().note}"
            )
            return
        await reply_chunks(update, f"Fetching available models for {tool.name}...")
        ok, result = await asyncio.to_thread(tool.list_models)
        if not ok:
            await reply_chunks(update, f"Could not list models: {result}")
            return
        current = tool.current_model()
        lines = [f"Models available for {tool.name} (current: {current}):"]
        for entry in result:
            marker = " (current)" if entry["id"] == current else ""
            lines.append(f"- {entry['id']} — {entry['display_name']}{marker}")
        lines.extend(["", f"Switch: /model {tool.name} set <id>"])
        await reply_chunks(update, "\n".join(lines))
        return

    # /model <tool> set <name>
    is_set = context.args[1] == "set" and len(context.args) >= 3
    if not is_set:
        await reply_chunks(update, f"Usage: /model {tool.name} list | set <name>")
        return

    if not tool.manageable:
        await reply_chunks(
            update,
            f"{tool.name} is read-only here. {tool.info().note}",
        )
        return

    candidate = context.args[2]
    await reply_chunks(update, f"Verifying {candidate} before switching {tool.name}...")
    reachable, detail = await asyncio.to_thread(tool.verify, candidate)
    if not reachable:
        await reply_chunks(
            update,
            f"Refusing to switch: {candidate} is {detail}.\nThe current model is unchanged.",
        )
        return

    await asyncio.to_thread(tool.set_model, candidate)
    restart_note = await asyncio.to_thread(schedule_restart)
    await reply_chunks(
        update,
        f"Verified and saved {tool.name} model = {candidate}.\n\n"
        f"{restart_note}\nConfirm with /model {tool.name} after a few seconds.",
    )


async def _on_planner_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, selected: str
) -> None:
    if not require_authorized(update):
        return
    try:
        selected = normalize_planning_agent(selected)
    except ValueError as error:
        await update.callback_query.edit_message_text(str(error))
        return
    context.user_data["planning_agent"] = selected
    await update.callback_query.edit_message_text(
        f"Planning agent set to {planning_agent_label(selected)}."
    )


async def _on_agent_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, selected: str
) -> None:
    if not require_authorized(update):
        return
    try:
        selected = normalize_implementation_agent(selected)
    except ValueError as error:
        await update.callback_query.edit_message_text(str(error))
        return
    context.user_data["implementation_agent"] = selected
    await update.callback_query.edit_message_text(
        f"Implementation agent set to {implementation_agent_label(selected)}."
    )
