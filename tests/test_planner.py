import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = {
            "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
            "YOUR_CHAT_ID": os.environ.get("YOUR_CHAT_ID"),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
        }
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-secret"

        self.previous_anthropic = sys.modules.get("anthropic")
        sys.modules.pop("anthropic", None)
        sys.modules.pop("ai_agent.planner", None)

        anthropic_module = types.ModuleType("anthropic")

        class FakeAnthropic:
            def __init__(self, api_key: str) -> None:
                self.messages = FakeMessages()

        class FakeMessages:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return types.SimpleNamespace(content=[types.SimpleNamespace(text="planned")])

        self.fake_anthropic_class = FakeAnthropic
        anthropic_module.Anthropic = FakeAnthropic
        sys.modules["anthropic"] = anthropic_module

    def tearDown(self) -> None:
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        sys.modules.pop("ai_agent.planner", None)
        sys.modules.pop("anthropic", None)
        if self.previous_anthropic is not None:
            sys.modules["anthropic"] = self.previous_anthropic

    def test_plan_feature_uses_enriched_feature_description(self) -> None:
        planner = importlib.import_module("ai_agent.planner")

        with patch("ai_agent.planner.kotlin_file_sample", return_value="App.kt"):
            with patch("ai_agent.planner.enrich_feature_description", return_value="enriched request"):
                result = planner.plan_feature("original request")

        prompt = planner.client.messages.kwargs["messages"][0]["content"]
        self.assertEqual(result, "planned")
        self.assertIn("Feature request: enriched request", prompt)

    def test_build_bugfix_prompt_uses_enriched_bug_description(self) -> None:
        planner = importlib.import_module("ai_agent.planner")

        with patch("ai_agent.planner.enrich_feature_description", return_value="enriched bug"):
            prompt = planner.build_bugfix_prompt("original bug")

        self.assertIn("Fix this bug", prompt)
        self.assertIn("Bug report:\nenriched bug", prompt)
        self.assertIn("Do not make unrelated refactors", prompt)

    def test_assess_bugfix_report_uses_enriched_bug_description(self) -> None:
        planner = importlib.import_module("ai_agent.planner")

        with patch("ai_agent.planner.enrich_feature_description", return_value="enriched bug"):
            result = planner.assess_bugfix_report("original bug")

        prompt = planner.client.messages.kwargs["messages"][0]["content"]
        self.assertEqual(result, "planned")
        self.assertIn("Bug report:\nenriched bug", prompt)

    def test_bugfix_questions_parses_ready_and_questions(self) -> None:
        planner = importlib.import_module("ai_agent.planner")

        self.assertIsNone(planner.bugfix_questions("READY"))
        self.assertEqual(planner.bugfix_questions("QUESTIONS:\n1. Steps?"), "1. Steps?")


if __name__ == "__main__":
    unittest.main()
