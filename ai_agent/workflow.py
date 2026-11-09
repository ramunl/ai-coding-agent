"""Implement changes and publish their branches and pull requests."""

import hashlib
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path

from ai_agent import codex_models, isolation
from ai_agent.ai_tools import get_tool, tools_for_role
from ai_agent.claude_code_usage import (
    ClaudeCodeUsage,
    estimate_fields,
    parse_stream,
    provider_for_model,
    result_text,
)
from ai_agent.config import (
    CLAUDE_CODE_ALLOWED_TOOLS,
    CLAUDE_CODE_ARGS,
    IMPLEMENTATION_AGENT,
    IMPLEMENTATION_TIMEOUT_SECONDS,
    WORKER_ISOLATION_ENABLED,
)
from ai_agent.context_attempts import checkpoint, current_attempt, intent
from ai_agent.context_packets import build_packet, with_context
from ai_agent.context_sources import ContextRequest
from ai_agent.git_recovery import (
    _session,
    guarded_run,
    require_clean_checkout,
    retain_branch,
)
from ai_agent.github import PullRequest, ensure_github_configured, github_request
from ai_agent.image_delivery import run_with_images
from ai_agent.model_errors import codex_capacity_explained
from ai_agent.ops_context import OUTCOME_INSTRUCTIONS, diagnostic_context
from ai_agent.preflight import preflight
from ai_agent.projects import active_project
from ai_agent.run_outcomes import NoCodeOutcomeError
from ai_agent.shell import CommandResult, run
from ai_agent.token_usage import UsageRecord, record_usage
from ai_agent.usage_details import (
    UsageDetail,
    bounded_output_chars,
    current_attempt_id,
    new_call_id,
    record_detail,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImplementationResult:
    """Capture agent output, changed paths, and the resulting diff."""

    output: str
    files_changed: list[str]
    diff: str


SUPPORTED_IMPLEMENTATION_AGENTS = tuple(tools_for_role("executor"))
# GitHub rejects pull request titles longer than this with HTTP 422.
GITHUB_TITLE_MAX_LENGTH = 256


def normalize_implementation_agent(agent: str | None = None) -> str:
    """Resolve an implementation provider or reject an unsupported name."""
    value = (agent or IMPLEMENTATION_AGENT).strip().lower()
    supported = tools_for_role("executor")
    if value not in supported:
        raise ValueError(
            f"Unsupported implementation agent: {value}. Use {' or '.join(supported)}."
        )
    return value


def implementation_agent_label(agent: str | None = None) -> str:
    """Return the display name for an implementation provider."""
    selected = normalize_implementation_agent(agent)
    tool = get_tool(selected)
    return tool.label if tool else selected


def safe_claude_code_args() -> list[str]:
    """Adapt configured CLI arguments for execution as root."""
    args = list(CLAUDE_CODE_ARGS)
    if os.geteuid() != 0:
        return args

    safe_args: list[str] = []
    skip_next = False
    for index, value in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if value == "--dangerously-skip-permissions":
            continue
        if (
            value == "--permission-mode"
            and index + 1 < len(args)
            and args[index + 1] == "bypassPermissions"
        ):
            safe_args.extend(["--permission-mode", "acceptEdits"])
            skip_next = True
            continue
        safe_args.append(value)
    return safe_args


def claude_allowed_tools_args() -> list[str]:
    """Let Claude Code run tests, lint and read CI without a permission prompt."""
    if not CLAUDE_CODE_ALLOWED_TOOLS or "--allowedTools" in CLAUDE_CODE_ARGS:
        return []  # nothing to allow, or CLAUDE_CODE_ARGS already decides
    return ["--allowedTools", ",".join(CLAUDE_CODE_ALLOWED_TOOLS)]


def claude_context_control_args() -> list[str]:
    """Version-gated context/output-control flags for the Claude implementer.

    Logs and omits unsupported flags; never disturbs ``safe_claude_code_args``
    or ``claude_allowed_tools_args``.
    """
    from ai_agent.runner_controls import context_control_args

    return context_control_args("claude")


def codex_context_control_args() -> list[str]:
    """Version-gated context/output-control flags for the Codex implementer.

    Logs and omits unsupported flags; never disturbs the workspace-write
    sandbox or the model override.
    """
    from ai_agent.runner_controls import context_control_args

    return context_control_args("codex")


def implementation_command(prompt: str, agent: str | None = None) -> list[str]:
    """Build the selected provider command for an implementation prompt."""
    selected_agent = normalize_implementation_agent(agent)
    tool = get_tool(selected_agent)
    if tool is None:
        raise ValueError(f"Unsupported implementation agent: {selected_agent}.")
    return tool.command(prompt)


def _implementer_facts(agent: str) -> tuple[str, str, str | None] | None:
    """Runner/provider/model for a known implementer; None for unsupported."""
    if agent == "codex":
        model = codex_models.current_model()
        return (
            "codex",
            "codex",
            None if model == codex_models.DEFAULT_MODEL else model,
        )
    return None


def _implementer_tokens(agent: str, output: str) -> int | None:
    """The tool's reported token total, or None when it reports none."""
    tool = get_tool(agent)
    if tool is not None and tool.tokens is not None:
        return tool.tokens(output)
    return None


def _elapsed_seconds(started: float) -> int:
    """Whole seconds since ``started``, never negative."""
    return max(0, int(time.monotonic() - started))


def _with_output(result: CommandResult, output: str) -> CommandResult:
    """Return ``result`` with its output replaced.

    ``CommandResult`` is a frozen dataclass, so production results are rebuilt
    with :func:`dataclasses.replace`. Unit tests occasionally mock the runner
    with a plain ``SimpleNamespace``, which is mutable and not a dataclass, so
    its ``output`` is assigned in place instead.
    """
    if is_dataclass(result):
        return replace(result, output=output)
    result.output = output
    return result


def _claude_failure(detail: str) -> RuntimeError:
    """A bounded, prose-only failure so stream-json noise never reaches the UI."""
    detail = " ".join(detail.split())[:1000] or "Claude Code failed"
    return RuntimeError(detail)


def _record_implementer_detail(
    selected: str,
    attempt_id: str | None,
    call_id: str,
    started: float,
    result: CommandResult | None,
    tokens: int | None,
    usage: ClaudeCodeUsage | None,
) -> None:
    """Write the per-call detail record, surviving a cancelled/failed run."""
    if selected == "codex":
        facts = _implementer_facts(selected)
        record_detail(
            UsageDetail(
                role="implementer",
                runner=facts[0],
                provider=facts[1],
                model=facts[2],
                attempt_id=attempt_id,
                call_id=call_id,
                total_tokens=tokens,
                duration_seconds=_elapsed_seconds(started),
                output_chars=bounded_output_chars(
                    result.output if result is not None else None
                ),
            )
        )
        return
    # The claude runner is the Claude Code CLI wrapper. Its model-request
    # provider/model derive only from the reported model id, never from the
    # tool label or CLAUDE_CODE_AUTH.
    model = usage.model if usage is not None else None
    provider = provider_for_model(model) or "claude-code"
    record_detail(
        UsageDetail(
            role="implementer",
            runner="claude-code",
            provider=provider,
            model=model,
            attempt_id=attempt_id,
            call_id=call_id,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            cache_read_tokens=usage.cache_read_tokens if usage is not None else None,
            cache_creation_tokens=(
                usage.cache_creation_tokens if usage is not None else None
            ),
            reasoning_tokens=usage.reasoning_tokens if usage is not None else None,
            total_tokens=usage.tokens() if usage is not None else tokens,
            cost=usage.cost_usd if usage is not None else None,
            duration_seconds=_elapsed_seconds(started),
            output_chars=bounded_output_chars(
                result.output if result is not None else None
            ),
            **estimate_fields(
                model,
                usage.input_tokens if usage is not None else None,
                usage.output_tokens if usage is not None else None,
            ),
        )
    )


@with_context
def run_implementation_agent(
    prompt: str, agent: str | None = None, *, context: ContextRequest | None = None
) -> CommandResult:
    """Run a provider command, count its usage, and explain capacity failures.

    The Claude runner now asks for ``--output-format stream-json``, so its
    stdout is parsed back into prose and its tokens/provider/model are counted
    from the stream. The detailed record is written in a ``finally`` block so a
    run cancelled through ``implementation_stop`` still leaves an unknown-token
    record behind while ``git_recovery`` preserves the partial edits as it
    already does.
    """
    selected = normalize_implementation_agent(agent)
    preflight(selected)  # block before any paid work on a missing prerequisite
    intent("implementation")
    attempt_id = current_attempt_id()
    call_id = new_call_id()
    started = time.monotonic()
    result: CommandResult | None = None
    tokens: int | None = None
    usage = None  # ClaudeCodeUsage for the claude runner
    try:
        with codex_capacity_explained():
            run_kwargs: dict[str, object] = {
                "timeout": IMPLEMENTATION_TIMEOUT_SECONDS,
                "provider_run": True,
                "isolated": WORKER_ISOLATION_ENABLED,
            }
            sandbox = isolation.current_sandbox()
            if sandbox is not None:
                # The provider edits the disposable worktree, never repo_path.
                run_kwargs["cwd"] = sandbox.path
            result = run_with_images(
                implementation_command(
                    prompt + diagnostic_context() + OUTCOME_INSTRUCTIONS, agent
                ),
                run,
                **run_kwargs,
            )
        if selected == "claude":
            usage = parse_stream(result.output)
            prose = result_text(result.output)
            if usage.is_error and prose:
                raise _claude_failure(prose)
            result = _with_output(result, prose)
            tokens = usage.tokens()
        else:
            tokens = _implementer_tokens(selected, result.output)
        record_usage(UsageRecord("implementer", selected, tokens))
        return result
    except RuntimeError as error:
        if selected == "claude":
            # A failed/cancelled stream is embedded in the runner's error text;
            # recover its partial usage and keep the owner-facing prose clean.
            if usage is None:
                usage = parse_stream(str(error))
            prose = result_text(str(error))
            if prose:
                raise _claude_failure(prose) from None
        raise
    finally:
        _record_implementer_detail(
            selected, attempt_id, call_id, started, result, tokens, usage
        )


def slugify_branch_name(
    change_description: str, prefix: str = "feature", max_slug_length: int = 20
) -> str:
    """Build a valid, bounded branch name from a change description."""
    validate_branch_prefix(prefix)
    slug = change_description.lower().strip()
    slug = re.sub(r"[/\\:?*\[\]().]+", "-", slug)
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-./")
    if not slug:
        slug = "change"
    slug = truncate_slug(slug, max_slug_length)
    branch_name = f"{prefix}/{slug}"
    validate_branch_name(branch_name)
    return branch_name


def truncate_slug(slug: str, max_length: int) -> str:
    """Shorten a branch slug, preferring a word boundary."""
    if len(slug) <= max_length:
        return slug
    truncated = slug[:max_length]
    # Prefer cutting at a word boundary so we don't leave a chopped-off
    # fragment like "...slash-commands-im" (from "...commands-immediately").
    last_hyphen = truncated.rfind("-")
    if last_hyphen > 0:
        truncated = truncated[:last_hyphen]
    return truncated.strip("-./")


def truncate_title(title: str, max_length: int = GITHUB_TITLE_MAX_LENGTH) -> str:
    """Shorten a pull request title to GitHub's limit, ending with an ellipsis."""
    if len(title) <= max_length:
        return title
    ellipsis = "..."
    truncated = title[: max_length - len(ellipsis)]
    # Prefer cutting at a word boundary, as long as that keeps most of the title.
    last_space = truncated.rfind(" ")
    if last_space > max_length // 2:
        truncated = truncated[:last_space]
    return truncated.rstrip() + ellipsis


def validate_branch_prefix(prefix: str) -> None:
    """Reject prefixes containing characters outside the allowed set."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", prefix):
        raise ValueError(f"Invalid branch prefix: {prefix}")


def validate_branch_name(branch_name: str) -> None:
    """Reject unsupported branch names before invoking Git."""
    invalid = (
        branch_name.startswith("/")
        or branch_name.endswith("/")
        or branch_name.endswith(".")
        or ".." in branch_name
        or "@{" in branch_name
        or "\\" in branch_name
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch_name)
    )
    if invalid:
        raise ValueError(f"Invalid branch name: {branch_name}")


@with_context
@guarded_run
def implement(
    plan: str,
    branch_name: str,
    agent: str | None = None,
    *,
    context: ContextRequest | None = None,
) -> ImplementationResult:
    """Create a feature branch and capture the implementation result."""
    validate_branch_name(branch_name)
    base_branch = active_project().base_branch
    run(["git", "checkout", "--no-overwrite-ignore", base_branch])
    run(["git", "pull", "origin", base_branch])
    # Explicit start-fresh protects the previous tip before replacing it.
    # -B, not -b: a run that failed (a usage limit, a crash) leaves its branch
    # behind, and a retry must start again from the fresh base, not fail.
    retain_branch(branch_name)
    run(["git", "checkout", "--no-overwrite-ignore", "-B", branch_name])
    state = _session.get()
    if state and state.task_id:
        from ai_agent.checkpoints import begin

        begin(state.task_id, branch_name)
        state.checkpoint_started = True
    return _run_and_capture_changes(plan, agent, context)


@with_context
@guarded_run
def resume_implementation(
    plan: str,
    branch_name: str,
    agent: str | None = None,
    *,
    context: ContextRequest | None = None,
) -> ImplementationResult:
    """Restore this task's verified checkpoint without pulling/resetting base."""
    from ai_agent.checkpoints import restore

    validate_branch_name(branch_name)
    state = _session.get()
    if not state or not state.task_id or not state.resume:
        raise RuntimeError("Resume requires a task-bound checkpoint.")
    state.checkpoint_started = True
    saved = restore(state.task_id, branch_name)
    return _run_and_capture_changes(
        plan
        + "\n\nCONTINUE SAVED WORK: restored branch "
        + branch_name
        + " at "
        + saved["head"]
        + ". Inspect existing changes and tests first. "
        "Complete remaining approved work; do not reset or discard partial progress. "
        "This is a fresh model invocation, not a resumed provider conversation.",
        agent,
        context,
    )


@with_context
@guarded_run
def repair_implementation(
    prompt: str,
    branch_name: str,
    agent: str | None = None,
    *,
    context: ContextRequest | None = None,
) -> ImplementationResult:
    """Update an existing branch and capture a repair result."""
    validate_branch_name(branch_name)
    run(["git", "checkout", "--no-overwrite-ignore", branch_name])
    run(["git", "pull", "origin", branch_name])
    return _run_and_capture_changes(prompt, agent, context)


@with_context
@guarded_run
def repair_pull_request_branch(
    prompt: str,
    branch_name: str,
    agent: str | None = None,
    *,
    context: ContextRequest | None = None,
) -> ImplementationResult:
    """Check out a remote pull-request branch and capture its repair result."""
    validate_branch_name(branch_name)
    retain_branch(branch_name)
    run(["git", "fetch", "origin", branch_name])
    run(
        [
            "git",
            "checkout",
            "--no-overwrite-ignore",
            "-B",
            branch_name,
            f"origin/{branch_name}",
        ]
    )
    return _run_and_capture_changes(prompt, agent, context)


def _task_context() -> tuple[str, str]:
    """The recovery-bound task/branch for a sandbox name; '' when unbound."""
    state = _session.get()
    if state and state.task_id and state.branch:
        return state.task_id, state.branch
    branch = ""
    try:
        branch = run(["git", "branch", "--show-current"]).output.strip()
    except Exception:
        pass
    return "", branch or "work"


def _isolation_repo() -> Path | None:
    """The production checkout to sandbox, or None when there is no real one.

    Tests stub ``active_project`` with objects that carry no ``repo_path`` and
    fake projects whose checkout does not exist; both mean there is no real
    checkout to isolate, so the legacy direct run is used. Production always
    has a real checkout, so the disposable sandbox path is taken there.
    """
    repo = getattr(active_project(), "repo_path", None)
    if not repo:
        return None
    repo = Path(repo)
    return repo if repo.is_dir() else None


def _run_and_capture_changes(
    prompt: str, agent: str | None, context: ContextRequest | None = None
) -> ImplementationResult:
    """Capture provider output and the resulting Git changes consistently."""
    task_id, branch = _task_context()
    repo = _isolation_repo()

    def invoke() -> CommandResult:
        if context is not None:
            return run_implementation_agent(prompt, agent, context=context)
        return run_implementation_agent(prompt, agent)

    try:
        if context is not None:
            head = run(["git", "rev-parse", "HEAD"]).output.strip()
            context = replace(context, repository_head=head)
            packet = build_packet(context)
            attempt = current_attempt.get()
            if attempt:
                attempt.packets.append(packet.reference)
            checkpoint(
                "checkout",
                sha=head,
                packet=asdict(packet.reference),
                packet_metadata=packet.metadata.to_dict(),
            )
        if repo is None:
            agent_result = invoke()
        else:
            with isolation.sandbox_for(repo, task_id, branch) as sandbox:
                try:
                    agent_result = invoke()
                except BaseException:
                    # Re-apply partial sandbox edits before recovery preserves
                    # them.
                    try:
                        isolation.apply_sandbox(repo, sandbox)
                    except Exception as apply_error:
                        logger.warning(
                            "Could not re-apply sandbox partial edits: %s",
                            apply_error,
                        )
                    raise
                isolation.apply_sandbox(repo, sandbox)
    except BaseException:
        # Observe partial edits before guarded_run preserves them and restores
        # base. Never treat an interrupted provider's prose as evidence.
        if current_attempt.get() is not None:
            try:
                paths = changed_files()
                partial = run(["git", "diff", "--no-ext-diff", "HEAD"]).output
                checkpoint(
                    "diff",
                    sha256=hashlib.sha256(partial.encode()).hexdigest(),
                    files=paths,
                    digest_scope="tracked diff only; untracked paths listed",
                    model_tests="unverified",
                )
            except Exception:
                checkpoint("git_observation_unavailable", state="unknown")
        raise
    run(["git", "add", "-N", "."])
    files_changed = changed_files()
    if not files_changed:
        files_changed = run(
            ["git", "diff", "--name-only", active_project().base_branch + "...HEAD"]
        ).output.splitlines()
        if not files_changed:
            raise NoCodeOutcomeError(agent_result.output)
    diff = run(["git", "diff", "--no-ext-diff"]).output
    checkpoint(
        "diff",
        sha256=hashlib.sha256(diff.encode()).hexdigest(),
        files=files_changed,
        model_tests="unverified",
    )
    return ImplementationResult(
        output=agent_result.output, files_changed=files_changed, diff=diff
    )


def return_to_base_branch() -> None:
    """Check out and update the active project base branch."""
    require_clean_checkout()
    base_branch = active_project().base_branch
    run(["git", "checkout", "--no-overwrite-ignore", base_branch])
    run(["git", "pull", "origin", base_branch])


def changed_files() -> list[str]:
    """Return unique paths reported by Git status."""
    output = run(["git", "status", "--porcelain"]).output
    names = [line[3:].strip() for line in output.splitlines() if len(line) > 3]
    return sorted(set(names))


def has_changes() -> bool:
    """Check whether the active repository has uncommitted changes."""
    return bool(run(["git", "status", "--porcelain"]).output.strip())


def push(branch_name: str, change_name: str, commit_type: str = "feat") -> str:
    """Commit and push changes, returning the resulting commit SHA."""
    validate_branch_name(branch_name)
    if (
        not has_changes()
        and not run(
            ["git", "log", "--oneline", active_project().base_branch + "..HEAD"]
        ).output.strip()
    ):
        raise RuntimeError(
            "There are no uncommitted file changes to publish. "
            "No commit or pull request was created."
        )
    if has_changes():
        run(["git", "add", "."])
        intent("commit")
        run(["git", "commit", "-m", f"{commit_type}: {change_name}"])
    sha = run(["git", "rev-parse", "HEAD"]).output.strip()
    checkpoint("commit", sha=sha, branch=branch_name)
    intent("push")
    run(["git", "push", "origin", branch_name])
    checkpoint("push", sha=sha, branch=branch_name)
    return sha


def create_pull_request(
    branch_name: str,
    change_name: str,
    body_text: str,
    title_type: str = "feat",
    body_label: str = "Plan",
) -> PullRequest:
    """Open a pull request against the active project base branch."""
    ensure_github_configured()
    validate_branch_name(branch_name)
    payload = {
        "title": truncate_title(f"{title_type}: {change_name}"),
        "head": branch_name,
        "base": active_project().base_branch,
        "body": f"Generated by Channel Cast Agent.\n\n{body_label}:\n\n{body_text}",
        "maintainer_can_modify": True,
    }
    attempt = current_attempt.get()
    if attempt:
        repository = active_project().github_repository
        existing = github_request(
            "GET",
            f"/repos/{repository}/pulls",
            query={
                "state": "open",
                "head": f"{repository.split('/')[0]}:{branch_name}",
                "base": active_project().base_branch,
                "per_page": 100,
            },
        )
        if existing:
            if len(existing) != 1:
                raise RuntimeError(
                    "Ambiguous existing pull requests; recovery required"
                )
            response = existing[0]
            local_sha = run(["git", "rev-parse", "HEAD"]).output.strip()
            if response["head"]["sha"] != local_sha:
                raise RuntimeError(
                    "PR head differs from local commit; recovery required"
                )
            checkpoint(
                "pr",
                number=int(response["number"]),
                url=response["html_url"],
                sha=response["head"]["sha"],
            )
            return PullRequest(int(response["number"]), response["html_url"], local_sha)
    intent("pr")
    response = github_request(
        "POST", f"/repos/{active_project().github_repository}/pulls", payload
    )
    checkpoint(
        "pr",
        number=int(response["number"]),
        url=response["html_url"],
        sha=response["head"]["sha"],
    )
    return PullRequest(
        number=int(response["number"]),
        url=response["html_url"],
        head_sha=response["head"]["sha"],
    )
