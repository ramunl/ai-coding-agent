"""Implementation limits are independent of planning and accept unlimited runs."""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest


@pytest.mark.parametrize("setting, expected", [(None, 37), ("0", None), ("7200", 7200)])
def test_implementation_timeout_configuration(setting, expected):
    env = {**os.environ, "CODEX_TIMEOUT_SECONDS": "37"}
    env.pop("IMPLEMENTATION_TIMEOUT_SECONDS", None)
    if setting is not None:
        env["IMPLEMENTATION_TIMEOUT_SECONDS"] = setting
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from ai_agent.config import IMPLEMENTATION_TIMEOUT_SECONDS, CODEX_TIMEOUT_SECONDS; print(json.dumps([IMPLEMENTATION_TIMEOUT_SECONDS, CODEX_TIMEOUT_SECONDS]))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [expected, 37]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_unlimited_timeout_reaches_provider_without_affecting_planning(agent):
    from ai_agent import workflow
    from ai_agent.shell import CommandResult

    with (
        patch.object(workflow, "IMPLEMENTATION_TIMEOUT_SECONDS", None),
        patch.object(workflow, "diagnostic_context", return_value=""),
        patch.object(
            workflow, "run_with_images", return_value=CommandResult([], 0, "done")
        ) as run,
        patch.object(workflow, "record_usage"),
    ):
        workflow.run_implementation_agent("fix", agent)
    assert run.call_args.kwargs["timeout"] is None
