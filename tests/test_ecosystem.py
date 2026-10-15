import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_agent import ecosystem, plan_state


def _project(name: str, path: Path) -> SimpleNamespace:
    return SimpleNamespace(name=name, repo_path=path, github_repository=f"owner/{name}")


class EcosystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.rules = root / "rules"
        self.rules.mkdir()
        self.projects = []
        for name, brief in (
            ("ai-dashboard", "# ai-dashboard\n\nThe owner's Mini App.\nAll screens.\n"),
            ("ai-pm-agent", "# ai-pm-agent\n\nStores todo lists.\n\n## More\nx"),
            ("channel-cast", ""),
        ):
            path = root / name
            path.mkdir()
            if brief:
                (path / "AGENTS.md").write_text(brief, encoding="utf-8")
            self.projects.append(_project(name, path))
        self.patches = [
            patch.object(ecosystem, "list_projects", return_value=self.projects),
            patch.object(ecosystem, "active_project", return_value=self.projects[1]),
            patch.object(ecosystem, "RULES_REPO_PATH", self.rules),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def test_the_map_lists_every_repository_with_its_first_paragraph(self) -> None:
        block = ecosystem.ecosystem_block()
        self.assertIn("- ai-dashboard: The owner's Mini App. All screens.", block)
        self.assertIn("- ai-pm-agent (active): Stores todo lists.", block)
        self.assertNotIn("More", block)
        self.assertIn("- channel-cast: owner/channel-cast", block)
        self.assertNotIn("Who owns what", block)

    def test_the_hand_written_map_is_added_when_present(self) -> None:
        (self.rules / "ecosystem.md").write_text(
            "- PM window UI: ai-dashboard", encoding="utf-8"
        )
        block = ecosystem.ecosystem_block()
        self.assertIn(
            "Who owns what (ecosystem.md):\n- PM window UI: ai-dashboard", block
        )

    def test_one_repository_needs_no_map(self) -> None:
        with patch.object(ecosystem, "list_projects", return_value=self.projects[:1]):
            self.assertEqual(ecosystem.ecosystem_block(), "")

    def test_summary_lines_are_bounded(self) -> None:
        line = ecosystem.summary_line("# T\n\n" + "word " * 200)
        self.assertLessEqual(len(line), ecosystem.SUMMARY_LIMIT)

    def test_plan_repositories_are_normalized(self) -> None:
        value = [
            {"name": "ai-pm-agent", "changes": "move  op\n in task_cli"},
            {"name": "ai-pm-agent", "changes": "duplicate"},
            {"name": "", "changes": "nameless"},
            "junk",
            {"name": "ai-dashboard", "changes": "drag the list"},
        ]
        self.assertEqual(
            ecosystem.plan_repositories(value),
            [
                {"name": "ai-pm-agent", "changes": "move op in task_cli"},
                {"name": "ai-dashboard", "changes": "drag the list"},
            ],
        )
        self.assertEqual(ecosystem.plan_repositories("nope"), [])

    def test_notes_say_what_approval_runs(self) -> None:
        pm = {"name": "ai-pm-agent", "changes": "x"}
        dash = {"name": "ai-dashboard", "changes": "y"}
        self.assertEqual(ecosystem.repository_note([pm], "ai-pm-agent"), "")
        self.assertIn(
            "Approving runs only ai-pm-agent; ai-dashboard would need their own task",
            ecosystem.repository_note([pm, dash], "ai-pm-agent"),
        )
        self.assertIn(
            "does not change channel-cast",
            ecosystem.repository_note([pm, dash], "channel-cast"),
        )
        self.assertIn(
            "This plan is for ai-dashboard, not channel-cast",
            ecosystem.repository_note([dash], "channel-cast"),
        )


class PlanRepositoriesTests(unittest.TestCase):
    PLAN = json.dumps(
        {
            "branch": "feature/drag-todos",
            "summary": "Drag todo items",
            "files": ["ai-pm-agent: task_cli.py"],
            "steps": ["ai-pm-agent: add move"],
            "risks": [],
            "repositories": [
                {"name": "ai-pm-agent", "changes": "move op"},
                {"name": "ai-dashboard", "changes": "drag UI"},
            ],
        }
    )

    def setUp(self) -> None:
        project = SimpleNamespace(name="ai-pm-agent", github_repository="o/pm")
        self.active = patch.object(plan_state, "active_project", return_value=project)
        self.active.start()

    def tearDown(self) -> None:
        self.active.stop()

    def plan(self) -> plan_state.PlanState:
        return plan_state.new_plan_state("drag todos", self.PLAN)

    def test_the_plan_shows_its_repositories_and_what_approval_runs(self) -> None:
        text = plan_state.render_plan(self.plan())
        self.assertIn(
            "Repositories, in order:\n1. ai-pm-agent: move op\n2. ai-dashboard: drag UI",
            text,
        )
        self.assertIn("Approving runs only ai-pm-agent", text)
        outline = plan_state.plan_outline(self.plan())
        self.assertEqual(outline["repositories"][1]["name"], "ai-dashboard")
        self.assertIn(
            "ai-dashboard would need their own task", outline["repository_note"]
        )

    def test_the_implementer_is_told_to_change_only_this_repository(self) -> None:
        document = plan_state.parse_plan_document(self.PLAN)
        self.assertIn("Change only this repository", document.codex_prompt)
        single = json.loads(self.PLAN)
        single["repositories"] = single["repositories"][:1]
        document = plan_state.parse_plan_document(json.dumps(single))
        self.assertNotIn("Change only this repository", document.codex_prompt)

    def test_old_plans_without_repositories_still_render(self) -> None:
        old = json.loads(self.PLAN)
        del old["repositories"]
        plan = plan_state.new_plan_state("x", json.dumps(old))
        self.assertNotIn("Repositories", plan_state.render_plan(plan))
        self.assertEqual(plan_state.plan_outline(plan)["repositories"], [])
        self.assertEqual(plan_state.plan_outline(plan)["repository_note"], "")
