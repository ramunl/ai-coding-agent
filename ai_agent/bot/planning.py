"""Handle plan revisions and bugfix clarification."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.image_intake import report_missing
from ai_agent.bot.state import (
    current_planning_agent,
    forget_pending_implementation,
    planning_guard,
    set_pending_from_plan,
    think,
)
from ai_agent.bot.transport import (
    prompt_for_arguments,
    reply_chunks,
    require_authorized,
)
from ai_agent.image_delivery import invoke
from ai_agent.plan_state import (
    PlanState,
    capture_repository_identity,
    new_plan_state,
    parse_plan_document,
    render_history,
    render_plan,
    repository_mismatch,
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
from ai_agent.projects import active_project
from ai_agent.task_split import SplitError, split_pending
from ai_agent.tasks import find_task
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

    with planning_guard(context):
        identity = capture_repository_identity()
        provider = current_planning_agent(context)
        await reply_chunks(update, f"Planning with {planning_agent_label(provider)}...")
        task = find_task(context.user_data, context.user_data.get("planning_task", ""))
        images = (task or {}).get("images", [])
        await report_missing(update, images)
        plan_text = await think(
            context, "plan", feature, invoke, images, plan_feature, feature, provider
        )
        await report_missing(update, images)
        plan_state = new_plan_state(feature, plan_text, identity)
        plan_state.images = list(images)
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

    refusal = repository_mismatch(plan_state.repository_identity)
    if refusal:
        await reply_chunks(update, refusal)
        return

    feedback = " ".join(context.args).strip()
    if not feedback:
        await prompt_for_arguments(
            update, context, "discuss", "Describe the changes to the current plan."
        )
        return

    with planning_guard(context):
        provider = current_planning_agent(context)
        await reply_chunks(
            update, f"Revising plan with {planning_agent_label(provider)}..."
        )
        await report_missing(update, plan_state.images)
        plan_text = await think(
            context,
            "revise",
            plan_state.feature,
            invoke,
            plan_state.images,
            revise_feature_plan,
            plan_state.feature,
            plan_state.plan_text,
            feedback,
            provider,
        )
        await report_missing(update, plan_state.images)
        revised = revise_plan_state(plan_state, plan_text)
        context.user_data["pending_plan"] = revised
        forget_pending_implementation(context)
        await reply_chunks(
            update,
            f"Plan updated (Revision {revised.revision})\n\n{render_plan(revised)}",
        )


def _not_here(plan_state: PlanState) -> str | None:
    """Why this plan can't run in the active project, or None if it can."""
    document = parse_plan_document(plan_state.plan_text, plan_state.feature)
    names = [item["name"] for item in document.repositories]
    if not names:
        return None
    active = active_project().name
    if active in names:
        return None
    return (
        f"This plan does not change {active}, so it can't run here. Use /split "
        "to make one task per repository, or /discuss to revise it."
    )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve the current plan revision without starting implementation."""
    if not require_authorized(update):
        return
    plan_state = context.user_data.get("pending_plan")
    if not plan_state:
        await reply_chunks(update, "No pending plan. Use /plan <feature> first.")
        return

    refusal = repository_mismatch(plan_state.repository_identity) or _not_here(
        plan_state
    )
    if refusal:
        await reply_chunks(update, refusal)
        return

    plan_state.approved = True
    context.user_data["pending_plan"] = plan_state
    set_pending_from_plan(context, plan_state)
    document = parse_plan_document(plan_state.plan_text, plan_state.feature)
    await reply_chunks(
        update,
        (
            f"Plan approved.\n\nBranch:\n{document.branch}\n\nCommands:\n- /confirm "
            f"to enqueue\n- /cancel"
        ),
    )


async def split(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn the pending multi-repository plan into one task per repository."""
    if not require_authorized(update):
        return
    try:
        reply = split_pending(context.user_data)
    except SplitError as error:
        reply = str(error)
    await reply_chunks(update, reply)


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

    with planning_guard(context):
        identity = capture_repository_identity()
        provider = current_planning_agent(context)
        await reply_chunks(update, f"Planning with {planning_agent_label(provider)}...")
        plan_text = await think(
            context, "implement", feature, plan_feature, feature, provider
        )
        plan_state = new_plan_state(feature, plan_text, identity)
        refusal = repository_mismatch(plan_state.repository_identity) or _not_here(
            plan_state
        )
        if refusal:
            context.user_data["pending_plan"] = plan_state  # kept for /split
            await reply_chunks(update, f"{render_plan(plan_state)}\n\n{refusal}")
            return

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

    with planning_guard(context):
        identity = capture_repository_identity()
        images = list(getattr(update.message, "images", []) or [])
        # Own the uploads before yielding; failed assessments remain retryable.
        context.user_data["pending_bugfix_clarification"] = {
            "repository_identity": identity,
            "bug": bug,
            "branch_source": bug,
            "questions": "Assessment incomplete. Retry with /answer <details>.",
            "images": images,
        }
        await reply_chunks(update, "Checking whether the bug report is actionable...")
        await report_missing(update, images)
        questions = await think(
            context,
            "bugfix",
            bug,
            invoke,
            images,
            get_bugfix_questions,
            bug,
            current_planning_agent(context),
        )
        await report_missing(update, images)
        if questions:
            context.user_data["pending_bugfix_clarification"] = {
                "repository_identity": identity,
                "bug": bug,
                "branch_source": bug,
                "questions": questions,
                "images": images,
            }
            await reply_chunks(
                update,
                f"I need a bit more detail before running Codex:\n{questions}\n\n"
                "Reply with /answer <details>, or /cancel to discard this request.",
            )
            return

        await prepare_bugfix(update, context, bug, bug, identity, images)


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

    refusal = repository_mismatch(pending.get("repository_identity"))
    if refusal:
        await reply_chunks(update, refusal)
        return

    details = " ".join(context.args).strip()
    if not details:
        await prompt_for_arguments(
            update, context, "answer", "Provide the answers to the bugfix questions."
        )
        return

    with planning_guard(context):
        images = list(pending.get("images") or [])
        await report_missing(update, images)
        combined_bug = f"{pending['bug']}\n\nUser clarification:\n{details}"
        branch_source = pending.get("branch_source", pending["bug"])
        await reply_chunks(update, "Checking the updated bug report...")
        questions = await think(
            context,
            "answer",
            branch_source,
            invoke,
            images,
            get_bugfix_questions,
            combined_bug,
            current_planning_agent(context),
        )
        await report_missing(update, images)
        if questions:
            context.user_data["pending_bugfix_clarification"] = {
                "repository_identity": dict(pending["repository_identity"]),
                "bug": combined_bug,
                "branch_source": branch_source,
                "questions": questions,
                "images": images,
            }
            await reply_chunks(
                update,
                f"I still need more detail:\n{questions}\n\n"
                "Reply with /answer <details>, or /cancel to discard this request.",
            )
            return

        await prepare_bugfix(
            update,
            context,
            combined_bug,
            branch_source,
            pending["repository_identity"],
            images,
        )


def get_bugfix_questions(bug: str, provider: str | None = None) -> str | None:
    """Return clarification questions from the selected planner."""
    return bugfix_questions(assess_bugfix_report(bug, provider))


async def prepare_bugfix(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    bug: str,
    branch_source: str,
    identity: dict[str, str] | None = None,
    images: list[dict] | None = None,
) -> None:
    """Prepare pending implementation data for a bugfix branch."""
    with planning_guard(context):
        identity = (
            dict(identity) if identity is not None else capture_repository_identity()
        )
        bugfix_prompt = await asyncio.to_thread(build_bugfix_prompt, bug)
        branch_name = slugify_branch_name(branch_source, "bugfix")

        context.user_data["pending_implementation"] = {
            "repository_identity": identity,
            "change": bug,
            "images": list(images or []),
            "codex_prompt": bugfix_prompt,
            "branch_name": branch_name,
            "commit_type": "fix",
            "pr_body_label": "Bug fix prompt",
            "confirmation_label": "bug fix",
        }

        context.user_data.pop("pending_bugfix_clarification", None)
        await reply_chunks(
            update,
            f"Pending bug fix branch: {branch_name}\n"
            "Send /confirm to enqueue this task, or /cancel to discard this request.",
        )
