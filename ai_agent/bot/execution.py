"""Run queued implementations and repair pull requests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.ci_monitor import (
    build_ci_repair_prompt,
    build_fix_pr_repair_prompt,
    choose_pull_request,
    failed_ci_exhausted_message,
    final_ci_status_message,
    watch_ci,
)
from ai_agent.bot.state import (
    active_execution_text,
    current_implementation_agent,
    enqueue_pending_implementation,
    get_verbosity,
    last_execution,
    set_active_execution,
    set_pending_from_plan,
    task_queue,
    update_last_execution,
)
from ai_agent.bot.transport import reply_chunks, require_authorized
from ai_agent.ci import CiResult, build_failure_context
from ai_agent.config import CI_FIX_ATTEMPTS
from ai_agent.github import PullRequest, ensure_github_configured, github_request
from ai_agent.plan_state import Verbosity, render_completion
from ai_agent.projects import active_project
from ai_agent.workflow import (
    ImplementationResult,
    create_pull_request,
    implement,
    implementation_agent_label,
    normalize_implementation_agent,
    push,
    repair_implementation,
    repair_pull_request_branch,
    return_to_base_branch,
    validate_branch_name,
)

logger = logging.getLogger(__name__)


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enqueue approved work and start the queue when idle."""
    if not require_authorized(update):
        return

    pending = context.user_data.get("pending_implementation")
    plan_state = context.user_data.get("pending_plan")
    if not pending and plan_state and plan_state.approved:
        set_pending_from_plan(context, plan_state)
        pending = context.user_data.get("pending_implementation")
    if not pending and plan_state and not plan_state.approved:
        await reply_chunks(
            update, "Plan is not approved yet. Use /approve before /confirm."
        )
        return
    if not pending:
        restored = task_queue(context)
        if restored and not context.user_data.get("queue_runner_active"):
            await reply_chunks(update, f"Resuming {len(restored)} queued task(s).")
            await run_task_queue(update, context)
            return
        await reply_chunks(
            update,
            "No pending implementation. Use /implement <feature> or /bugfix <bug> first.",
        )
        return

    task = enqueue_pending_implementation(context, pending)
    context.user_data.pop("pending_implementation", None)
    if plan_state:
        context.user_data.pop("pending_plan", None)

    position = len(task_queue(context))
    await reply_chunks(
        update,
        f"Queued task #{task['id']} at position {position}.\n\nBranch:\n{task['branch_name']}",
    )

    if context.user_data.get("queue_runner_active"):
        return

    await run_task_queue(update, context)


async def run_task_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drain queued tasks in FIFO order and release the runner flag."""
    context.user_data["queue_runner_active"] = True
    try:
        while task_queue(context):
            task = task_queue(context).pop(0)
            await run_queued_implementation(update, context, task)
    finally:
        context.user_data.pop("queue_runner_active", None)


async def run_queued_implementation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, task: dict
) -> None:
    """Implement one queued task and restore the base branch afterward."""
    change = task["change"]
    codex_prompt = task["codex_prompt"]
    branch_name = task["branch_name"]
    commit_type = task.get("commit_type", "feat")
    pr_body_label = task.get("pr_body_label", "Plan")
    confirmation_label = task.get("confirmation_label", "implementation")
    implementation_agent = normalize_implementation_agent(
        task.get("implementation_agent")
    )
    implementation_agent_name = implementation_agent_label(implementation_agent)

    try:
        set_active_execution(context, branch_name, "Checking GitHub configuration")
        await asyncio.to_thread(ensure_github_configured)

        set_active_execution(
            context, branch_name, f"Running {implementation_agent_name}"
        )
        await reply_chunks(
            update,
            f"Task #{task['id']} started.\n\nBranch:\n{branch_name}\n\nStatus:\nRUNNING",
        )
        implementation_result = await asyncio.to_thread(
            implement, codex_prompt, branch_name, implementation_agent
        )

        set_active_execution(context, branch_name, "Committing and pushing branch")
        await reply_chunks(update, "Committing and pushing branch...")
        commit_sha = await asyncio.to_thread(push, branch_name, change, commit_type)

        set_active_execution(context, branch_name, "Opening GitHub PR")
        await reply_chunks(update, "Opening GitHub PR...")
        pull_request = await asyncio.to_thread(
            create_pull_request,
            branch_name,
            change,
            codex_prompt,
            commit_type,
            pr_body_label,
        )
        update_last_execution(
            context,
            branch_name,
            implementation_result,
            pull_request.url,
        )

        if get_verbosity(context) != Verbosity.CONCISE:
            await reply_chunks(
                update,
                f"PR opened: {pull_request.url}\nHead: {pull_request.head_sha or commit_sha}",
            )

        set_active_execution(context, branch_name, "Polling CI")
        ci_result = await watch_ci(update, commit_sha)

        request = _RepairRequest(
            branch_name=branch_name,
            pull_request=pull_request,
            implementation_agent=implementation_agent,
            prompt=partial(build_ci_repair_prompt, codex_prompt),
            run=repair_implementation,
            commit_message=f"{change} CI repair",
        )
        ci_result, repair_attempt = await _repair_failed_ci(
            update, context, request, ci_result
        )
        if await _report_execution(update, context, ci_result, repair_attempt):
            return
        else:
            await reply_chunks(
                update,
                f"Done with {confirmation_label}.\nBranch: {branch_name}\nPR: {pull_request.url}",
            )
    finally:
        context.user_data.pop("active_execution", None)
        await reset_to_base_branch()


async def reset_to_base_branch() -> None:
    """Restore the project base branch and log cleanup failures."""
    try:
        await asyncio.to_thread(return_to_base_branch)
    except Exception:
        logger.exception(
            "Failed to check the repo back out to its base branch after task completion"
        )


async def fixpr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repair failing CI for an open pull request in the active repository."""
    if not require_authorized(update):
        return

    active_text = active_execution_text(context)
    if active_text:
        await reply_chunks(
            update, f"An implementation is already running.\n\n{active_text}"
        )
        return

    if not context.args:
        await choose_pull_request(update, context, "fixpr")
        return
    if not context.args[0].isdigit():
        await reply_chunks(update, "Usage: /fixpr <pr-number>")
        return

    pr_number = int(context.args[0])
    branch_name = ""
    pr_url = ""
    try:
        set_active_execution(
            context, f"PR #{pr_number}", "Checking GitHub configuration"
        )
        await asyncio.to_thread(ensure_github_configured)

        repository = active_project().github_repository
        pull_data = await asyncio.to_thread(
            github_request, "GET", f"/repos/{repository}/pulls/{pr_number}"
        )
        if pull_data.get("state") != "open":
            await reply_chunks(update, f"PR #{pr_number} is not open.")
            return

        head = pull_data.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        branch_name = str(head.get("ref") or "")
        validate_branch_name(branch_name)
        if head_repo != repository:
            await reply_chunks(
                update,
                f"PR #{pr_number} is from {head_repo or 'an unknown repository'}.\n"
                f"/fixpr can only push to branches in {repository}.",
            )
            return

        pr_url = str(pull_data.get("html_url") or "")
        head_sha = str(head.get("sha") or "")
        title = str(pull_data.get("title") or f"PR #{pr_number}")
        body = str(pull_data.get("body") or "")
        implementation_agent = current_implementation_agent(context)

        set_active_execution(context, branch_name, "Polling PR CI")
        await reply_chunks(
            update, f"Checking CI for PR #{pr_number}.\n\nBranch:\n{branch_name}"
        )
        ci_result = await watch_ci(update, head_sha)

        request = _RepairRequest(
            branch_name=branch_name,
            pull_request=PullRequest(pr_number, pr_url, head_sha),
            implementation_agent=implementation_agent,
            prompt=partial(build_fix_pr_repair_prompt, pr_number, title, body),
            run=repair_pull_request_branch,
            commit_message=f"PR #{pr_number} CI repair",
            is_existing_pr=True,
        )
        ci_result, repair_attempt = await _repair_failed_ci(
            update, context, request, ci_result
        )
        if repair_attempt and await _report_execution(
            update, context, ci_result, repair_attempt
        ):
            return

        if ci_result.state == "passed":
            await reply_chunks(
                update, f"PR #{pr_number} CI is already passing.\n{pr_url}"
            )
        else:
            await reply_chunks(
                update,
                f"PR #{pr_number} CI did not fail within the polling window.\n{pr_url}",
            )
    finally:
        context.user_data.pop("active_execution", None)
        await reset_to_base_branch()


@dataclass(frozen=True)
class _RepairRequest:
    """Hold branch and provider details for one CI repair sequence."""

    branch_name: str
    pull_request: PullRequest
    implementation_agent: str
    prompt: Callable[[str], str]
    run: Callable[[str, str, str], ImplementationResult]
    commit_message: str
    is_existing_pr: bool = False


async def _repair_failed_ci(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request: _RepairRequest,
    ci_result: CiResult,
) -> tuple[CiResult, int]:
    """Repair failed builds up to the budget, polling each pushed commit."""
    repair_attempt = 0
    label = "PR repair" if request.is_existing_pr else "repair"
    failure_label = "PR CI failure" if request.is_existing_pr else "CI failure"
    push_label = "PR repair" if request.is_existing_pr else "CI repair"
    while ci_result.state == "failed" and repair_attempt < CI_FIX_ATTEMPTS:
        repair_attempt += 1
        attempt = f"{repair_attempt}/{CI_FIX_ATTEMPTS}"
        set_active_execution(
            context,
            request.branch_name,
            f"Repairing {failure_label} (attempt {attempt})",
        )
        await reply_chunks(update, f"CI failed. Running {label} attempt {attempt}...")
        failure_context = await asyncio.to_thread(
            build_failure_context, request.pull_request.number, ci_result
        )
        result = await asyncio.to_thread(
            request.run,
            request.prompt(failure_context),
            request.branch_name,
            request.implementation_agent,
        )
        set_active_execution(
            context, request.branch_name, f"Pushing {push_label} (attempt {attempt})"
        )
        commit_sha = await asyncio.to_thread(
            push, request.branch_name, request.commit_message, "fix"
        )
        update_last_execution(
            context, request.branch_name, result, request.pull_request.url
        )
        set_active_execution(
            context,
            request.branch_name,
            f"Polling CI after {label} (attempt {attempt})",
        )
        ci_result = await watch_ci(update, commit_sha)
    return ci_result, repair_attempt


async def _report_execution(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ci_result: CiResult,
    repair_attempt: int,
) -> bool:
    """Update the captured test result and report completion when output exists."""
    execution = last_execution(context)
    if execution is None:
        return False
    tests = {"passed": "PASS", "failed": "FAIL"}.get(ci_result.state, "UNKNOWN")
    execution = replace(execution, tests=tests)
    context.user_data["last_execution"] = execution
    if ci_result.state == "passed":
        await reply_chunks(update, final_ci_status_message(ci_result))
    if ci_result.state == "failed":
        await reply_chunks(
            update, failed_ci_exhausted_message(ci_result, repair_attempt)
        )
    await reply_chunks(update, render_completion(execution, get_verbosity(context)))
    return True
