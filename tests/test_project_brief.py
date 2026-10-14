import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_agent import project_brief


class ProjectBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8")

    def test_agents_md_comes_first_then_claude_md_then_readme(self) -> None:
        self.assertEqual(project_brief.project_brief(self.repo), ("", ""))
        self.write("README.md", "# App\nA cast app.")
        self.assertEqual(project_brief.project_brief(self.repo)[0], "README.md")
        self.write("CLAUDE.md", "Kotlin, Gradle; run ./gradlew test.")
        self.assertEqual(project_brief.project_brief(self.repo)[0], "CLAUDE.md")
        self.write("AGENTS.md", "Python bot; run pytest.")
        self.assertEqual(
            project_brief.project_brief(self.repo),
            ("AGENTS.md", "Python bot; run pytest."),
        )

    def test_a_claude_md_that_only_points_to_agents_md_is_skipped(self) -> None:
        self.write("CLAUDE.md", "@AGENTS.md\n")
        self.write("README.md", "Readme text")
        self.assertEqual(project_brief.project_brief(self.repo)[0], "README.md")

    def test_the_brief_is_bounded_and_redacted(self) -> None:
        self.write("AGENTS.md", "token secret-123\n" + "x" * 20000)
        with patch.object(
            project_brief,
            "redact_sensitive",
            lambda text: text.replace("secret-123", "[redacted]"),
        ):
            name, text = project_brief.project_brief(self.repo)
        self.assertNotIn("secret-123", text)
        self.assertLess(len(text), project_brief.BRIEF_LIMIT + 100)
        self.assertTrue(text.endswith("read the file for the rest]"))
        self.write("README.md", "y" * 20000)
        (self.repo / "AGENTS.md").unlink()
        self.assertLess(
            len(project_brief.project_brief(self.repo)[1]),
            project_brief.README_LIMIT + 100,
        )

    def test_block_names_its_source_and_defers_to_the_rules(self) -> None:
        project = SimpleNamespace(repo_path=self.repo)
        with patch.object(project_brief, "active_project", return_value=project):
            self.assertEqual(project_brief.project_brief_block(), "")
            self.write("README.md", "A cast app.")
            block = project_brief.project_brief_block()
            self.assertIn("start of README.md; the repository has no AGENTS.md", block)
            self.write("AGENTS.md", "Python bot.")
            block = project_brief.project_brief_block()
        self.assertIn("PROJECT BRIEF (the repository's AGENTS.md)", block)
        self.assertIn("coding rules below take priority", block)
        self.assertIn("Python bot.", block)

    def test_no_active_project_means_no_brief(self) -> None:
        with patch.object(
            project_brief, "active_project", side_effect=RuntimeError("none")
        ):
            self.assertEqual(project_brief.project_brief_block(), "")
