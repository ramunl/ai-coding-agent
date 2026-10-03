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
import uuid

logger = logging.getLogger(__name__)

SERVICE_NAME = os.environ.get("AGENT_SERVICE_NAME", "ai-coding-agent")
RESTART_DELAY_SECONDS = int(os.environ.get("AGENT_RESTART_DELAY_SECONDS", "3"))


def schedule_restart() -> str:
    """Fire a detached restart so the reply is sent before the process dies.

    systemd-run creates a transient unit outside this service's cgroup, so
    the restart survives the bot's own death. Scheduler failures are reported
    instead of promising a restart from within the bot's cgroup.
    """
    command = (
        f"sleep {RESTART_DELAY_SECONDS} && systemctl restart "
        f"{shlex.quote(SERVICE_NAME)}"
    )
    try:
        subprocess.run(
            [
                "systemd-run",
                "--collect",
                f"--unit={SERVICE_NAME}-selfupdate-{uuid.uuid4().hex}",
                "/bin/sh",
                "-c",
                command,
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
        return f"Restart scheduled in {RESTART_DELAY_SECONDS}s."
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ) as error:
        logger.error("Could not schedule service restart (%s)", type(error).__name__)
        raise RuntimeError(
            "Model saved, but restart could not be scheduled; "
            f"restart {SERVICE_NAME}.service on the server."
        ) from error
