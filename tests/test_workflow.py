import unittest
from unittest.mock import patch

from ai_agent.shell import CommandResult
from ai_agent.workflow import (
    implementation_command,
    repair_implementation,
    repair_pull_request_branch,
    return_to_base_branch,
    slugify_branch_name,
    truncate_slug,
    validate_branch_name,
)


class WorkflowTests(unittest.TestCase):
    def test_slugify_branch_name_normalizes_feature_text(self) -> None:
        branch = slugify_branch_name("Add per-channel proxy toggle!")

        self.assertEqual(branch, "feature/add-per-channel")

    def test_slugify_branch_name_falls_back_for_empty_text(self) -> None:
        branch = slugify_branch_name("...")

        self.assertEqual(branch, "feature/change")

    def test_slugify_branch_name_accepts_bugfix_prefix(self) -> None:
        branch = slugify_branch_name("Player crashes after rotation", "bugfix")

        self.assertEqual(branch, "bugfix/player-crashes")

    def test_slugify_branch_name_replaces_reserved_path_characters(self) -> None:
        branch = slugify_branch_name("Channel proxy on/off restarts cast (TV): [bad]?")

        self.assertEqual(branch, "feature/channel-proxy-on")

    def test_slugify_branch_name_truncates_at_word_boundary(self) -> None:
        description = (
            "telegram bot doesn't show an inline hint listing all available "
            "slash commands immediately after the user types the '/' character"
        )

        branch = slugify_branch_name(description, "bugfix")

        self.assertLessEqual(len(branch.removeprefix("bugfix/")), 20)
        self.assertFalse(branch.endswith("-"))
        # Every remaining segment should be a whole word, never a chopped fragment.
        for word in branch.removeprefix("bugfix/").split("-"):
            self.assertIn(word, description.lower())

    def test_truncate_slug_falls_back_to_hard_cut_for_single_long_word(self) -> None:
        slug = truncate_slug("a" * 100, 60)

        self.assertEqual(slug, "a" * 60)

    def test_truncate_slug_keeps_short_slug_unchanged(self) -> None:
        self.assertEqual(truncate_slug("short-slug", 60), "short-slug")

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
        self.assertEqual(
            implementation_command("do work", "codex"),
            ["codex", "exec", "-s", "workspace-write", "do work"],
        )

    def test_implementation_command_supports_claude(self) -> None:
        self.assertEqual(
            implementation_command("do work", "claude"),
            ["claude", "-p", "do work", "--permission-mode", "acceptEdits"],
        )

    @patch("ai_agent.workflow.CLAUDE_CODE_ARGS", ("--permission-mode", "bypassPermissions"))
    @patch("ai_agent.workflow.os.geteuid", return_value=0)
    def test_root_replaces_claude_bypass_permissions(self, unused_geteuid) -> None:
        self.assertEqual(
            implementation_command("do work", "claude"),
            ["claude", "-p", "do work", "--permission-mode", "acceptEdits"],
        )

    @patch("ai_agent.workflow.CLAUDE_CODE_ARGS", ("--dangerously-skip-permissions",))
    @patch("ai_agent.workflow.os.geteuid", return_value=0)
    def test_root_removes_dangerous_claude_flag(self, unused_geteuid) -> None:
        self.assertEqual(implementation_command("do work", "claude"), ["claude", "-p", "do work"])

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
        self.assertIn(["codex", "exec", "-s", "workspace-write", "fix compile error"], calls)
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
        self.assertIn(["claude", "-p", "fix compile error", "--permission-mode", "acceptEdits"], calls)

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
        self.assertIn(["codex", "exec", "-s", "workspace-write", "fix compile error"], calls)
        self.assertEqual(result.files_changed, ["File.kt"])

    @patch("ai_agent.workflow.run")
    @patch("ai_agent.workflow.active_project")
    def test_return_to_base_branch_checks_out_and_pulls_base(self, mock_active_project, mock_run) -> None:
        mock_active_project.return_value.base_branch = "main"
        mock_run.return_value = CommandResult(["git"], 0, "ok\n")

        return_to_base_branch()

        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertIn(["git", "checkout", "main"], calls)
        self.assertIn(["git", "pull", "origin", "main"], calls)


if __name__ == "__main__":
    unittest.main()
