"""Detached restart helper.

Used after operations that change the running process's own config on disk
(e.g. `/model ... set ...`) and need the service to pick it up: the handler
replies first, then `schedule_restart()` fires the restart after a short
delay, since a process cannot survive its own `systemctl restart`.
"""

import logging
import os
import shlex
import subprocess


logger = logging.getLogger(__name__)

SERVICE_NAME = os.environ.get("AGENT_SERVICE_NAME", "ai-agent")
RESTART_DELAY_SECONDS = int(os.environ.get("AGENT_RESTART_DELAY_SECONDS", "3"))


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
