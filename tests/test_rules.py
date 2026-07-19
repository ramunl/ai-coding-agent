import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _init_rules_repo(root: Path) -> None:
    """Create a small rules repo with global and project rules."""
    (root / "global").mkdir(parents=True)
    (root / "projects" / "channel-cast").mkdir(parents=True)
    (root / "global" / "kotlin.md").write_text(
        "# Kotlin\n- Avoid return operators\n", encoding="utf-8"
    )
    (root / "projects" / "channel-cast" / "architecture.md").write_text(
        "# Architecture\n- Repository is the single source of truth\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


class RulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-secret"

        self.tmp = tempfile.mkdtemp()
        self.rules_path = Path(self.tmp) / "ai-rules"
        _init_rules_repo(self.rules_path)

        os.environ["RULES_ENABLED"] = "true"
        os.environ["RULES_REPO_PATH"] = str(self.rules_path)
        os.environ["RULES_PROJECT_NAME"] = "channel-cast"

        for module in ("ai_agent.config", "ai_agent.projects", "ai_agent.shell", "ai_agent.rules"):
            sys.modules.pop(module, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)
        for module in ("ai_agent.config", "ai_agent.projects", "ai_agent.shell", "ai_agent.rules"):
            sys.modules.pop(module, None)

    def test_load_rules_includes_global_and_project(self) -> None:
        rules = importlib.import_module("ai_agent.rules")
        text = rules.load_rules_text()
        self.assertIn("Avoid return operators", text)
        self.assertIn("single source of truth", text)

    def test_prompt_block_has_mandatory_header(self) -> None:
        rules = importlib.import_module("ai_agent.rules")
        block = rules.rules_prompt_block()
        self.assertIn("MANDATORY CODING RULES", block)
        self.assertIn("Avoid return operators", block)

    def test_disabled_returns_empty(self) -> None:
        os.environ["RULES_ENABLED"] = "false"
        for module in ("ai_agent.config", "ai_agent.projects", "ai_agent.shell", "ai_agent.rules"):
            sys.modules.pop(module, None)
        rules = importlib.import_module("ai_agent.rules")
        self.assertEqual(rules.load_rules_text(), "")
        self.assertEqual(rules.rules_prompt_block(), "")

    def test_missing_repo_returns_empty_not_error(self) -> None:
        os.environ["RULES_REPO_PATH"] = str(Path(self.tmp) / "does-not-exist")
        os.environ["RULES_REPO_URL"] = "file:///nonexistent/repo.git"
        for module in ("ai_agent.config", "ai_agent.projects", "ai_agent.shell", "ai_agent.rules"):
            sys.modules.pop(module, None)
        rules = importlib.import_module("ai_agent.rules")
        self.assertEqual(rules.rules_prompt_block(), "")

    def test_project_rules_absent_still_returns_global(self) -> None:
        os.environ["RULES_PROJECT_NAME"] = "nonexistent-project"
        for module in ("ai_agent.config", "ai_agent.projects", "ai_agent.shell", "ai_agent.rules"):
            sys.modules.pop(module, None)
        rules = importlib.import_module("ai_agent.rules")
        text = rules.load_rules_text()
        self.assertIn("Avoid return operators", text)
        self.assertNotIn("single source of truth", text)


if __name__ == "__main__":
    unittest.main()
