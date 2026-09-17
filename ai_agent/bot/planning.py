"""Handle plan revisions and bugfix clarification."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.state import (
    current_planning_agent,
    forget_pending_implementation,
    set_pending_from_plan,
)
from ai_agent.bot.transport import (
    prompt_for_arguments,
    reply_chunks,
    require_authorized,
)
from ai_agent.plan_state import (
    new_plan_state,
    parse_plan_document,
    render_history,
    render_plan,
    revise_plan_state,
)
from ai_agent.planner import (
    assess_bugfix_report,
    bugfix_questions,
    build_bugfix_prompt,
    plan_feature,
    planning_agent_label,
    revise_feature_plan,
)
from ai_agent.workflow import slugify_branch_name


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a pending plan from the requested feature."""
    if not require_authorized(update):
        return
    feature = " ".join(context.args).strip()
    if not feature:
        await prompt_for_arguments(
            update, context, "plan", "Describe the feature to plan."
        )
        return

    provider = current_planning_agent(context)
    await reply_chunks(update, f"Planning with {planning_agent_label(provider)}...")
    plan_text = await asyncio.to_thread(plan_feature, feature, provider)
    plan_state = new_plan_state(feature, plan_text)
    context.user_data["pending_plan"] = plan_state
    forget_pending_implementation(context)
    await reply_chunks(update, render_plan(plan_state))


async def discuss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revise the pending plan using the supplied feedback."""
    if not require_authorized(update):
        return
    plan_state = context.user_data.get("pending_plan")
    if not plan_state:
        await reply_chunks(update, "No pending plan. Use /plan <feature> first.")
        return

    feedback = " ".join(context.args).strip()
    if not feedback:
        await prompt_for_arguments(
            update, context, "discuss", "Describe the changes to the current plan."
        )
        return

    provider = current_planning_agent(context)
    await reply_chunks(
        update, f"Revising plan with {planning_agent_label(provider)}..."
    )
    plan_text = await asyncio.to_thread(
        revise_feature_plan,
        plan_state.feature,
        plan_state.plan_text,
        feedback,
        provider,
    )
    revised = revise_plan_state(plan_state, plan_text)
    context.user_data["pending_plan"] = revised
    forget_pending_implementation(context)
    await reply_chunks(
        update, f"Plan updated (Revision {revised.revision})\n\n{render_plan(revised)}"
    )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve the current plan revision without starting implementation."""
    if not require_authorized(update):
        return
    plan_state = context.user_data.get("pending_plan")
    if not plan_state:
        await reply_chunks(update, "No pending plan. Use /plan <feature> first.")
        return

    plan_state.approved = True
    context.user_data["pending_plan"] = plan_state
    set_pending_from_plan(context, plan_state)
    document = parse_plan_document(plan_state.plan_text, plan_state.feature)
    await reply_chunks(
        update,
        f"Plan approved.\n\nBranch:\n{document.branch}\n\nCommands:\n- /confirm to enqueue\n- /cancel",
    )


async def showplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current pending plan."""
    if not require_authorized(update):
        return
    plan_state = context.user_data.get("pending_plan")
    if not plan_state:
        await reply_chunks(update, "No pending plan.")
        return
    await reply_chunks(update, render_plan(plan_state))


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the pending plan revision history."""
    if not require_authorized(update):
        return
    plan_state = context.user_data.get("pending_plan")
    if not plan_state:
        await reply_chunks(update, "No pending plan history.")
        return
    await reply_chunks(update, render_history(plan_state))


async def implement_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create an approved plan and wait for confirmation."""
    if not require_authorized(update):
        return
    feature = " ".join(context.args).strip()
    if not feature:
        await prompt_for_arguments(
            update, context, "implement", "Describe the feature to implement."
        )
        return

    provider = current_planning_agent(context)
    await reply_chunks(update, f"Planning with {planning_agent_label(provider)}...")
    plan_text = await asyncio.to_thread(plan_feature, feature, provider)
    plan_state = new_plan_state(feature, plan_text)
    plan_state.approved = True
    context.user_data["pending_plan"] = plan_state
    set_pending_from_plan(context, plan_state)
    document = parse_plan_document(plan_state.plan_text, plan_state.feature)

    await reply_chunks(
        update,
        f"{render_plan(plan_state)}\n\n"
        "Plan approved for the existing /implement flow.\n\n"
        f"Pending branch: {document.branch}\n"
        "Send /confirm to enqueue this task, or /cancel to discard this request.",
    )


async def bugfix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Assess a bug report and request clarification when needed."""
    if not require_authorized(update):
        return
    bug = " ".join(context.args).strip()
    if not bug:
        await prompt_for_arguments(
            update, context, "bugfix", "Describe the bug and the expected behavior."
        )
        return

    await reply_chunks(update, "Checking whether the bug report is actionable...")
    questions = await asyncio.to_thread(
        get_bugfix_questions, bug, current_planning_agent(context)
    )
    if questions:
        context.user_data["pending_bugfix_clarification"] = {
            "bug": bug,
            "branch_source": bug,
        }
        await reply_chunks(
            update,
            f"I need a bit more detail before running Codex:\n{questions}\n\n"
            "Reply with /answer <details>, or /cancel to discard this request.",
        )
        return

    await prepare_bugfix(update, context, bug, bug)


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply answers to pending bugfix clarification questions."""
    if not require_authorized(update):
        return
    pending = context.user_data.get("pending_bugfix_clarification")
    if not pending:
        await reply_chunks(
            update, "No pending bugfix questions. Use /bugfix <bug> first."
        )
        return

    details = " ".join(context.args).strip()
    if not details:
        await prompt_for_arguments(
            update, context, "answer", "Provide the answers to the bugfix questions."
        )
        return

    combined_bug = f"{pending['bug']}\n\nUser clarification:\n{details}"
    branch_source = pending.get("branch_source", pending["bug"])
    await reply_chunks(update, "Checking the updated bug report...")
    questions = await asyncio.to_thread(
        get_bugfix_questions, combined_bug, current_planning_agent(context)
    )
    if questions:
        context.user_data["pending_bugfix_clarification"] = {
            "bug": combined_bug,
            "branch_source": branch_source,
        }
        await reply_chunks(
            update,
            f"I still need more detail:\n{questions}\n\n"
            "Reply with /answer <details>, or /cancel to discard this request.",
        )
        return

    context.user_data.pop("pending_bugfix_clarification", None)
    await prepare_bugfix(update, context, combined_bug, branch_source)


def get_bugfix_questions(bug: str, provider: str | None = None) -> str | None:
    """Return clarification questions from the selected planner."""
    return bugfix_questions(assess_bugfix_report(bug, provider))


async def prepare_bugfix(
    update: Update, context: ContextTypes.DEFAULT_TYPE, bug: str, branch_source: str
) -> None:
    """Prepare pending implementation data for a bugfix branch."""
    bugfix_prompt = await asyncio.to_thread(build_bugfix_prompt, bug)
    branch_name = slugify_branch_name(branch_source, "bugfix")

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
        "Send /confirm to enqueue this task, or /cancel to discard this request.",
    )
