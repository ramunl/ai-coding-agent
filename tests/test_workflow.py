import unittest
from unittest.mock import patch

from ai_agent.shell import CommandResult
from ai_agent.workflow import (
    implementation_command,
    repair_implementation,
    repair_pull_request_branch,
    slugify_branch_name,
    validate_branch_name,
)


class WorkflowTests(unittest.TestCase):
    def test_slugify_branch_name_normalizes_feature_text(self) -> None:
        branch = slugify_branch_name("Add per-channel proxy toggle!")

        self.assertEqual(branch, "feature/add-per-channel-proxy-toggle")

    def test_slugify_branch_name_falls_back_for_empty_text(self) -> None:
        branch = slugify_branch_name("...")

        self.assertEqual(branch, "feature/change")

    def test_slugify_branch_name_accepts_bugfix_prefix(self) -> None:
        branch = slugify_branch_name("Player crashes after rotation", "bugfix")

        self.assertEqual(branch, "bugfix/player-crashes-after-rotation")

    def test_slugify_branch_name_replaces_reserved_path_characters(self) -> None:
        branch = slugify_branch_name("Channel proxy on/off restarts cast (TV): [bad]?")

        self.assertEqual(branch, "feature/channel-proxy-on-off-restarts-cast-tv-bad")

    def test_validate_branch_name_rejects_invalid_names(self) -> None:
        invalid_names = [
            "/feature/start",
            "feature/end/",
            "feature/end.",
            "feature/has..dots",
            "feature/@{bad",
            "feature\\bad",
            "feature/space bad",
        ]

        for branch_name in invalid_names:
            with self.subTest(branch_name=branch_name):
                with self.assertRaises(ValueError):
                    validate_branch_name(branch_name)

    def test_implementation_command_defaults_to_codex(self) -> None:
        self.assertEqual(implementation_command("do work", "codex"), ["codex", "exec", "do work"])

    def test_implementation_command_supports_claude(self) -> None:
        self.assertEqual(
            implementation_command("do work", "claude"),
            ["claude", "-p", "do work", "--permission-mode", "bypassPermissions"],
        )

    @patch("ai_agent.workflow.run")
    def test_repair_implementation_runs_codex_on_existing_branch(self, mock_run) -> None:
        def fake_run(args, *unused_args, **unused_kwargs):
            if args == ["git", "status", "--porcelain"]:
                return CommandResult(args, 0, " M File.kt\n")
            if args == ["git", "diff", "--no-ext-diff"]:
                return CommandResult(args, 0, "diff --git a/File.kt b/File.kt\n")
            return CommandResult(args, 0, "ok\n")

        mock_run.side_effect = fake_run

        result = repair_implementation("fix compile error", "bugfix/example")

        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertIn(["git", "checkout", "bugfix/example"], calls)
        self.assertIn(["codex", "exec", "fix compile error"], calls)
        self.assertEqual(result.files_changed, ["File.kt"])

    @patch("ai_agent.workflow.run")
    def test_repair_implementation_can_run_claude(self, mock_run) -> None:
        def fake_run(args, *unused_args, **unused_kwargs):
            if args == ["git", "status", "--porcelain"]:
                return CommandResult(args, 0, " M File.kt\n")
            if args == ["git", "diff", "--no-ext-diff"]:
                return CommandResult(args, 0, "diff --git a/File.kt b/File.kt\n")
            return CommandResult(args, 0, "ok\n")

        mock_run.side_effect = fake_run

        repair_implementation("fix compile error", "bugfix/example", "claude")

        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertIn(["claude", "-p", "fix compile error", "--permission-mode", "bypassPermissions"], calls)

    @patch("ai_agent.workflow.run")
    def test_repair_pull_request_branch_resets_from_origin_branch(self, mock_run) -> None:
        def fake_run(args, *unused_args, **unused_kwargs):
            if args == ["git", "status", "--porcelain"]:
                return CommandResult(args, 0, " M File.kt\n")
            if args == ["git", "diff", "--no-ext-diff"]:
                return CommandResult(args, 0, "diff --git a/File.kt b/File.kt\n")
            return CommandResult(args, 0, "ok\n")

        mock_run.side_effect = fake_run

        result = repair_pull_request_branch("fix compile error", "bugfix/example")

        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertIn(["git", "fetch", "origin", "bugfix/example"], calls)
        self.assertIn(["git", "checkout", "-B", "bugfix/example", "origin/bugfix/example"], calls)
        self.assertIn(["codex", "exec", "fix compile error"], calls)
        self.assertEqual(result.files_changed, ["File.kt"])


if __name__ == "__main__":
    unittest.main()
