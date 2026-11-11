"""Tasks follow their pull request on GitHub: open, merged, closed.

Lookups are classified into explicit evidence states (absent, verified,
mismatched, unavailable) so "no PR" is never confused with "lookup failed".
"""

import asyncio
import importlib
import os
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai_agent.context_models import (
    AttemptSnapshot,
    EvidenceReference,
    RepositoryIdentity,
    Scope,
)
from ai_agent.task_pull_evidence import EvidenceStatus, HeadProvenance


class TaskPullTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        self.pulls = importlib.import_module("ai_agent.task_pulls")
        self.tasks = importlib.import_module("ai_agent.tasks")
        self.data: dict = {}
        self.task = self.tasks.add_task(self.data, "add login", "repo", None)
        self.task.update(stage="pr", branch="feature/login")
        self.repo = patch.object(
            self.pulls,
            "get_project",
            lambda name: SimpleNamespace(github_repository="ramunl/repo"),
        )
        self.repo.start()

    def tearDown(self) -> None:
        self.repo.stop()
        os.environ.clear()
        os.environ.update(self.previous_env)

    def test_asks_github_for_the_branch_read_only(self) -> None:
        calls = []

        def fake(method, path, data=None, query=None):
            calls.append((method, path, query))
            return [
                {
                    "html_url": "https://github.com/ramunl/repo/pull/7",
                    "state": "closed",
                    "merged_at": "2026-10-08T10:00:00Z",
                    "merge_commit_sha": "abc123",
                }
            ]

        with patch.object(self.pulls, "github_request", fake):
            pull = self.pulls.pull_state("ramunl/repo", "feature/login")
        self.assertEqual(
            calls,
            [
                (
                    "GET",
                    "/repos/ramunl/repo/pulls",
                    {"head": "ramunl:feature/login", "state": "all", "per_page": "5"},
                )
            ],
        )
        self.assertEqual(pull["state"], "merged")
        self.assertEqual(pull["merge_commit"], "abc123")
        expected = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(pull["merged_at"], expected)

    def test_merged_closed_and_open_move_the_task(self) -> None:
        merged = {"url": "u", "state": "merged", "merged_at": 5.0, "merge_commit": "c"}
        self.assertTrue(self.pulls.apply_pull(self.task, merged))
        self.assertEqual((self.task["stage"], self.task["merged_at"]), ("done", 5.0))
        other = self.tasks.add_task(self.data, "x", "repo", None)
        other.update(stage="ended", note=self.tasks.ENDED_NOTE)
        self.assertTrue(self.pulls.apply_pull(other, {"url": "u2", "state": "open"}))
        self.assertEqual(
            (other["stage"], other["note"], other["pr_url"]), ("pr", None, "u2")
        )
        self.assertFalse(self.pulls.apply_pull(other, {"url": "u2", "state": "open"}))
        self.assertTrue(self.pulls.apply_pull(other, {"url": "u2", "state": "closed"}))
        self.assertEqual(other["stage"], "stopped")
        self.assertFalse(self.pulls.apply_pull(other, None))

    def test_apply_pull_accepts_exact_pr_facts_shape(self) -> None:
        merged = {
            "state": "closed",
            "merged": True,
            "merged_at": 5.0,
            "merge_commit": "c",
            "url": "u",
            "head": "a" * 40,
        }
        self.assertTrue(self.pulls.apply_pull(self.task, merged))
        self.assertEqual((self.task["stage"], self.task["merge_commit"]), ("done", "c"))
        open_task = self.tasks.add_task(self.data, "open", "repo", None)
        open_task.update(stage="ended")
        open_facts = {
            "state": "open",
            "merged": False,
            "reviewable": True,
            "url": "open-url",
            "head": "b" * 40,
        }
        self.assertTrue(self.pulls.apply_pull(open_task, open_facts))
        self.assertEqual(open_task["stage"], "pr")
        closed_facts = {
            "state": "closed",
            "merged": False,
            "reviewable": False,
            "url": "closed-url",
            "head": "c" * 40,
        }
        self.assertTrue(self.pulls.apply_pull(open_task, closed_facts))
        self.assertEqual(open_task["stage"], "stopped")

    async def test_check_once_skips_other_stages_and_survives_failures(self) -> None:
        todo = self.tasks.add_task(self.data, "later", "repo", None)
        broken = self.tasks.add_task(self.data, "broken", "repo", None)
        broken.update(stage="pr", branch="feature/broken")
        verified = {
            "url": "u",
            "state": "merged",
            "merged_at": 1.0,
            "merge_commit": "c",
        }

        def fake(task, repository):
            if task["branch"] == "feature/broken":
                return self.pulls.PullEvidence(
                    EvidenceStatus.UNAVAILABLE, reason="GitHub API failed (404)"
                )
            return self.pulls.PullEvidence(
                EvidenceStatus.VERIFIED, pull=verified, reason="verified"
            )

        with (
            patch.object(self.pulls, "evidence_for", fake),
            self.assertLogs("ai_agent.task_pulls", "WARNING"),
        ):
            self.assertTrue(await self.pulls.check_once(self.data))
        self.assertEqual(self.task["stage"], "done")
        self.assertEqual(broken["stage"], "pr")
        self.assertEqual(todo["stage"], "todo")

    async def test_stopped_identity_bound_attempt_is_rechecked_for_merged_pr(self):
        self.task.update(stage="stopped", context_handoff={"record_id": "attempt"})
        merged = {"url": "u", "state": "merged", "merged_at": 1.0, "merge_commit": "c"}
        with patch.object(
            self.pulls,
            "evidence_for",
            return_value=self.pulls.PullEvidence(
                EvidenceStatus.VERIFIED, pull=merged, reason="verified"
            ),
        ):
            self.assertTrue(await self.pulls.check_once(self.data))
        self.assertEqual(self.task["stage"], "done")

    async def test_unbound_existing_pr_is_unknown_not_confirmed_absent(self):
        self.task.update(stage="ended", run={"context_spec": {"task_id": "new"}})
        before = dict(self.task)
        with (
            patch.object(
                self.pulls,
                "evidence_for",
                return_value=self.pulls.PullEvidence(
                    EvidenceStatus.MISMATCHED,
                    pull={"url": "old", "state": "merged"},
                    reason="existing PR does not match the archived attempt",
                ),
            ),
            self.assertLogs("ai_agent.task_pulls", "WARNING"),
        ):
            self.assertFalse(await self.pulls.check_once(self.data))
        self.assertEqual(self.task, before)

    async def test_loop_marks_changes_for_saving(self) -> None:
        app = SimpleNamespace(
            user_data={123: self.data}, mark_data_for_update_persistence=MagicMock()
        )
        merged = {"url": "u", "state": "merged", "merged_at": 1.0, "merge_commit": "c"}
        with patch.object(
            self.pulls,
            "evidence_for",
            return_value=self.pulls.PullEvidence(
                EvidenceStatus.VERIFIED, pull=merged, reason="verified"
            ),
        ):
            loop = asyncio.create_task(
                self.pulls.track_forever(app, 123, interval=0.01)
            )
            await asyncio.sleep(0.05)
            loop.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop
        app.mark_data_for_update_persistence.assert_called_with(user_ids=123)
        self.assertIn("done", self.tasks.FINISHED)

    def test_an_ended_run_without_a_pull_request_is_stopped_not_pr_open(self) -> None:
        self.task.update(stage="ended", note=self.tasks.ENDED_NOTE)
        self.assertTrue(self.pulls.apply_pull(self.task, None))
        self.assertEqual(self.task["stage"], "stopped")
        self.assertIn("without a pull request", self.task["note"])
        pr_task = self.tasks.add_task(self.data, "y", "repo", None)
        pr_task.update(stage="pr", branch="b")
        self.assertTrue(self.pulls.apply_pull(pr_task, None))
        self.assertEqual(pr_task["stage"], "stopped")

    async def test_ended_tasks_are_checked_again_soon(self) -> None:
        self.task.update(stage="ended")
        app = SimpleNamespace(
            user_data={123: self.data}, mark_data_for_update_persistence=MagicMock()
        )
        calls = []

        def first_absent_then_open(task, repository):
            calls.append(task["branch"])
            if len(calls) == 1:
                return self.pulls.PullEvidence(EvidenceStatus.ABSENT)
            return self.pulls.PullEvidence(
                EvidenceStatus.VERIFIED,
                pull={"url": "u", "state": "open"},
                reason="verified",
            )

        other = self.tasks.add_task(self.data, "z", "repo", None)
        other.update(stage="ended", branch="feature/z")
        with (
            patch.object(self.pulls, "evidence_for", first_absent_then_open),
            patch.object(self.pulls, "ENDED_POLL_SECONDS", 0.01),
        ):
            loop = asyncio.create_task(self.pulls.track_forever(app, 123, interval=60))
            for _ in range(200):
                if self.task["stage"] == "stopped" and other["stage"] == "pr":
                    break
                await asyncio.sleep(0.01)
            loop.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop
        self.assertEqual(calls, ["feature/login", "feature/z"])
        self.assertEqual((self.task["stage"], other["stage"]), ("stopped", "pr"))

    async def test_empty_lookup_clears_stale_metadata_for_pr_and_ended(self) -> None:
        for stage in ("pr", "ended"):
            with self.subTest(stage=stage):
                self.task.update(
                    stage=stage, pr_url="old", merged_at=1, merge_commit="s"
                )
                with patch.object(self.pulls, "github_request", return_value=[]):
                    self.assertTrue(await self.pulls.check_once(self.data))
                self.assertEqual(self.task["stage"], "stopped")
                self.assertIn("GitHub confirmed none", self.task["note"])
                for key in ("pr_url", "merged_at", "merge_commit"):
                    self.assertIsNone(self.task.get(key))
                self.assertIn("finished_at", self.task)
                self.assertFalse(self.tasks.sync(self.data))

    async def test_invalid_responses_and_errors_preserve_status(self) -> None:
        invalid = [
            None,
            {},
            "",
            [None],
            [{}],
            [{"state": "unknown"}],
            [{"state": "open"}],
            [{"state": "closed", "html_url": "u", "merged_at": "bad"}],
        ]
        for stage in ("pr", "ended"):
            for response in invalid:
                with self.subTest(stage=stage, response=response):
                    self.task.update(stage=stage, pr_url="old", note="unchanged")
                    before = dict(self.task)
                    with (
                        patch.object(
                            self.pulls, "github_request", return_value=response
                        ),
                        self.assertLogs("ai_agent.task_pulls", "WARNING"),
                    ):
                        self.assertFalse(await self.pulls.check_once(self.data))
                    self.assertEqual(self.task, before)
            with (
                patch.object(
                    self.pulls, "github_request", side_effect=RuntimeError("offline")
                ),
                self.assertLogs("ai_agent.task_pulls", "WARNING"),
            ):
                self.assertFalse(await self.pulls.check_once(self.data))
            self.assertEqual(self.task, before)

    def test_unknown_state_cannot_promote_ended_task(self) -> None:
        self.task.update(stage="ended")
        before = dict(self.task)
        for state in (None, "unknown", ""):
            with self.assertRaises(ValueError):
                self.pulls.apply_pull(self.task, {"state": state, "url": "u"})
            self.assertEqual(self.task, before)

    def test_exact_archived_pr_is_preferred_over_branch_search(self) -> None:
        record = SimpleNamespace(
            evidence=(
                EvidenceReference("commit", "commit", "a" * 40, "head"),
                EvidenceReference(
                    "pr",
                    "pull_request",
                    "https://github.com/ramunl/repo/pull/7",
                    "PR",
                ),
            )
        )
        facts = {"head": "a" * 40, "url": "https://github.com/ramunl/repo/pull/7"}
        archived = self.task
        archived["context_handoff"] = {"record_id": "archived"}

        def no_branch_search(repository, branch):
            raise AssertionError("branch search must not run for an archived PR")

        with (
            patch.object(
                self.pulls,
                "archived_attempt",
                return_value=(SimpleNamespace(github_repository="ramunl/repo"), record),
            ),
            patch.object(self.pulls, "pull_facts", return_value=facts),
            patch.object(self.pulls, "pull_state", no_branch_search),
        ):
            evidence = self.pulls.evidence_for(archived, "ramunl/repo")
        self.assertIs(evidence.status, EvidenceStatus.VERIFIED)
        self.assertIs(evidence.pull, facts)

    def test_repaired_head_is_mismatched_until_owner_reconciles(self) -> None:
        record = SimpleNamespace(
            evidence=(
                EvidenceReference("commit", "commit", "a" * 40, "head"),
                EvidenceReference(
                    "pr",
                    "pull_request",
                    "https://github.com/ramunl/repo/pull/7",
                    "PR",
                ),
            )
        )
        facts = {"head": "b" * 40, "url": "https://github.com/ramunl/repo/pull/7"}
        self.task["context_handoff"] = {"record_id": "archived"}
        with (
            patch.object(
                self.pulls,
                "archived_attempt",
                return_value=(SimpleNamespace(github_repository="ramunl/repo"), record),
            ),
            patch.object(self.pulls, "pull_facts", return_value=facts),
        ):
            evidence = self.pulls.evidence_for(self.task, "ramunl/repo")
        self.assertIs(evidence.status, EvidenceStatus.MISMATCHED)
        self.assertIn("reconcile_task", evidence.reason)

    def test_absent_verified_mismatched_and_unavailable_are_explicit(self) -> None:
        branch = {"url": "u", "head_sha": "a" * 40, "state": "merged"}
        # Absent: GitHub answered no PR.
        with (
            patch.object(self.pulls, "pull_state", return_value=None),
            patch.object(self.pulls, "attempt_pull", return_value=branch),
        ):
            self.assertIs(
                self.pulls.evidence_for(self.task, "ramunl/repo").status,
                EvidenceStatus.ABSENT,
            )
        # Verified: branch-discovered PR bound to the attempt.
        with (
            patch.object(self.pulls, "pull_state", return_value=branch),
            patch.object(self.pulls, "attempt_pull", return_value=branch),
        ):
            self.assertIs(
                self.pulls.evidence_for(self.task, "ramunl/repo").status,
                EvidenceStatus.VERIFIED,
            )
        # Mismatched: PR exists but does not bind.
        with (
            patch.object(self.pulls, "pull_state", return_value=branch),
            patch.object(self.pulls, "attempt_pull", return_value=None),
        ):
            self.assertIs(
                self.pulls.evidence_for(self.task, "ramunl/repo").status,
                EvidenceStatus.MISMATCHED,
            )
        # Unavailable: lookup failed.
        with (
            patch.object(self.pulls, "pull_state", side_effect=RuntimeError("offline")),
            patch.object(self.pulls, "attempt_pull", return_value=branch),
        ):
            self.assertIs(
                self.pulls.evidence_for(self.task, "ramunl/repo").status,
                EvidenceStatus.UNAVAILABLE,
            )

    def test_archived_attempt_rejects_attempt_ownership_mismatch(self) -> None:
        scope = Scope("123", RepositoryIdentity("repo", "/repo", "ramunl/repo"))
        dependency = SimpleNamespace(
            attempt=SimpleNamespace(record_id="archived", revision=1), scope=scope
        )
        record = AttemptSnapshot(
            record_id="archived",
            revision=1,
            scope=scope,
            summary="observed",
            task_id="11111111",
            attempt_id="other",
            outcome="failed",
            evidence=(),
        )
        task = {"id": "11111111", "context_handoff": {"scope": asdict(scope)}}
        with (
            patch(
                "ai_agent.context_validation.decode_dependency",
                return_value=dependency,
            ),
            patch("ai_agent.context_store.ContextStore") as store,
        ):
            store.return_value.get.return_value = record
            with self.assertRaisesRegex(ValueError, "ownership"):
                self.pulls.archived_attempt(task)

    def test_pull_facts_keeps_identity_and_head_checks(self) -> None:
        head = "a" * 40
        url = "https://github.com/ramunl/repo/pull/7"
        pull = {
            "number": 7,
            "html_url": url,
            "state": "closed",
            "draft": False,
            "merged": True,
            "merged_at": "2026-10-08T10:00:00Z",
            "merge_commit_sha": "c" * 40,
            "base": {"repo": {"full_name": "ramunl/repo"}},
            "head": {"sha": head, "repo": {"full_name": "ramunl/repo"}},
        }
        with patch.object(self.pulls, "github_request", return_value=pull):
            facts = self.pulls.pull_facts("ramunl/repo", url)
        self.assertTrue(facts["merged"])
        self.assertEqual(facts["state"], "closed")
        self.assertEqual(facts["head"], head)
        with patch.object(self.pulls, "github_request", return_value=pull):
            self.assertIs(
                self.pulls.verified_pull("ramunl/repo", url, head)["merged"], True
            )
        pull["head"]["sha"] = "b" * 40
        with (
            patch.object(self.pulls, "github_request", return_value=pull),
            self.assertRaisesRegex(ValueError, "identity"),
        ):
            self.pulls.verified_pull("ramunl/repo", url, head)

    def test_classify_head_provenance(self) -> None:
        evidence = importlib.import_module("ai_agent.task_pull_evidence")
        self.assertIs(
            evidence.classify_head("/nonexistent", {"a" * 40}, "a" * 40),
            HeadProvenance.EXACT,
        )
        self.assertIs(
            evidence.classify_head("/nonexistent", {"a" * 40}, "b" * 40),
            HeadProvenance.UNVERIFIABLE,
        )
