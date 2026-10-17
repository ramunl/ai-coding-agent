import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_agent import code_review, log_scan, tasks

ANSWER = json.dumps(
    {
        "proposals": [
            {
                "kind": "bug",
                "title": "Inbox drops requests over 4 KB",
                "detail": "inbox.py reads 4096 bytes and fails on bigger files.",
                "files": ["ai_agent/inbox.py"],
            },
            {"kind": "feature", "title": "Weekly summary", "detail": "x", "files": []},
            {"kind": "style", "title": "Rename things", "detail": "", "files": []},
            {"kind": "improvement", "title": "", "detail": "no title", "files": []},
        ]
    }
)


NOW = 1_800_000_000.0


class CodeReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "AGENTS.md").write_text("# repo\n\nA Telegram bot.\n")
        self.data: dict = {"worker": {"enabled": True}}
        self.asked: list = []
        self.rules = patch.object(code_review, "rules_prompt_block", lambda: "")
        self.rules.start()

    def tearDown(self) -> None:
        self.rules.stop()
        self.tmp.cleanup()

    def reviewer(self, answer: str = ANSWER):
        def run(tool: str, text: str, path: Path) -> str:
            self.asked.append((tool, text, path))
            return f"Here you go:\n{answer}"

        return run

    def review(self, count: int = 3, now: float = NOW, answer: str = ANSWER):
        return code_review.review(
            self.data, "repo", self.repo, "codex", count, now, self.reviewer(answer)
        )

    def test_proposals_become_proposed_tasks_with_kind_and_files(self) -> None:
        created = self.review()
        self.assertEqual(
            [task["title"] for task in created],
            ["Inbox drops requests over 4 KB", "Weekly summary"],
        )
        first = created[0]
        self.assertEqual(first["stage"], "proposed")
        self.assertEqual(first["source"], "review")
        self.assertEqual(first["note"], "bug · code review")
        self.assertIn("Files: ai_agent/inbox.py", first["text"])
        self.assertEqual(first["evidence"][-1], "ai_agent/inbox.py")
        tool, text, path = self.asked[0]
        self.assertEqual((tool, path), ("codex", self.repo))
        self.assertIn("A Telegram bot.", text)
        self.assertIn("at most 3 changes", text)

    def test_the_count_is_capped_and_repeats_are_skipped(self) -> None:
        self.assertEqual(len(self.review(count=1)), 1)
        again = self.review(count=3, now=NOW + 1000)
        self.assertEqual([task["title"] for task in again], ["Weekly summary"])
        self.assertIn("Inbox drops requests", self.asked[-1][1])  # told to skip it

    def test_a_dismissed_proposal_is_not_proposed_again(self) -> None:
        (first, second) = self.review()
        log_scan.dismiss(self.data, first, NOW)
        tasks.tasks_of(self.data).clear()
        again = self.review(now=NOW + 24 * 3600)
        self.assertEqual(again, [])  # quiet for 30 days

    def test_repositories_are_reviewed_in_turn_once_a_day(self) -> None:
        names = ["b-repo", "a-repo"]
        self.assertTrue(code_review.is_due(self.data, NOW))
        self.assertEqual(code_review.next_repository(self.data, names), "a-repo")
        self.review(answer="{}")
        self.assertFalse(code_review.is_due(self.data, NOW + 3600))
        self.assertEqual(code_review.next_repository(self.data, names), "b-repo")
        self.assertEqual(self.data["worker"]["review"]["done"]["proposed"], 0)
        self.assertIsNone(code_review.next_repository(self.data, []))

    def test_unreadable_answers_propose_nothing(self) -> None:
        self.assertEqual(code_review.parse("no json here", 3), [])
        self.assertEqual(code_review.parse('{"proposals": "x"}', 3), [])
