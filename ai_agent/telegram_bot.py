import asyncio
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ai_agent.anthropic_limits import get_anthropic_limits
from ai_agent.ci import evaluate_ci
from ai_agent.codex_status import get_codex_status
from ai_agent.config import (
    CHAT_ID,
    CI_POLL_INTERVAL_SECONDS,
    CI_TIMEOUT_SECONDS,
    COMMAND_TIMEOUT_SECONDS,
    GITHUB_REPOSITORY,
    MAX_LOG_LINES,
    MAX_TELEGRAM_MESSAGE_LENGTH,
    TELEGRAM_TOKEN,
    redact_sensitive,
)
from ai_agent.github import ensure_github_configured, github_request
from ai_agent.plan_state import (
    ExecutionState,
    Verbosity,
    new_plan_state,
    parse_plan_document,
    parse_verbosity,
    render_completion,
    render_diff_summary,
    render_history,
    render_plan,
    revise_plan_state,
)
from ai_agent.planner import assess_bugfix_report, build_bugfix_prompt, bugfix_questions, plan_feature, revise_feature_plan
from ai_agent.shell import run
from ai_agent.test_runner import run_unit_tests
from ai_agent.workflow import create_pull_request, implement, push, slugify_branch_name


logger = logging.getLogger(__name__)


def is_authorized(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID)


def require_authorized(update: Update) -> bool:
    if is_authorized(update):
        return True
    logger.warning("Ignoring unauthorized update from chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    return False


async def reply_chunks(update: Update, text: str) -> None:
    if not update.message:
        return

    text = redact_sensitive(text)
    if not text:
        await update.message.reply_text("(no output)")
        return

    for start in range(0, len(text), MAX_TELEGRAM_MESSAGE_LENGTH):
        await update.message.reply_text(text[start : start + MAX_TELEGRAM_MESSAGE_LENGTH])


def get_verbosity(context: ContextTypes.DEFAULT_TYPE) -> Verbosity:
    value = context.user_data.get("verbosity", Verbosity.CONCISE.value)
    return parse_verbosity(str(value)) or Verbosity.CONCISE


def set_pending_from_plan(context: ContextTypes.DEFAULT_TYPE, plan_state) -> None:
    document = parse_plan_document(plan_state.plan_text, plan_state.feature)
    context.user_data["pending_implementation"] = {
        "change": plan_state.feature,
        "codex_prompt": document.codex_prompt,
        "branch_name": document.branch,
        "commit_type": "feat",
        "pr_body_label": f"Plan revision {plan_state.revision}",
        "confirmation_label": "implementation",
    }


def forget_pending_implementation(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_implementation", None)


def last_execution(context: ContextTypes.DEFAULT_TYPE) -> ExecutionState | None:
    execution = context.user_data.get("last_execution")
    return execution if isinstance(execution, ExecutionState) else None


def extract_file_diff(diff_text: str, file_name: str) -> str:
    chunks = []
    current = []
    in_target = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if in_target and current:
                chunks.append("\n".join(current))
            current = [line]
            in_target = file_name in line
            continue
        if in_target:
            current.append(line)
    if in_target and current:
        chunks.append("\n".join(current))
    return "\n\n".join(chunks)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    await reply_chunks(
        update,
        "Channel Cast Agent ready.\n\n"
        "Planning workflow:\n"
        "1. /plan <feature>\n"
        "2. /discuss <feedback> as needed\n"
        "3. /approve\n"
        "4. /confirm\n\n"
        "Commands:\n"
        "/plan <feature> - create a plan for discussion\n"
        "/discuss <feedback> - revise the current plan\n"
        "/approve - approve the current plan before implementation\n"
        "/showplan - show the current plan\n"
        "/history - show plan revisions\n"
        "/implement <feature> - shortcut: plan, approve, then wait for /confirm\n"
        "/bugfix <bug> - clarify if needed, then wait for /confirm on a bugfix branch\n"
        "/answer <details> - answer pending bugfix clarification questions\n"
        "/confirm - run approved work quietly, open PR, and poll CI\n"
        "/verbosity concise|normal|debug - set output detail\n"
        "/diff - show changed files and line counts from the last run\n"
        "/show <file-number> - show a specific file diff from the last run\n"
        "/logs [lines] - last run logs in debug mode, or service logs when no run exists\n"
        "/pr - show the last PR URL\n"
        "/cancel - discard the pending implementation\n"
        "/ci <pr-number> - show current GitHub Actions result for a PR\n"
        "/limits - show remaining Claude API rate limits\n"
        "/codex - show Codex CLI/login status\n"
        "/test - run agent unit tests\n"
        "/branches - list branches\n"
        "/status - git status\n"
        "/help - show this help",
    )


async def branches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    result = await asyncio.to_thread(run, ["git", "branch", "-a"])
    await reply_chunks(update, f"Branches:\n{result.output}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    result = await asyncio.to_thread(run, ["git", "status"])
    await reply_chunks(update, f"Status:\n{result.output}")


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    execution = last_execution(context)
    if execution:
        if get_verbosity(context) == Verbosity.DEBUG:
            await reply_chunks(update, f"Logs:\n{execution.logs or '(no logs captured)'}")
        else:
            await reply_chunks(update, "Logs are available in debug mode. Use /verbosity debug, then /logs.")
        return

    requested_lines = int(context.args[0]) if context.args and context.args[0].isdigit() else 60
    lines = max(1, min(requested_lines, MAX_LOG_LINES))
    result = await asyncio.to_thread(
        run,
        ["journalctl", "-u", "ai-agent.service", "-n", str(lines), "--no-pager"],
        Path("/"),
        COMMAND_TIMEOUT_SECONDS,
    )
    await reply_chunks(update, f"Logs:\n{result.output}")


async def limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    result = await asyncio.to_thread(get_anthropic_limits)
    await reply_chunks(update, result)


async def codex_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    result = await asyncio.to_thread(get_codex_status)
    await reply_chunks(update, result)


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    await reply_chunks(update, "Running agent unit tests...")
    result = await asyncio.to_thread(run_unit_tests)
    await reply_chunks(update, result)


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    feature = " ".join(context.args).strip()
    if not feature:
        await reply_chunks(update, "Usage: /plan <feature description>")
        return

    await reply_chunks(update, "Planning with Claude...")
    plan_text = await asyncio.to_thread(plan_feature, feature)
    plan_state = new_plan_state(feature, plan_text)
    context.user_data["pending_plan"] = plan_state
    forget_pending_implementation(context)
    await reply_chunks(update, render_plan(plan_state))


async def discuss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    plan_state = context.user_data.get("pending_plan")
    if not plan_state:
        await reply_chunks(update, "No pending plan. Use /plan <feature> first.")
        return

    feedback = " ".join(context.args).strip()
    if not feedback:
        await reply_chunks(update, "Usage: /discuss <plan feedback>")
        return

    await reply_chunks(update, "Revising plan with Claude...")
    plan_text = await asyncio.to_thread(revise_feature_plan, plan_state.feature, plan_state.plan_text, feedback)
    revised = revise_plan_state(plan_state, plan_text)
    context.user_data["pending_plan"] = revised
    forget_pending_implementation(context)
    await reply_chunks(update, f"Plan updated (Revision {revised.revision})\n\n{render_plan(revised)}")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await reply_chunks(update, f"Plan approved.\n\nBranch:\n{document.branch}\n\nCommands:\n- /confirm\n- /cancel")


async def showplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    plan_state = context.user_data.get("pending_plan")
    if not plan_state:
        await reply_chunks(update, "No pending plan.")
        return
    await reply_chunks(update, render_plan(plan_state))


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    plan_state = context.user_data.get("pending_plan")
    if not plan_state:
        await reply_chunks(update, "No pending plan history.")
        return
    await reply_chunks(update, render_history(plan_state))


async def verbosity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    if not context.args:
        await reply_chunks(update, f"Verbosity: {get_verbosity(context).value}")
        return
    selected = parse_verbosity(context.args[0])
    if not selected:
        await reply_chunks(update, "Usage: /verbosity concise|normal|debug")
        return
    context.user_data["verbosity"] = selected.value
    await reply_chunks(update, f"Verbosity set to {selected.value}.")


async def implement_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    feature = " ".join(context.args).strip()
    if not feature:
        await reply_chunks(update, "Usage: /implement <feature description>")
        return

    await reply_chunks(update, "Planning with Claude...")
    plan_text = await asyncio.to_thread(plan_feature, feature)
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
        "Send /confirm to run Codex and push, or /cancel to discard this request.",
    )


async def bugfix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    bug = " ".join(context.args).strip()
    if not bug:
        await reply_chunks(update, "Usage: /bugfix <bug description>")
        return

    await reply_chunks(update, "Checking whether the bug report is actionable...")
    questions = await asyncio.to_thread(get_bugfix_questions, bug)
    if questions:
        context.user_data["pending_bugfix_clarification"] = {"bug": bug, "branch_source": bug}
        await reply_chunks(
            update,
            f"I need a bit more detail before running Codex:\n{questions}\n\n"
            "Reply with /answer <details>, or /cancel to discard this request.",
        )
        return

    await prepare_bugfix(update, context, bug, bug)


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    pending = context.user_data.get("pending_bugfix_clarification")
    if not pending:
        await reply_chunks(update, "No pending bugfix questions. Use /bugfix <bug> first.")
        return

    details = " ".join(context.args).strip()
    if not details:
        await reply_chunks(update, "Usage: /answer <details>")
        return

    combined_bug = f"{pending['bug']}\n\nUser clarification:\n{details}"
    branch_source = pending.get("branch_source", pending["bug"])
    await reply_chunks(update, "Checking the updated bug report...")
    questions = await asyncio.to_thread(get_bugfix_questions, combined_bug)
    if questions:
        context.user_data["pending_bugfix_clarification"] = {"bug": combined_bug, "branch_source": branch_source}
        await reply_chunks(
            update,
            f"I still need more detail:\n{questions}\n\n"
            "Reply with /answer <details>, or /cancel to discard this request.",
        )
        return

    context.user_data.pop("pending_bugfix_clarification", None)
    await prepare_bugfix(update, context, combined_bug, branch_source)


def get_bugfix_questions(bug: str) -> str | None:
    return bugfix_questions(assess_bugfix_report(bug))


async def prepare_bugfix(update: Update, context: ContextTypes.DEFAULT_TYPE, bug: str, branch_source: str) -> None:
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
        "Send /confirm to run Codex and push, or /cancel to discard this request.",
    )


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return

    pending = context.user_data.get("pending_implementation")
    plan_state = context.user_data.get("pending_plan")
    if not pending and plan_state and plan_state.approved:
        set_pending_from_plan(context, plan_state)
        pending = context.user_data.get("pending_implementation")
    if not pending and plan_state and not plan_state.approved:
        await reply_chunks(update, "Plan is not approved yet. Use /approve before /confirm.")
        return
    if not pending:
        await reply_chunks(update, "No pending implementation. Use /implement <feature> or /bugfix <bug> first.")
        return

    change = pending["change"]
    codex_prompt = pending["codex_prompt"]
    branch_name = pending["branch_name"]
    commit_type = pending.get("commit_type", "feat")
    pr_body_label = pending.get("pr_body_label", "Plan")
    confirmation_label = pending.get("confirmation_label", "implementation")

    await asyncio.to_thread(ensure_github_configured)

    await reply_chunks(update, f"Task started.\n\nBranch:\n{branch_name}\n\nStatus:\nRUNNING")
    implementation_result = await asyncio.to_thread(implement, codex_prompt, branch_name)

    await reply_chunks(update, "Committing and pushing branch...")
    commit_sha = await asyncio.to_thread(push, branch_name, change, commit_type)

    await reply_chunks(update, "Opening GitHub PR...")
    pull_request = await asyncio.to_thread(
        create_pull_request,
        branch_name,
        change,
        codex_prompt,
        commit_type,
        pr_body_label,
    )
    context.user_data["last_execution"] = ExecutionState(
        branch=branch_name,
        files_changed=implementation_result.files_changed,
        diff_summary=render_diff_summary(implementation_result.diff, implementation_result.files_changed),
        full_diff=implementation_result.diff,
        logs=implementation_result.output,
        pr_url=pull_request.url,
        tests="PENDING",
    )

    if get_verbosity(context) != Verbosity.CONCISE:
        await reply_chunks(update, f"PR opened: {pull_request.url}\nHead: {pull_request.head_sha or commit_sha}")

    ci_result = await watch_ci(update, pull_request.head_sha or commit_sha)

    execution = last_execution(context)
    if execution:
        tests = "PASS" if ci_result.state == "passed" else "FAIL" if ci_result.state == "failed" else "UNKNOWN"
        context.user_data["last_execution"] = ExecutionState(
            branch=execution.branch,
            files_changed=execution.files_changed,
            diff_summary=execution.diff_summary,
            full_diff=execution.full_diff,
            logs=execution.logs,
            pr_url=execution.pr_url,
            tests=tests,
        )

    context.user_data.pop("pending_implementation", None)
    if plan_state:
        context.user_data.pop("pending_plan", None)
    execution = last_execution(context)
    if execution:
        await reply_chunks(update, render_completion(execution, get_verbosity(context)))
    else:
        await reply_chunks(update, f"Done with {confirmation_label}.\nBranch: {branch_name}\nPR: {pull_request.url}")


async def watch_ci(update: Update, head_sha: str):
    deadline = asyncio.get_running_loop().time() + CI_TIMEOUT_SECONDS
    last_summary = None

    while True:
        result = await asyncio.to_thread(evaluate_ci, head_sha)
        if result.summary != last_summary:
            message = result.summary
            if result.url:
                message = f"{message}\n{result.url}"
            await reply_chunks(update, message)
            last_summary = result.summary

        if result.state in {"passed", "failed"}:
            return result

        if asyncio.get_running_loop().time() >= deadline:
            await reply_chunks(update, f"CI polling timed out after {CI_TIMEOUT_SECONDS}s for {head_sha}")
            return result

        await asyncio.sleep(CI_POLL_INTERVAL_SECONDS)


async def ci(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    if not context.args or not context.args[0].isdigit():
        await reply_chunks(update, "Usage: /ci <pr-number>")
        return

    pr_number = int(context.args[0])
    ensure_github_configured()
    pull_data = await asyncio.to_thread(github_request, "GET", f"/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}")
    head_sha = pull_data["head"]["sha"]
    result = await asyncio.to_thread(evaluate_ci, head_sha)
    message = f"PR #{pr_number}: {result.summary}"
    if result.url:
        message = f"{message}\n{result.url}"
    await reply_chunks(update, message)


async def diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    execution = last_execution(context)
    if not execution:
        await reply_chunks(update, "No implementation diff is available yet.")
        return
    await reply_chunks(update, execution.diff_summary)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    execution = last_execution(context)
    if not execution:
        await reply_chunks(update, "No implementation diff is available yet.")
        return
    if not context.args:
        await reply_chunks(update, "Usage: /show <file-number>")
        return

    selector = context.args[0]
    file_name = ""
    if selector.isdigit():
        index = int(selector) - 1
        if index < 0 or index >= len(execution.files_changed):
            await reply_chunks(update, "File number is out of range. Use /diff to list files.")
            return
        file_name = execution.files_changed[index]
    else:
        file_name = " ".join(context.args).strip()

    file_diff = extract_file_diff(execution.full_diff, file_name)
    if not file_diff:
        await reply_chunks(update, f"No diff captured for {file_name}.")
        return
    await reply_chunks(update, file_diff)


async def pr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    execution = last_execution(context)
    if not execution or not execution.pr_url:
        await reply_chunks(update, "No PR is available yet.")
        return
    await reply_chunks(update, execution.pr_url)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_authorized(update):
        return
    context.user_data.pop("pending_implementation", None)
    context.user_data.pop("pending_plan", None)
    context.user_data.pop("pending_bugfix_clarification", None)
    await reply_chunks(update, "Pending request discarded.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc_info = None
    if context.error:
        exc_info = (type(context.error), context.error, context.error.__traceback__)
    logger.error("Unhandled Telegram handler error", exc_info=exc_info)
    if isinstance(update, Update) and is_authorized(update):
        error_text = str(context.error or "unknown error")
        lines = error_text.splitlines()
        if len(lines) > 20:
            error_text = "\n".join(lines[:20]) + "\n... truncated. Use /verbosity debug and /logs when run logs are available."
        await reply_chunks(update, f"Error:\n{error_text}")


def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("discuss", discuss))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("showplan", showplan))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("verbosity", verbosity))
    app.add_handler(CommandHandler("implement", implement_cmd))
    app.add_handler(CommandHandler("bugfix", bugfix_cmd))
    app.add_handler(CommandHandler("answer", answer))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("ci", ci))
    app.add_handler(CommandHandler("diff", diff))
    app.add_handler(CommandHandler("show", show))
    app.add_handler(CommandHandler("pr", pr))
    app.add_handler(CommandHandler("limits", limits))
    app.add_handler(CommandHandler("codex", codex_status))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CommandHandler("branches", branches))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_error_handler(error_handler)
    return app
