"""Secret-free, historical AI selections for task cards; never execution authority."""

import os

from ai_agent import config
from ai_agent.provider_capabilities import valid_model_id


def public_selection(value: object) -> dict | None:
    """Accept only bounded model/tool identifiers, never URLs, prompts or env maps."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("tool", "model"):
        item = value.get(key)
        if isinstance(item, str) and valid_model_id(item):
            result[key] = item
    return result if result.get("tool") else None


def capture(tool: str, role: str) -> dict:
    """Capture configured invocation identity, keeping CLI-default models unknown."""
    from ai_agent.ai_tools import get_tool

    model = None
    if tool in {"claude", "claude-code"}:
        api = (
            role == "planner"
            and tool == "claude"
            and config.CLAUDE_PLANNER_VIA == "api"
        )
        if api:
            model = config.ANTHROPIC_MODEL
        else:
            if role == "planner":
                model = config.CLAUDE_PLANNER_MODEL
            else:
                args = config.CLAUDE_CODE_ARGS
                for index, arg in enumerate(args):
                    if arg == "--model" and index + 1 < len(args):
                        model = args[index + 1]
                    elif arg.startswith("--model="):
                        model = arg.split("=", 1)[1]
            model = model or os.environ.get("ANTHROPIC_MODEL")
            tool = "claude-code"
    elif selected := get_tool(tool):
        model = selected.current_model()
    return public_selection({"tool": tool, "model": model}) or {"tool": "unknown"}


def task_ai(task: dict) -> dict:
    """Project retained facts only; never retrofit today's model onto old runs."""
    run = task.get("run") or {}
    implementation = public_selection(task.get("implementation_ai"))
    if implementation is None:
        implementation = public_selection(run.get("implementation_ai"))
    if implementation is None:
        implementation = public_selection(
            {
                "tool": run.get("implementation_agent"),
                "model": run.get("implementation_model"),
            }
        )
    return {
        "planner": public_selection(task.get("planning_ai")),
        "split": public_selection(task.get("split_ai")),
        "implementer": implementation,
    }


def record_execution(tool: str, model: str | None = None) -> None:
    """Attach the current invocation to its exact task, including CI repairs."""
    from ai_agent.context_attempts import current_attempt

    attempt = current_attempt.get()
    if attempt is None:
        return
    selection = capture(tool, "executor")
    if model and valid_model_id(model):
        selection["model"] = model
    for task in attempt.data.get("tasks", []):
        if (
            task.get("id") == attempt.request.task_id
            and task.get("branch") == attempt.state["branch"]
            and task.get("repo")
            == attempt.state["spec"]["scope"]["repository"]["project_name"]
        ):
            task["implementation_ai"] = selection
