import unittest
from unittest.mock import patch

from ai_agent.ci import evaluate_ci, summarize_failed_jobs


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


if __name__ == "__main__":
    unittest.main()
