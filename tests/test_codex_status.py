import unittest
from unittest.mock import patch

from ai_agent.shell import CommandResult


class CodexStatusTests(unittest.TestCase):
    @patch(
        "ai_agent.codex_status.run",
        side_effect=[
            CommandResult(["codex", "--version"], 0, "codex-cli 0.130.0\n"),
            CommandResult(["codex", "login", "status"], 0, "Logged in using ChatGPT\n"),
        ],
    )
    def test_get_codex_status_reports_cli_and_login(self, _mock_run) -> None:
        from ai_agent.codex_status import get_codex_status

        status = get_codex_status()

        self.assertIn("codex-cli 0.130.0", status)
        self.assertIn("Logged in using ChatGPT", status)
        self.assertIn("not exposed by the Codex CLI/API", status)


if __name__ == "__main__":
    unittest.main()
