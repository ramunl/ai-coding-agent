"""Switching the worker on and off, and its budget: dashboard and /worker.

Kept apart from the loop so the chat command loads without the loop's
handlers (the loop needs the whole bot; this only needs the settings).
"""

from __future__ import annotations

import time
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.transport import reply_chunks, require_authorized
from ai_agent.worker import settings, view

WORKER_ACTIONS = (
    "worker_switch",
    "worker_settings",
    "worker_tools",
    "worker_scan",
    "worker_reviews",
    "worker_review",
)
USAGE = (
    "Usage: /worker [on|off|budget <runs per day> <reserve %>|tools auto|fixed"
    "|reviews <0-10>|review]"
)


def run_worker_action(data: Any, action: str, args: dict[str, str]) -> str:
    """Dashboard switches: on/off, and the budget settings."""
    worker = settings(data)
    if action == "worker_switch":
        worker["enabled"] = args["value"] == "on"
        if worker["enabled"]:
            worker["failures"] = 0
        data["worker_status"] = "starting" if worker["enabled"] else "off"
        return f"Worker {'on' if worker['enabled'] else 'off'}."
    if action == "worker_reviews":
        worker["review_proposals"] = int(args["value"])
        if worker["review_proposals"]:
            return (
                f"Daily code review: up to {worker['review_proposals']} proposals, "
                "one repository a day."
            )
        return "Daily code reviews are off."
    if action == "worker_tools":
        worker["auto_tool"] = args["value"] == "auto"
        if worker["auto_tool"]:
            return "Worker switches between Codex and Claude by their limits."
        return "Worker uses only the chosen planner and implementer."
    worker["max_runs"] = int(args["max_runs"])
    worker["reserve"] = int(args["reserve"])
    cap = worker["max_runs"] or "no cap"
    return f"Worker budget: {cap} runs a day, keeps {worker['reserve']}% of limits."


def status_text(data: Any) -> str:
    """The /worker reply."""
    shown = view(data, time.time())
    cap = shown["max_runs"] or "no cap"
    return (
        f"Worker: {'on' if shown['enabled'] else 'off'}\n"
        f"Now: {shown['status']}\n"
        f"Runs today: {shown['runs_today']} (cap {cap})\n"
        f"Reserve: {shown['reserve']}% of Codex limits\n"
        f"Tools: {'switch by limits' if shown['auto_tool'] else 'chosen ones only'}\n"
        f"Code review: "
        f"{shown['review_proposals'] or 'off'}"
        f"{' proposals a day' if shown['review_proposals'] else ''}\n"
        + "".join(
            f"{tool} out until {time.strftime('%H:%M', time.localtime(until))}\n"
            for tool, until in shown["out_until"].items()
        )
        + "\n/worker on · off · scan · review · reviews <0-10> · budget <runs> "
        "<reserve%> · tools auto|fixed"
    )


async def worker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/worker, /worker on|off, /worker budget <runs per day> <reserve %>."""
    if not require_authorized(update):
        return
    args = [arg.lower() for arg in context.args]
    if args == ["scan"]:
        from ai_agent.bot.worker_loop import scan_logs  # needs the whole bot

        proposed = await scan_logs(context.application, update.effective_chat.id)
        if not proposed:
            await reply_chunks(update, "No new recurring errors in the last 24 hours.")
        return
    if args == ["review"]:
        from ai_agent.bot.worker_loop import review_next  # needs the whole bot

        await reply_chunks(
            update, await review_next(context.application, update.effective_chat.id)
        )
        return
    if len(args) == 2 and args[0] == "reviews" and args[1].isdigit():
        if int(args[1]) > 10:
            await reply_chunks(update, "Reviews: 0–10 proposals a day (0 = off).")
            return
        run_worker_action(context.user_data, "worker_reviews", {"value": args[1]})
    elif len(args) == 2 and args[0] == "tools" and args[1] in ("auto", "fixed"):
        run_worker_action(context.user_data, "worker_tools", {"value": args[1]})
    elif args in (["on"], ["off"]):
        run_worker_action(context.user_data, "worker_switch", {"value": args[0]})
    elif len(args) == 3 and args[0] == "budget":
        runs, reserve = args[1], args[2].rstrip("%")
        if not (runs.isdigit() and reserve.isdigit()):
            await reply_chunks(
                update, "Usage: /worker budget <runs per day> <reserve %>"
            )
            return
        if int(runs) > 50 or int(reserve) > 90:
            await reply_chunks(
                update, "Runs per day: 0–50 (0 = no cap); reserve: 0–90%."
            )
            return
        run_worker_action(
            context.user_data, "worker_settings", {"max_runs": runs, "reserve": reserve}
        )
    elif args:
        await reply_chunks(update, USAGE)
        return
    await reply_chunks(update, status_text(context.user_data))
