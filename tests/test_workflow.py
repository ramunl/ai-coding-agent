import unittest

from ai_agent.workflow import slugify_branch_name, validate_branch_name


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


if __name__ == "__main__":
    unittest.main()
