import base64
import unittest
from unittest.mock import patch

from ai_agent.github_links import (
    build_github_links_context,
    enrich_feature_description,
    fetch_web_reference_context,
    find_github_references,
    parse_github_reference,
    WebReference,
)


class GitHubLinksTests(unittest.TestCase):
    def test_parse_github_reference_accepts_issue_and_pr_urls(self) -> None:
        issue = parse_github_reference("https://github.com/owner/repo/issues/12")
        pull = parse_github_reference("https://github.com/owner/repo/pull/34")

        self.assertEqual((issue.owner, issue.repo, issue.kind, issue.number), ("owner", "repo", "issues", 12))
        self.assertEqual((pull.owner, pull.repo, pull.kind, pull.number), ("owner", "repo", "pull", 34))

    def test_parse_github_reference_accepts_blob_and_commit_urls(self) -> None:
        blob = parse_github_reference("https://github.com/owner/repo/blob/main/app/src/Main.kt")
        commit = parse_github_reference("https://github.com/owner/repo/commit/abcdef123456")

        self.assertEqual((blob.owner, blob.repo, blob.kind, blob.ref, blob.path), ("owner", "repo", "blob", "main", "app/src/Main.kt"))
        self.assertEqual((commit.owner, commit.repo, commit.kind, commit.sha), ("owner", "repo", "commit", "abcdef123456"))

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

    @patch("ai_agent.github_links.github_request")
    def test_build_github_links_context_fetches_blob_content(self, mock_request) -> None:
        mock_request.return_value = {
            "content": base64.b64encode(b"fun main() = Unit").decode("ascii"),
            "encoding": "base64",
            "size": 17,
        }

        context = build_github_links_context("https://github.com/owner/repo/blob/main/app/src/Main.kt")

        self.assertIn("File owner/repo/app/src/Main.kt", context)
        self.assertIn("Ref: main", context)
        self.assertIn("fun main() = Unit", context)

    @patch("ai_agent.github_links.github_request")
    def test_build_github_links_context_fetches_commit_summary(self, mock_request) -> None:
        mock_request.return_value = {
            "commit": {"message": "Fix playback", "author": {"name": "Roman"}},
            "files": [{"filename": "app/src/Main.kt", "status": "modified", "additions": 3, "deletions": 1}],
        }

        context = build_github_links_context("https://github.com/owner/repo/commit/abcdef123456")

        self.assertIn("Commit owner/repo@abcdef123456", context)
        self.assertIn("Fix playback", context)
        self.assertIn("app/src/Main.kt", context)

    @patch("ai_agent.github_links.urllib.request.urlopen")
    def test_fetch_web_reference_context_extracts_html_text(self, mock_urlopen) -> None:
        response = mock_urlopen.return_value.__enter__.return_value
        response.headers = {"content-type": "text/html"}
        response.read.return_value = b"<html><title>Android docs</title><body><script>x</script><h1>Activity</h1></body></html>"

        context = fetch_web_reference_context(WebReference("https://developer.android.com/guide", "developer.android.com"))

        self.assertIn("Web page: Android docs", context)
        self.assertIn("Activity", context)
        self.assertNotIn("<script>", context)

    @patch("ai_agent.github_links.build_link_context", return_value="Issue context")
    def test_enrich_feature_description_appends_link_context(self, _mock_context) -> None:
        enriched = enrich_feature_description("Fix linked issue")

        self.assertIn("Fix linked issue", enriched)
        self.assertIn("Link context:", enriched)
        self.assertIn("Issue context", enriched)


if __name__ == "__main__":
    unittest.main()
