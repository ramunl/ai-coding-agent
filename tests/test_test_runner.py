import unittest
from unittest.mock import patch

from ai_agent.test_runner import run_unit_tests


class TestRunnerTests(unittest.TestCase):
    @patch("ai_agent.test_runner.subprocess.run")
    def test_run_unit_tests_uses_dummy_environment(self, mock_run) -> None:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""

        result = run_unit_tests()

        self.assertIn("Tests passed (0)", result)
        call_kwargs = mock_run.call_args.kwargs
        self.assertEqual(call_kwargs["env"]["TELEGRAM_BOT_TOKEN"], "test-telegram-token")
        self.assertEqual(call_kwargs["env"]["YOUR_CHAT_ID"], "1")
        self.assertEqual(call_kwargs["env"]["ANTHROPIC_API_KEY"], "test-anthropic-key")
        self.assertEqual(call_kwargs["env"]["GITHUB_TOKEN"], "test-github-token")


if __name__ == "__main__":
    unittest.main()
