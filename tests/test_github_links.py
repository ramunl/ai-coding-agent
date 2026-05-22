import unittest
from unittest.mock import patch

from ai_agent.github_links import (
    build_github_links_context,
    enrich_feature_description,
    find_github_references,
    parse_github_reference,
)


class GitHubLinksTests(unittest.TestCase):
    def test_parse_github_reference_accepts_issue_and_pr_urls(self) -> None:
        issue = parse_github_reference("https://github.com/owner/repo/issues/12")
        pull = parse_github_reference("https://github.com/owner/repo/pull/34")

        self.assertEqual((issue.owner, issue.repo, issue.kind, issue.number), ("owner", "repo", "issues", 12))
        self.assertEqual((pull.owner, pull.repo, pull.kind, pull.number), ("owner", "repo", "pull", 34))

    def test_parse_github_reference_rejects_other_urls(self) -> None:
        self.assertIsNone(parse_github_reference("https://example.com/owner/repo/issues/12"))
        self.assertIsNone(parse_github_reference("https://github.com/owner/repo/tree/main"))

    def test_find_github_references_deduplicates_and_strips_punctuation(self) -> None:
        references = find_github_references(
            "Fix https://github.com/owner/repo/issues/12, also https://github.com/owner/repo/issues/12."
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].url, "https://github.com/owner/repo/issues/12")

    @patch("ai_agent.github_links.github_request")
    def test_build_github_links_context_fetches_issue_and_comments(self, mock_request) -> None:
        mock_request.side_effect = [
            {
                "title": "Broken playback",
                "body": "Playback fails on some streams",
                "state": "open",
                "user": {"login": "roman"},
            },
            [{"body": "Happens on Android TV", "user": {"login": "tester"}}],
        ]

        context = build_github_links_context("https://github.com/owner/repo/issues/12")

        self.assertIn("Issue owner/repo#12: Broken playback", context)
        self.assertIn("Playback fails on some streams", context)
        self.assertIn("tester: Happens on Android TV", context)

    @patch("ai_agent.github_links.build_github_links_context", return_value="Issue context")
    def test_enrich_feature_description_appends_link_context(self, _mock_context) -> None:
        enriched = enrich_feature_description("Fix linked issue")

        self.assertIn("Fix linked issue", enriched)
        self.assertIn("GitHub link context:", enriched)
        self.assertIn("Issue context", enriched)


if __name__ == "__main__":
    unittest.main()
