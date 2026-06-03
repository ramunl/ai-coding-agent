import unittest
from unittest.mock import patch

from ai_agent.ci import CiResult, build_failure_context, evaluate_ci, summarize_failed_jobs


class CiTests(unittest.TestCase):
    @patch("ai_agent.ci.list_workflow_runs", return_value=[])
    def test_evaluate_ci_waits_when_no_runs_exist(self, _mock_runs) -> None:
        result = evaluate_ci("abc123")

        self.assertEqual(result.state, "waiting")
        self.assertEqual(result.summary, "CI has not started yet")

    @patch(
        "ai_agent.ci.list_workflow_runs",
        return_value=[{"name": "Build", "status": "in_progress", "html_url": "https://example.test/run"}],
    )
    def test_evaluate_ci_reports_running_workflows(self, _mock_runs) -> None:
        result = evaluate_ci("abc123")

        self.assertEqual(result.state, "running")
        self.assertEqual(result.summary, "CI running: Build")
        self.assertEqual(result.url, "https://example.test/run")

    @patch(
        "ai_agent.ci.list_workflow_runs",
        return_value=[{"name": "Build", "status": "completed", "conclusion": "success", "html_url": "https://example.test/run"}],
    )
    def test_evaluate_ci_reports_successful_workflows(self, _mock_runs) -> None:
        result = evaluate_ci("abc123")

        self.assertEqual(result.state, "passed")
        self.assertEqual(result.summary, "CI passed: Build")

    @patch(
        "ai_agent.ci.list_workflow_runs",
        return_value=[
            {
                "id": 2,
                "workflow_id": 10,
                "name": "Build",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-06-03T01:10:00Z",
                "html_url": "https://example.test/new",
            },
            {
                "id": 1,
                "workflow_id": 10,
                "name": "Build",
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-06-03T01:00:00Z",
                "html_url": "https://example.test/old",
            },
        ],
    )
    def test_evaluate_ci_ignores_older_failed_run_for_same_workflow(self, _mock_runs) -> None:
        result = evaluate_ci("abc123")

        self.assertEqual(result.state, "passed")
        self.assertEqual(result.summary, "CI passed: Build")
        self.assertEqual(result.url, "https://example.test/new")

    @patch(
        "ai_agent.ci.list_workflow_jobs",
        return_value=[
            {
                "name": "compile",
                "conclusion": "failure",
                "html_url": "https://example.test/job",
                "steps": [{"name": "Kotlin compile", "conclusion": "failure"}],
            }
        ],
    )
    def test_summarize_failed_jobs_includes_failed_steps(self, _mock_jobs) -> None:
        summary = summarize_failed_jobs(
            [{"id": 7, "name": "Build", "conclusion": "failure", "html_url": "https://example.test/run"}]
        )

        self.assertIn("compile (Kotlin compile)", summary)
        self.assertIn("https://example.test/job", summary)

    @patch(
        "ai_agent.ci.github_request",
        return_value=[
            {"body": "unrelated comment"},
            {"body": "**Build failed**\n\n```\ne: compile failed\n```"},
        ],
    )
    def test_build_failure_context_includes_latest_build_failure_comment(self, _mock_request) -> None:
        context = build_failure_context(12, CiResult("failed", "CI failed: build", "https://example.test/run"))

        self.assertIn("CI failed: build", context)
        self.assertIn("https://example.test/run", context)
        self.assertIn("e: compile failed", context)


if __name__ == "__main__":
    unittest.main()
