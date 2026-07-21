"""Tests-gated self-update for the agent.

Flow for /pull:
  1. git fetch origin main; if nothing new, stop.
  2. Record the current commit as the rollback point.
  3. git reset --hard origin/main  (working copy now has the NEW code)
  4. Run the unit test suite against the new code.
  5. Tests fail  -> git reset --hard back to the rollback point. The running
                    process never loaded the bad code, so nothing restarts.
  6. Tests pass  -> caller may trigger a detached systemd restart.

The restart is detached (systemd-run) because a process cannot survive its
own `systemctl restart`: the handler would be killed before replying. The
bot therefore replies FIRST, then the restart fires after a short delay.
"""

import logging
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)

AGENT_DIR = Path(__file__).resolve().parent.parent
SERVICE_NAME = os.environ.get("AGENT_SERVICE_NAME", "ai-agent")
RESTART_DELAY_SECONDS = int(os.environ.get("AGENT_RESTART_DELAY_SECONDS", "3"))
UPDATE_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    restart_pending: bool
    message: str


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        check=False,
        cwd=AGENT_DIR,
        text=True,
        timeout=UPDATE_TIMEOUT_SECONDS,
    )


def _short(commit: str) -> str:
    return commit[:7]


def _run_tests_on_new_code() -> tuple[bool, str]:
    """Run the suite in a fresh process so it imports the NEW files on disk.

    The running bot keeps its old modules in memory; a subprocess is the only
    way to exercise what was just fetched.
    """
    env = os.environ.copy()
    env.update(
        {
            "TELEGRAM_BOT_TOKEN": "test-telegram-token",
            "YOUR_CHAT_ID": "1",
            "ANTHROPIC_API_KEY": "test-anthropic-key",
            "GITHUB_TOKEN": "test-github-token",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover"],
        capture_output=True,
        check=False,
        cwd=AGENT_DIR,
        env=env,
        text=True,
        timeout=UPDATE_TIMEOUT_SECONDS,
    )
    passed = result.returncode == 0
    tail = (result.stdout + result.stderr).strip().splitlines()[-12:]
    return passed, "\n".join(tail)


def check_and_apply_update() -> UpdateResult:
    fetch = _git(["fetch", "origin", "main"])
    fetch_ok = fetch.returncode == 0
    if not fetch_ok:
        return UpdateResult(False, False, f"git fetch failed:\n{fetch.stderr.strip()}")

    local = _git(["rev-parse", "HEAD"]).stdout.strip()
    remote = _git(["rev-parse", "origin/main"]).stdout.strip()
    is_current = local == remote
    if is_current:
        return UpdateResult(True, False, f"Already up to date at {_short(local)}.")

    dirty = _git(["status", "--porcelain"]).stdout.strip()
    is_clean = not dirty
    if not is_clean:
        return UpdateResult(
            False,
            False,
            "Working copy has local changes; refusing to update over them:\n"
            f"{dirty}\n\nCommit or discard them on the server first.",
        )

    incoming = _git(["log", "--oneline", f"{local}..{remote}"]).stdout.strip()
    logger.info("Updating %s -> %s", _short(local), _short(remote))

    reset = _git(["reset", "--hard", remote])
    reset_ok = reset.returncode == 0
    if not reset_ok:
        return UpdateResult(False, False, f"git reset failed:\n{reset.stderr.strip()}")

    tests_passed, test_tail = _run_tests_on_new_code()
    if not tests_passed:
        rollback = _git(["reset", "--hard", local])
        rollback_ok = rollback.returncode == 0
        rollback_note = (
            f"Rolled back to {_short(local)}; the running bot was never affected."
            if rollback_ok
            else f"ROLLBACK FAILED — repair manually: git reset --hard {_short(local)}"
        )
        logger.warning("Update to %s rejected by tests", _short(remote))
        return UpdateResult(
            False,
            False,
            f"Update to {_short(remote)} REJECTED — tests failed on the new code.\n\n"
            f"{test_tail}\n\n{rollback_note}",
        )

    return UpdateResult(
        True,
        True,
        f"Updated {_short(local)} -> {_short(remote)}, tests passed.\n\n"
        f"Incoming commits:\n{incoming}",
    )


def schedule_restart() -> str:
    """Fire a detached restart so the reply is sent before the process dies.

    systemd-run creates a transient unit outside this service's cgroup, so
    the restart survives the bot's own death. Falls back to a double-forked
    shell if systemd-run is unavailable.
    """
    command = f"sleep {RESTART_DELAY_SECONDS} && systemctl restart {shlex.quote(SERVICE_NAME)}"
    try:
        subprocess.run(
            ["systemd-run", "--collect", f"--unit={SERVICE_NAME}-selfupdate", "/bin/sh", "-c", command],
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
        return f"Restart scheduled in {RESTART_DELAY_SECONDS}s."
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as error:
        logger.warning("systemd-run unavailable (%s); using detached shell", error)
        subprocess.Popen(
            ["/bin/sh", "-c", command],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Restart scheduled in {RESTART_DELAY_SECONDS}s (detached shell)."
