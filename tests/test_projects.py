import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


class ProjectsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-secret"

        self.tmp = tempfile.mkdtemp()
        self.projects_file = Path(self.tmp) / "projects.json"
        os.environ["PROJECTS_FILE"] = str(self.projects_file)
        os.environ["PROJECTS_ROOT"] = str(Path(self.tmp) / "repos")
        os.environ["REPO_PATH"] = str(Path(self.tmp) / "legacy-repo")
        os.environ["GITHUB_REPOSITORY"] = "ramunl/channel-cast"
        os.environ["GITHUB_BASE_BRANCH"] = "main"

        self._reload()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)
        for module in ("ai_agent.config", "ai_agent.projects", "ai_agent.shell"):
            sys.modules.pop(module, None)

    def _reload(self):
        for module in ("ai_agent.config", "ai_agent.projects", "ai_agent.shell"):
            sys.modules.pop(module, None)
        return importlib.import_module("ai_agent.projects")

    def _write_registry(self, data: dict) -> None:
        self.projects_file.write_text(json.dumps(data), encoding="utf-8")

    # ---- fallback behaviour (backward compatibility) ----

    def test_env_fallback_when_no_file(self) -> None:
        projects = self._reload()
        active = projects.active_project()
        self.assertEqual(active.name, "channel-cast")
        self.assertEqual(active.github_repository, "ramunl/channel-cast")
        self.assertEqual(active.repo_path, Path(self.tmp) / "legacy-repo")

    def test_corrupt_file_falls_back_instead_of_crashing(self) -> None:
        self.projects_file.write_text("{ not json", encoding="utf-8")
        projects = self._reload()
        self.assertEqual(projects.active_project().name, "channel-cast")

    # ---- repository parsing ----

    def test_normalize_repository_forms(self) -> None:
        projects = self._reload()
        for value in (
            "ramunl/ai-rules",
            "git@github.com:ramunl/ai-rules.git",
            "https://github.com/ramunl/ai-rules",
            "https://github.com/ramunl/ai-rules.git",
        ):
            self.assertEqual(projects.normalize_repository(value), "ramunl/ai-rules")

    def test_normalize_repository_rejects_garbage(self) -> None:
        projects = self._reload()
        with self.assertRaises(projects.ProjectError):
            projects.normalize_repository("not-a-repo")

    # ---- add / switch / remove ----

    def test_add_project_registers_and_reports_needs_clone(self) -> None:
        projects = self._reload()
        project, needs_clone = projects.add_project("ramunl/other-app")
        self.assertEqual(project.name, "other-app")
        self.assertTrue(needs_clone)
        self.assertIn("other-app", [item.name for item in projects.list_projects()])

    def test_add_project_preserves_env_project(self) -> None:
        projects = self._reload()
        projects.add_project("ramunl/other-app")
        names = [item.name for item in projects.list_projects()]
        self.assertIn("channel-cast", names)
        self.assertIn("other-app", names)

    def test_set_active_switches_and_persists(self) -> None:
        projects = self._reload()
        projects.add_project("ramunl/other-app")
        projects.set_active("other-app")
        self.assertEqual(projects.active_project().name, "other-app")

        # Fresh import must still see the switch: state lives on disk.
        reloaded = self._reload()
        self.assertEqual(reloaded.active_project().name, "other-app")

    def test_set_active_unknown_raises(self) -> None:
        projects = self._reload()
        with self.assertRaises(projects.ProjectError):
            projects.set_active("nope")

    def test_remove_project_switches_active_away(self) -> None:
        projects = self._reload()
        projects.add_project("ramunl/other-app")
        projects.set_active("other-app")
        projects.remove_project("other-app")
        self.assertEqual(projects.active_project().name, "channel-cast")

    def test_cannot_remove_only_project(self) -> None:
        projects = self._reload()
        self._write_registry(
            {
                "active": "solo",
                "projects": {
                    "solo": {
                        "repo_path": "/tmp/solo",
                        "github_repository": "ramunl/solo",
                        "base_branch": "main",
                    }
                },
            }
        )
        with self.assertRaises(projects.ProjectError):
            projects.remove_project("solo")

    def test_missing_active_key_falls_back_to_first(self) -> None:
        projects = self._reload()
        self._write_registry(
            {
                "active": "deleted-project",
                "projects": {
                    "alpha": {
                        "repo_path": "/tmp/alpha",
                        "github_repository": "ramunl/alpha",
                        "base_branch": "main",
                    }
                },
            }
        )
        self.assertEqual(projects.active_project().name, "alpha")

    # ---- the regression this refactor exists to prevent ----

    def test_shell_cwd_follows_active_project_at_call_time(self) -> None:
        """shell.run used to bind REPO_PATH at import; it must resolve per call."""
        self._reload()
        projects = importlib.import_module("ai_agent.projects")
        shell = importlib.import_module("ai_agent.shell")

        alpha = Path(self.tmp) / "alpha"
        beta = Path(self.tmp) / "beta"
        alpha.mkdir()
        beta.mkdir()
        self._write_registry(
            {
                "active": "alpha",
                "projects": {
                    "alpha": {"repo_path": str(alpha), "github_repository": "r/alpha", "base_branch": "main"},
                    "beta": {"repo_path": str(beta), "github_repository": "r/beta", "base_branch": "main"},
                },
            }
        )

        first = shell.run(["pwd"])
        self.assertEqual(first.output.strip(), str(alpha))

        projects.set_active("beta")

        second = shell.run(["pwd"])
        self.assertEqual(second.output.strip(), str(beta))


if __name__ == "__main__":
    unittest.main()
