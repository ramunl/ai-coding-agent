import unittest
from unittest.mock import patch

from ai_agent import version
from ai_agent.version import get_runtime_version


class VersionTests(unittest.TestCase):
    def test_delegates_to_shared_helper_with_agent_name(self) -> None:
        # version.py must call the shared helper with this bot's name and root,
        # so all agents format /version identically.
        with patch.object(
            version, "_shared_runtime_version", return_value="stub"
        ) as shared:
            result = get_runtime_version()

        self.assertEqual(result, "stub")
        shared.assert_called_once()
        agent_name = shared.call_args.args[0]
        self.assertEqual(agent_name, "ai-coding-agent")

    def test_real_output_labels_itself(self) -> None:
        # End-to-end (real git in this repo): still labelled ai-coding-agent.
        result = get_runtime_version()
        self.assertTrue(result.startswith("ai-coding-agent v"))
        self.assertNotIn("ai_agent v", result)
        self.assertIn("branch:", result)
        self.assertIn("commit:", result)


if __name__ == "__main__":
    unittest.main()
