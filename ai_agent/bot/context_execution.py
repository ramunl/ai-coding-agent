"""Execution boundary shared by chat, inbox, worker and PR repair."""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from ai_agent.bot.transport import require_authorized
from ai_agent.config import CHAT_ID
from ai_agent.context_attempts import (
    Attempt,
    current_attempt,
    execution_control,
    unfinished,
)
from ai_agent.context_lifecycle import (
    current_context,
    new_spec,
    recovery_refusal,
    require_run,
)
from ai_agent.context_sources import ContextBlockedError
from ai_agent.plan_state import capture_repository_identity
from ai_agent.run_outcomes import NoCodeOutcomeError


def lifecycle_execution(
    function: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Keep the journal alive until serialized_execution's thread lease ends."""

    @wraps(function)
    async def wrapped(update: Any, context: Any, *args: Any, **kwargs: Any) -> Any:
        if not args and (
            not require_authorized(update)
            or not context.args
            or not context.args[0].isdigit()
        ):
            return await function(update, context, *args, **kwargs)
        data = context.user_data
        if recovery_refusal(data):
            raise ContextBlockedError(recovery_refusal(data))
        item = args[0] if args else {}
        require_run(item)
        spec = item.get("context_spec")
        if not spec and not args:  # Explicit owner request to repair an existing PR.
            spec = new_spec(str(CHAT_ID), capture_repository_identity())
        if spec is None:  # Existing saved tasks keep their legacy semantics.
            return await function(update, context, *args, **kwargs)
        if spec.get("scope", {}).get("owner_id") != str(CHAT_ID):
            raise ContextBlockedError("Context owner does not match authorized owner")
        if (
            args
            and item.get("repository_identity")
            and (item["repository_identity"] != spec["scope"].get("repository"))
        ):
            raise ContextBlockedError("Context repository differs from approved work")
        previous = next(
            (
                task
                for task in data.get("tasks", [])
                if task.get("id") == spec["task_id"]
            ),
            {},
        )
        if previous.get("context_handoff"):
            spec = {
                **spec,
                "candidates": [
                    *spec.get("candidates", []),
                    previous["context_handoff"],
                ],
            }
        attempt = Attempt(spec, item.get("branch_name", ""), data)
        if unfinished(attempt.store):
            data["context_recovery_pending"] = True
            raise ContextBlockedError("Unfinished attempt requires reconciliation")
        attempt.state["approval"] = item.get("approval_binding")
        attempt.state["run_digest"] = item.get("run_binding")
        attempt.save()
        control = execution_control.get()
        if control is not None:
            control["attempt"] = attempt
        token = current_attempt.set(attempt)
        context_token = current_context.set(attempt.request)
        try:
            result = await function(update, context, *args, **kwargs)
        except BaseException as error:
            stage = (
                "blocked"
                if isinstance(error, (ContextBlockedError, NoCodeOutcomeError))
                else "crash"
            )
            attempt.checkpoint(stage, {"exception_type": type(error).__name__})
            # The execution wrapper waits for provider threads and Git cleanup.
            # Before publication, a failed invocation is a durable incomplete
            # result, not an unfinished external effect. Explicit Resume can
            # then use the separate Git checkpoint without losing the handoff.
            publication = any(
                event["stage"] in {"push_intent", "pr_intent", "pr"}
                for event in attempt.state["events"].values()
            )
            if not publication and not attempt.failed:
                attempt.finish("blocked" if stage == "blocked" else "failed")
            else:
                data["context_recovery_pending"] = True
            raise
        else:
            events = list(attempt.state["events"].values())
            ci_key = attempt.state.get("latest", {}).get("ci")
            ci = attempt.state["events"].get(ci_key, {}).get("facts", {}).get("state")
            no_code = any(event["stage"] == "no_code" for event in events)
            if no_code or not events:
                attempt.finish("blocked")
            elif ci in {"passed", "failed"}:
                attempt.finish("succeeded" if ci == "passed" else "failed")
            else:
                data["context_recovery_pending"] = True
            return result
        finally:
            current_context.reset(context_token)
            current_attempt.reset(token)

    return wrapped
