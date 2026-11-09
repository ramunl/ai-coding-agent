"""Bounded, cancellation-aware OpenCode executor runner.

Runs ``opencode run --format json`` through the same grouped, cancellation-aware
subprocess path the other implementers use
(:func:`ai_agent.provider_process.grouped_run`), streams its JSON events and
reduces them to a bounded :class:`OpenCodeRun`. Success requires a ``result``
event (and, in the caller, repository changes): a zero exit alone is a false
success and is rejected by ``workflow``.

Output is bounded in two places: the process reader truncates captured stdout to
``OPENCODE_MAX_OUTPUT_CHARS`` (keeping the tail so the trailing result event
survives), and the returned prose is a bounded reduction, never the raw stream.
Every truncation is marked explicitly rather than silently dropped.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class OpenCodeRun:
    """One reduced OpenCode run: prose, usage, and its outcome contract."""

    output: str
    usage: dict | None
    error: str | None
    error_kind: str | None
    cancelled: bool
    exit_code: int
    has_result: bool
    truncated: bool


def _run_text(result: object) -> tuple[str, bool, int]:
    """Extract captured text, the truncation flag and the exit code from a result."""
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    output = getattr(result, "output", None)
    if stdout is not None:
        text = (stdout or "") + ("" if stderr is None else (stderr or ""))
    elif output is not None:
        text = output or ""
    else:
        text = ""
    truncated = bool(getattr(result, "truncated", False))
    exit_code = getattr(result, "returncode", 0)
    return text, truncated, exit_code


def run_stream(
    command: list[str],
    run: Callable[..., Any] | None = None,
    *,
    max_output_chars: int | None = None,
    **kwargs: Any,
) -> OpenCodeRun:
    """Run the OpenCode command and reduce its JSON events to a bounded result.

    ``run`` is the subprocess runner (``grouped_run`` in production, a fake in
    tests); it must accept a ``max_output_chars`` bound. Exceptions the runner
    raises for owner stop and denial-loop blocking propagate unchanged;
    ``subprocess.TimeoutExpired`` is normalized to a bounded ``RuntimeError``.
    """
    from ai_agent.claude_env import provider_env
    from ai_agent.config import DEEPSEEK_API_KEY, OPENCODE_MAX_OUTPUT_CHARS
    from ai_agent.providers import opencode as contract

    if run is None:
        from ai_agent.provider_process import grouped_run

        run = grouped_run
    if max_output_chars is None:
        max_output_chars = OPENCODE_MAX_OUTPUT_CHARS
    env = kwargs.get("env")
    if env is None:
        env = provider_env(worker=bool(kwargs.get("isolated", False)))
    env = dict(env)
    # OpenCode reads its DeepSeek backend key from OPENCODE_API_KEY (see
    # scripts/opencode_characterize.sh). The key is otherwise stripped from the
    # provider environment and never logged or persisted.
    if DEEPSEEK_API_KEY:
        env["OPENCODE_API_KEY"] = DEEPSEEK_API_KEY
    kwargs["env"] = env
    kwargs.setdefault("text", True)
    try:
        result = run(list(command), max_output_chars=max_output_chars, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"OpenCode run timed out after {error.timeout}s.") from None
    text, truncated, exit_code = _run_text(result)
    events = contract.parse_events(text)
    outcome = contract.classify_run(events, exit_code)
    prose = contract.result_text(events)
    if truncated and prose:
        prose = prose + "\n[opencode output truncated; captured tail only]"
    return OpenCodeRun(
        output=prose,
        usage=outcome["usage"],
        error=outcome["error"],
        error_kind=outcome["error_kind"],
        cancelled=outcome["cancelled"],
        exit_code=outcome["exit_code"],
        has_result=any(
            isinstance(event, dict) and event.get("type") == contract.EVENT_RESULT
            for event in events
        ),
        truncated=truncated,
    )
