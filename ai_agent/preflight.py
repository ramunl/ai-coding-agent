"""Deterministic preflight of the paid implementation environment.

Before dispatching a paid implementation or CI-repair run, resolve the exact
child environment that ``ai_agent.shell.run`` would use and verify the known
prerequisites on that environment, without any paid inference. A missing
prerequisite raises ``PreflightBlockedError`` with a secret-free diagnostic naming
the Ops action the owner must complete first.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ai_agent import codex_models, isolation
from ai_agent.ai_tools import get_tool, tools_for_role
from ai_agent.claude_env import provider_env
from ai_agent.config import (
    CLAUDE_CODE_ALLOWED_TOOLS,
    CLAUDE_CODE_ARGS,
    IMPLEMENTATION_AGENT,
    WORKER_ISOLATION_ENABLED,
)
from ai_agent.projects import active_project

# Verification commands the repository documents for local verification
# (scripts/baseline.sh mirrors CI). These must stay in sync with the approved
# policy: CLAUDE_CODE_ALLOWED_TOOLS for Claude Code, the workspace-write
# sandbox for Codex. A newly required command missing from that policy blocks
# here, deterministically, instead of running without verified checks.
VERIFICATION_COMMANDS = (
    "pytest",
    "ruff check",
    "ruff format",
    "scripts/baseline.sh",
)

# Local tools the provider is expected to invoke for verification.
VERIFICATION_BINARIES = ("pytest", "ruff")

PROBE_TIMEOUT_SECONDS = 30

_MODEL = re.compile(r"[A-Za-z0-9._-]{1,100}")
_ALLOWED_BASH = re.compile(r"^Bash\((.*):\*\)$")

_VERIFICATION_TOOL_LABELS = {"pytest": "pytest", "ruff": "Ruff"}


@dataclass(frozen=True)
class PreflightPolicy:
    """The effective dispatch environment, resolved once per run."""

    agent: str
    runner: str
    tool: str
    model: str | None
    isolated: bool
    cwd: Path
    env: Mapping[str, str]
    path: str


class PreflightBlockedError(RuntimeError):
    """A known prerequisite is missing before a paid run; Ops must act first."""

    status = "ops_required"

    def __init__(self, *, action: str, reason: str) -> None:
        """Keep the diagnostic secret-free and name the Ops action."""
        self.action = action
        self.reason = reason
        self.reported = False
        self.report = f"Preflight blocked: {reason}. Ops action: {action}."
        super().__init__(self.report)


def _normalize_agent(agent: str | None) -> str:
    """Resolve the implementation agent or reject an unsupported name."""
    value = (agent or IMPLEMENTATION_AGENT).strip().lower()
    supported = tools_for_role("executor")
    if value not in supported:
        raise ValueError(
            f"Unsupported implementation agent: {value}. Use {' or '.join(supported)}."
        )
    return value


def resolve_policy(
    agent: str | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    isolated: bool | None = None,
) -> PreflightPolicy:
    """Resolve the exact child environment ``shell.run`` uses for a provider run."""
    selected = _normalize_agent(agent)
    tool = get_tool(selected)
    if tool is None:
        raise ValueError(f"Unsupported implementation agent: {selected}.")
    if selected == "codex":
        runner = "codex"
        provider = "codex"
    elif selected == "claude":
        runner = "claude"
        provider = "claude-code"
    else:
        runner = selected
        provider = selected
    model = codex_models.current_model() if selected == "codex" else None
    use_isolation = WORKER_ISOLATION_ENABLED if isolated is None else isolated
    # shell.run builds the provider env with env_for(args), then replaces it
    # with provider_env(worker=True) exactly when isolated is set. Reusing
    # provider_env here keeps preflight on the same PATH/HOME as real dispatch.
    child_env = dict(provider_env(worker=use_isolation)) if env is None else dict(env)
    if cwd is None:
        sandbox = isolation.current_sandbox()
        cwd = sandbox.path if sandbox is not None else active_project().repo_path
    path = child_env.get("PATH") or os.defpath
    return PreflightPolicy(
        agent=selected,
        runner=runner,
        tool=provider,
        model=model,
        isolated=use_isolation,
        cwd=cwd,
        env=child_env,
        path=path,
    )


def run_probe(args: list[str], policy: PreflightPolicy) -> bool:
    """Run one non-inference CLI probe with the child env; True on exit 0.

    Output is discarded entirely: a login status line can contain account
    details and must never reach a log or an error message.
    """
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            cwd=str(policy.cwd),
            env=dict(policy.env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def allowed_bash_commands(allowed_tools: tuple[str, ...]) -> tuple[str, ...]:
    """Return the command prefixes Claude Code may run under ``Bash(...:*)``."""
    commands: list[str] = []
    for entry in allowed_tools:
        match = _ALLOWED_BASH.match(entry)
        if match:
            commands.append(match.group(1).strip())
    return tuple(commands)


def verification_command_allowed(command: str, allowed: tuple[str, ...]) -> bool:
    """Whether one documented verification command is covered by the policy."""
    return any(
        command == prefix or command.startswith(prefix + " ") for prefix in allowed
    )


def effective_claude_allowed_tools() -> tuple[str, ...]:
    """The allowed tools Claude Code actually receives, mirroring workflow."""
    if "--allowedTools" in CLAUDE_CODE_ARGS:
        index = CLAUDE_CODE_ARGS.index("--allowedTools")
        if index + 1 < len(CLAUDE_CODE_ARGS):
            return tuple(
                tool.strip()
                for tool in CLAUDE_CODE_ARGS[index + 1].split(",")
                if tool.strip()
            )
        return ()
    return CLAUDE_CODE_ALLOWED_TOOLS


def unapproved_verification_commands(
    agent: str, commands: tuple[str, ...] = VERIFICATION_COMMANDS
) -> tuple[str, ...]:
    """Documented verification commands the approved policy does not cover."""
    if agent != "claude":
        # Codex runs under the workspace-write sandbox, which approves them.
        return ()
    allowed = allowed_bash_commands(effective_claude_allowed_tools())
    return tuple(
        command
        for command in commands
        if not verification_command_allowed(command, allowed)
    )


_PROBES = {
    "codex": (
        (
            ("codex", "--version"),
            "Install or repair the Codex CLI on the worker PATH",
            "codex --version did not run",
        ),
        (
            ("codex", "login", "status"),
            "Log the worker into Codex (codex login)",
            "codex login status did not report a login",
        ),
    ),
    "claude": (
        (
            ("claude", "--version"),
            "Install or repair the Claude Code CLI on the worker PATH",
            "claude --version did not run",
        ),
        (
            ("claude", "config", "stat"),
            "Complete Claude Code login and configuration for the worker",
            "claude config stat did not report a readable configuration",
        ),
    ),
}


def _check_runner(policy: PreflightPolicy) -> None:
    if shutil.which(policy.runner, path=policy.path) is None:
        raise PreflightBlockedError(
            action=f"Install the {policy.runner} CLI on the worker PATH",
            reason=f"{policy.runner} was not found on the child PATH",
        )


def _check_model(policy: PreflightPolicy) -> None:
    if policy.model is not None and not _MODEL.fullmatch(policy.model):
        raise PreflightBlockedError(
            action="Set a valid CODEX_MODEL",
            reason=f"invalid model identifier: {policy.model!r}",
        )


def _check_checkout(policy: PreflightPolicy) -> None:
    if not policy.cwd.is_dir():
        raise PreflightBlockedError(
            action="Repair the checkout or sandbox",
            reason=f"the dispatch checkout does not exist: {policy.cwd}",
        )


def _check_provider(
    policy: PreflightPolicy,
    probe: Callable[[list[str], PreflightPolicy], bool],
) -> None:
    for args, action, reason in _PROBES.get(policy.agent, ()):
        if not probe(list(args), policy):
            raise PreflightBlockedError(action=action, reason=reason)


def _baseline_required(policy: PreflightPolicy) -> bool:
    """Require the baseline script only when this checkout actually provides it."""
    if (policy.cwd / "scripts" / "baseline.sh").is_file():
        return True
    git = shutil.which("git", path=policy.path)
    if git is None:
        raise PreflightBlockedError(
            action="Install Git on the worker PATH",
            reason="cannot inspect the repository verification files without Git",
        )
    try:
        result = subprocess.run(
            [git, "ls-files", "--error-unmatch", "--", "scripts/baseline.sh"],
            cwd=policy.cwd,
            env=dict(policy.env),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode not in (0, 1):
        raise PreflightBlockedError(
            action="Repair the checkout verification metadata",
            reason="cannot inspect baseline-script tracking in this repository",
        )
    return result.returncode == 0


def _check_verification_tools(policy: PreflightPolicy) -> tuple[str, ...]:
    for binary in VERIFICATION_BINARIES:
        if shutil.which(binary, path=policy.path) is None:
            label = _VERIFICATION_TOOL_LABELS.get(binary, binary)
            raise PreflightBlockedError(
                action=f"Install {label} on the worker PATH",
                reason=f"{binary} was not found on the child PATH",
            )
    baseline = policy.cwd / "scripts" / "baseline.sh"
    required = _baseline_required(policy)
    if required and not baseline.is_file():
        raise PreflightBlockedError(
            action="Restore scripts/baseline.sh in the checkout",
            reason="scripts/baseline.sh is missing from the checkout",
        )
    return tuple(
        command
        for command in VERIFICATION_COMMANDS
        if command != "scripts/baseline.sh" or required
    )


def preflight(
    agent: str | None = None,
    *,
    policy: PreflightPolicy | None = None,
    probe: Callable[[list[str], PreflightPolicy], bool] = run_probe,
) -> None:
    """Block with a secret-free diagnostic when a known prerequisite is missing."""
    resolved = policy if policy is not None else resolve_policy(agent)
    _check_runner(resolved)
    _check_model(resolved)
    _check_checkout(resolved)
    _check_provider(resolved, probe)
    commands = _check_verification_tools(resolved)
    unapproved = unapproved_verification_commands(resolved.agent, commands)
    if unapproved:
        raise PreflightBlockedError(
            action=(
                "Approve the documented verification commands in "
                "CLAUDE_CODE_ALLOWED_TOOLS"
            ),
            reason="not approved: " + ", ".join(unapproved),
        )
