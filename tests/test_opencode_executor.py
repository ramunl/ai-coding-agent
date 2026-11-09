"""OpenCode executor: runner reduction and workflow integration.

The provider is owner-gated (``enabled=False``), so the worker can never route
an implementation through OpenCode on its own. These tests replay the pinned
JSON-event fixture and the other contract scenarios (false success, permission
error, timeout/cancel, truncation) through ``opencode_runner.run_stream`` with
a fake subprocess runner, then drive the workflow path with the provider
enabled to prove success records usage/detail and each failure outcome surfaces
a bounded error while the versioned usage-detail store keeps loading historical
format-1 records alongside the new ``opencode`` runner key.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_agent import usage_details
from ai_agent.ai_tools import OpenCodeTool, register_tool
from ai_agent.config import OPENCODE_MAX_OUTPUT_CHARS, OPENCODE_MODEL
from ai_agent.implementation_stop import ImplementationStoppedError
from ai_agent.opencode_runner import OpenCodeRun, run_stream
from ai_agent.providers import opencode
from ai_agent.token_usage import utc_day
from ai_agent.usage_details import UsageDetail, detail_section, record_detail

FIXTURE = Path(__file__).parent / "fixtures" / "opencode" / "success.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _stream_result(events: list, exit_code: int = 0, truncated: bool = False):
    """A fake grouped_run result carrying the given events as a JSON wrapper."""
    return SimpleNamespace(
        stdout=json.dumps({"events": events}),
        stderr="",
        returncode=exit_code,
        truncated=truncated,
    )


class _FakeRun:
    """A fake subprocess runner: returns ``result`` or raises ``error``."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.command = None
        self.max_output_chars = None
        self.kwargs = None

    def __call__(self, command, *, max_output_chars=None, **kwargs):
        self.command = list(command)
        self.max_output_chars = max_output_chars
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.result


class RunnerTests(unittest.TestCase):
    """``run_stream`` reduces the JSON stream to the bounded outcome contract."""

    def test_replays_the_pinned_success_fixture(self):
        fixture = _fixture()
        fake = _FakeRun(
            SimpleNamespace(stdout=json.dumps(fixture), stderr="", returncode=0)
        )
        run = run_stream(["opencode", "run", "--format", "json"], run=fake)

        self.assertEqual(run.exit_code, 0)
        self.assertFalse(run.cancelled)
        self.assertIsNone(run.error)
        self.assertIsNone(run.error_kind)
        self.assertTrue(run.has_result)
        self.assertFalse(run.truncated)
        self.assertEqual(run.output, "Created feature branch and edited two files.")
        self.assertEqual(
            run.usage,
            {
                "input_tokens": 42,
                "output_tokens": 18,
                "reasoning_tokens": 0,
                "total_tokens": 60,
            },
        )
        # The command is passed through verbatim and the output bound requested.
        self.assertEqual(fake.command, ["opencode", "run", "--format", "json"])
        self.assertEqual(fake.max_output_chars, OPENCODE_MAX_OUTPUT_CHARS)

    def test_zero_exit_without_result_is_a_false_success(self):
        # A zero exit alone must never count as done: workflow rejects this.
        events = [
            {"type": "message", "properties": {"text": "analyzing"}},
            {
                "type": "usage",
                "properties": {"input_tokens": 1, "output_tokens": 1},
            },
        ]
        run = run_stream(["opencode", "run"], run=_FakeRun(_stream_result(events)))

        self.assertEqual(run.exit_code, 0)
        self.assertFalse(run.has_result)
        self.assertIsNone(run.error)
        self.assertEqual(run.output, "")

    def test_permission_error_event_reports_error_and_kind(self):
        events = [
            {
                "type": "error",
                "properties": {"code": "auth", "message": "permission denied"},
            }
        ]
        run = run_stream(
            ["opencode", "run"], run=_FakeRun(_stream_result(events, exit_code=1))
        )

        self.assertEqual(run.exit_code, 1)
        self.assertEqual(run.error, "permission denied")
        self.assertEqual(run.error_kind, "auth")
        self.assertFalse(run.has_result)

    def test_nonzero_exit_without_error_is_an_execution_error(self):
        run = run_stream(
            ["opencode", "run"], run=_FakeRun(_stream_result([], exit_code=2))
        )

        self.assertEqual(run.exit_code, 2)
        self.assertEqual(run.error, "opencode exited with status 2")
        self.assertEqual(run.error_kind, "execution")

    def test_cancelled_event_marks_cancelled(self):
        events = [{"type": "cancelled", "properties": {"reason": "user"}}]
        run = run_stream(
            ["opencode", "run"], run=_FakeRun(_stream_result(events, exit_code=130))
        )

        self.assertTrue(run.cancelled)
        self.assertIsNone(run.error)
        self.assertEqual(run.exit_code, 130)

    def test_timeout_is_normalized_to_a_bounded_runtime_error(self):
        fake = _FakeRun(error=subprocess.TimeoutExpired(["opencode", "run"], 30))
        with self.assertRaises(RuntimeError) as raised:
            run_stream(["opencode", "run"], run=fake)
        self.assertEqual(str(raised.exception), "OpenCode run timed out after 30s.")

    def test_truncation_is_marked_on_the_prose(self):
        fixture = _fixture()
        result = SimpleNamespace(
            stdout=json.dumps(fixture), stderr="", returncode=0, truncated=True
        )
        run = run_stream(["opencode", "run"], run=_FakeRun(result))

        self.assertTrue(run.truncated)
        self.assertTrue(
            run.output.endswith("[opencode output truncated; captured tail only]")
        )


class OpenCodeWorkflowTests(unittest.TestCase):
    """The workflow path, with the owner gate flipped on for the test only."""

    def setUp(self):
        patcher = patch("ai_agent.workflow.preflight")
        patcher.start()
        self.addCleanup(patcher.stop)
        self._enabled = patch.object(opencode, "enabled", True)
        self._enabled.start()
        self.addCleanup(self._enabled.stop)
        from ai_agent import ai_tools

        self._original = ai_tools.get_tool("opencode")
        register_tool("opencode", OpenCodeTool())
        self.addCleanup(lambda: register_tool("opencode", self._original))

    def _run(self, stream_result):
        from ai_agent import workflow

        with (
            patch.object(workflow, "run_stream", return_value=stream_result),
            patch.object(workflow, "record_usage") as usage,
            patch.object(workflow, "record_detail") as detail,
        ):
            result = workflow.run_implementation_agent("fix", "opencode")
        return result, usage, detail

    def _success_run(self, **overrides):
        values = dict(
            output="Created feature branch and edited two files.",
            usage={
                "input_tokens": 42,
                "output_tokens": 18,
                "reasoning_tokens": 0,
                "total_tokens": 60,
            },
            error=None,
            error_kind=None,
            cancelled=False,
            exit_code=0,
            has_result=True,
            truncated=False,
        )
        values.update(overrides)
        return OpenCodeRun(**values)

    def test_success_records_usage_and_detail(self):
        result, usage, detail = self._run(self._success_run())

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output, "Created feature branch and edited two files.")
        record = usage.call_args.args[0]
        self.assertEqual(
            (record.role, record.tool, record.tokens),
            ("implementer", "opencode", 60),
        )
        detail = detail.call_args.args[0]
        self.assertEqual(
            (detail.role, detail.runner, detail.provider, detail.model),
            ("implementer", "opencode", "deepseek", OPENCODE_MODEL),
        )
        self.assertEqual(detail.input_tokens, 42)
        self.assertEqual(detail.output_tokens, 18)
        self.assertEqual(detail.total_tokens, 60)

    def test_false_success_raises_a_bounded_error(self):
        with self.assertRaises(RuntimeError) as raised:
            self._run(self._success_run(has_result=False, output=""))
        self.assertEqual(
            str(raised.exception), "OpenCode exited without reporting a result."
        )

    def test_permission_error_raises_bounded_prose(self):
        with self.assertRaises(RuntimeError) as raised:
            self._run(
                self._success_run(
                    error="permission denied for run",
                    error_kind="auth",
                    has_result=False,
                    output="",
                )
            )
        self.assertEqual(str(raised.exception), "permission denied for run")

    def test_cancelled_run_raises(self):
        run = self._success_run(cancelled=True, error=None, has_result=False, output="")
        with self.assertRaises(RuntimeError) as raised:
            self._run(run)
        self.assertEqual(
            str(raised.exception),
            "OpenCode run was cancelled before it reported a result.",
        )

    def test_owner_stop_preserves_an_unknown_detail(self):
        from ai_agent import workflow

        with (
            patch.object(workflow, "record_usage"),
            patch.object(workflow, "record_detail") as detail,
            patch.object(
                workflow,
                "run_stream",
                side_effect=ImplementationStoppedError("stopped"),
            ),
        ):
            with self.assertRaises(ImplementationStoppedError):
                workflow.run_implementation_agent("fix", "opencode")

        self.assertTrue(detail.called)
        record = detail.call_args.args[0]
        self.assertEqual(record.role, "implementer")
        self.assertEqual((record.runner, record.provider), ("opencode", "deepseek"))
        self.assertIsNone(record.total_tokens)

    def test_implementation_command_uses_json_output_and_model(self):
        from ai_agent import workflow

        command = workflow.implementation_command("do work", "opencode")
        self.assertEqual(
            command,
            [
                "opencode",
                "run",
                "--format",
                "json",
                "--model",
                OPENCODE_MODEL,
                "do work",
            ],
        )


class HistoricalStoreTests(unittest.TestCase):
    """A historical format-1 usage-detail store still loads next to opencode."""

    def test_legacy_store_loads_alongside_the_new_opencode_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage-details.json"
            legacy_record = {
                "role": "implementer",
                "runner": "codex",
                "provider": "codex",
                "model": None,
                "day": utc_day(),
                "input_tokens": None,
                "output_tokens": None,
                "cache_read_tokens": None,
                "cache_creation_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": 1234,
                "cost": None,
                "duration_seconds": 30,
                "output_chars": 10,
                "attempt_id": "attempt-legacy",
                "call_id": "call-legacy",
            }
            path.write_text(json.dumps({"format": 1, "records": [legacy_record]}))
            opencode_detail = UsageDetail(
                role="implementer",
                runner="opencode",
                provider="deepseek",
                model=OPENCODE_MODEL,
                input_tokens=42,
                output_tokens=18,
                total_tokens=60,
                attempt_id="attempt-open",
                call_id="call-open",
            )
            self.assertTrue(record_detail(opencode_detail, path))

            section = detail_section(path)
            self.assertIsNotNone(section)
            runners = {record["runner"] for record in section["records"]}
            self.assertTrue({"codex", "opencode"} <= runners)
            # The upgraded legacy record gained the estimate fields unchanged.
            upgraded = next(
                record for record in section["records"] if record["runner"] == "codex"
            )
            self.assertEqual(upgraded["total_tokens"], 1234)
            self.assertEqual(usage_details.FORMAT, 2)


if __name__ == "__main__":
    unittest.main()
