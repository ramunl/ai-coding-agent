"""Diagnostics are acquired by the orchestrator, before restricted AI work."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from ai_agent import ops_context


def test_known_repo_gets_readonly_evidence_and_untrusted_data_boundary():
    result = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"ok": True, "evidence": {"packages": "HTTP 402"}}),
    )
    with (
        patch.object(
            ops_context,
            "active_project",
            return_value=SimpleNamespace(name="ai-ops-agent"),
        ),
        patch.object(ops_context.subprocess, "run", return_value=result) as run,
    ):
        prompt = ops_context.diagnostic_context()
    assert "HTTP 402" in prompt
    assert "untrusted data" in prompt
    assert run.call_args.args[0] == ["/usr/local/sbin/ai-diagnostics", "ai-ops-agent"]
    assert run.call_args.kwargs["timeout"] == 25


def test_other_projects_never_read_server_evidence():
    with (
        patch.object(
            ops_context, "active_project", return_value=SimpleNamespace(name="other")
        ),
        patch.object(ops_context.subprocess, "run") as run,
    ):
        assert ops_context.diagnostic_context() == ""
    run.assert_not_called()


def test_missing_helper_reports_unavailable_instead_of_failing_planning():
    with (
        patch.object(
            ops_context,
            "active_project",
            return_value=SimpleNamespace(name="ai-dashboard"),
        ),
        patch.object(ops_context.subprocess, "run", side_effect=FileNotFoundError),
    ):
        assert "diagnostics unavailable" in ops_context.diagnostic_context()
