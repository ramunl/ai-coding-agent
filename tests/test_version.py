import unittest
from unittest.mock import patch

from ai_agent.version import get_runtime_version


class VersionTests(unittest.TestCase):
    @patch("ai_agent.version.get_git_commit", return_value="abc1234")
    @patch("ai_agent.version.get_git_branch", return_value="main")
    @patch("ai_agent.version.get_version", return_value="0.3.0")
    def test_get_runtime_version_labels_itself_ai_coding_agent(self, *_mocks) -> None:
        result = get_runtime_version()

        self.assertTrue(result.startswith("ai-coding-agent v0.3.0\n"))
        self.assertNotIn("ai_agent v", result)


if __name__ == "__main__":
    unittest.main()
