"""Validation tests for merge-conflict PR pinning."""

import importlib
import types
import unittest
from unittest.mock import patch

from ai_agent.merge_conflict import MergeConflictError, MergeConflictTarget

BASE_TIP = "a" * 40
HEAD_TIP = "b" * 40

ACTIVE = "owner/repo"


def _payload(**overrides) -> dict:
    payload = {
        "number": 5,
        "html_url": f"https://github.com/{ACTIVE}/pull/5",
        "title": "Fix conflicts",
        "state": "open",
        "base": {"ref": "main", "sha": "base-sha", "repo": {"full_name": ACTIVE}},
        "head": {
            "ref": "fix/branch",
            "sha": "head-sha",
            "repo": {"full_name": ACTIVE},
        },
        "merge_commit_sha": "merge-sha",
    }
    payload.update(overrides)
    return payload


class MergeConflictTests(unittest.TestCase):
    def _validate(self, text: str, payload=None, branches=None):
        module = importlib.import_module("ai_agent.merge_conflict")

        def request(method, path):
            if path == f"/repos/{ACTIVE}/pulls/5":
                return payload
            ref = path.split("/git/ref/heads/", 1)[1]
            if branches is not None:
                return branches[ref]
            return {
                "ref": f"refs/heads/{ref}",
                "object": {
                    "type": "commit",
                    "sha": BASE_TIP if ref == "main" else HEAD_TIP,
                },
            }

        with (
            patch.object(
                module,
                "active_project",
                return_value=types.SimpleNamespace(github_repository=ACTIVE),
            ),
            patch.object(module, "ensure_github_configured"),
            patch.object(module, "github_request", side_effect=request),
        ):
            return module.validate_merge_conflict_pr(text)

    def test_rejects_text_without_a_url(self) -> None:
        with self.assertRaisesRegex(MergeConflictError, "pull request URL"):
            self._validate("please fix my conflicts")

    def test_rejects_non_pull_request_url(self) -> None:
        with self.assertRaisesRegex(MergeConflictError, "not a pull request URL"):
            self._validate(f"https://github.com/{ACTIVE}/issues/5")

    def test_rejects_ambiguous_multiple_urls(self) -> None:
        with self.assertRaisesRegex(MergeConflictError, "exactly one"):
            self._validate(
                f"https://github.com/{ACTIVE}/pull/5 https://github.com/{ACTIVE}/pull/6"
            )

    def test_rejects_wrong_repository_before_any_request(self) -> None:
        module = importlib.import_module("ai_agent.merge_conflict")
        with (
            patch.object(
                module,
                "active_project",
                return_value=types.SimpleNamespace(github_repository=ACTIVE),
            ),
            patch.object(module, "ensure_github_configured") as ensure,
            patch.object(module, "github_request") as request,
        ):
            with self.assertRaisesRegex(MergeConflictError, "not the active project"):
                module.validate_merge_conflict_pr(
                    "https://github.com/other/repo/pull/5"
                )
        ensure.assert_not_called()
        request.assert_not_called()

    def test_rejects_closed_pr(self) -> None:
        with self.assertRaisesRegex(MergeConflictError, "not open"):
            self._validate(
                f"https://github.com/{ACTIVE}/pull/5", _payload(state="closed")
            )

    def test_rejects_fork_by_head_repository(self) -> None:
        payload = _payload()
        payload["head"]["repo"]["full_name"] = "fork/repo"
        with self.assertRaisesRegex(MergeConflictError, "fork"):
            self._validate(f"https://github.com/{ACTIVE}/pull/5", payload)

    def test_rejects_cross_repository_base(self) -> None:
        payload = _payload()
        payload["base"]["repo"]["full_name"] = "other/repo"
        with self.assertRaisesRegex(MergeConflictError, "fork"):
            self._validate(f"https://github.com/{ACTIVE}/pull/5", payload)

    def test_accepts_open_same_repo_pr_with_pinned_refs_and_shas(self) -> None:
        target = self._validate(f"https://github.com/{ACTIVE}/pull/5", _payload())

        self.assertEqual(
            target,
            MergeConflictTarget(
                number=5,
                url=f"https://github.com/{ACTIVE}/pull/5",
                title="Fix conflicts",
                base_ref="main",
                base_sha=BASE_TIP,
                head_ref="fix/branch",
                head_sha=HEAD_TIP,
                merge_commit_sha="merge-sha",
            ),
        )

    def test_stale_pr_base_sha_does_not_pin_an_old_base(self):
        payload = _payload()
        # The PR response can retain the original base even while GitHub
        # calculates mergeability against a newer main branch.
        payload["base"]["sha"] = "1" * 40
        payload["mergeable"] = False
        target = self._validate(f"https://github.com/{ACTIVE}/pull/5", payload)
        self.assertEqual(target.base_sha, BASE_TIP)
        self.assertNotEqual(target.base_sha, payload["base"]["sha"])

    def test_revalidation_detects_live_base_move_with_unchanged_pr_payload(self):
        payload = _payload()
        pinned = self._validate(f"https://github.com/{ACTIVE}/pull/5", payload)
        branches = {
            "main": {
                "ref": "refs/heads/main",
                "object": {"type": "commit", "sha": "c" * 40},
            },
            "fix/branch": {
                "ref": "refs/heads/fix/branch",
                "object": {"type": "commit", "sha": HEAD_TIP},
            },
        }
        current = self._validate(
            f"https://github.com/{ACTIVE}/pull/5", payload, branches
        )
        self.assertNotEqual(pinned.base_sha, current.base_sha)
        self.assertEqual(pinned.head_sha, current.head_sha)

    def test_missing_or_wrong_live_ref_never_falls_back_to_pr_sha(self):
        for result in (
            [],
            {"ref": "refs/heads/main", "object": [1]},
            {},
            {"ref": "refs/heads/other", "object": {"type": "commit", "sha": BASE_TIP}},
            {"ref": "refs/heads/main", "object": {"type": "tag", "sha": BASE_TIP}},
            {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "invalid"}},
        ):
            with (
                self.subTest(result=result),
                self.assertRaisesRegex(MergeConflictError, "current branch tip"),
            ):
                self._validate(
                    f"https://github.com/{ACTIVE}/pull/5", _payload(), {"main": result}
                )

    def test_rejection_performs_no_git_write(self) -> None:
        module = importlib.import_module("ai_agent.merge_conflict")
        shell = importlib.import_module("ai_agent.shell")
        payload = _payload()
        payload["head"]["repo"]["full_name"] = "fork/repo"
        with (
            patch.object(
                module,
                "active_project",
                return_value=types.SimpleNamespace(github_repository=ACTIVE),
            ),
            patch.object(module, "ensure_github_configured"),
            patch.object(module, "github_request", return_value=payload),
            patch.object(shell, "run") as git_run,
        ):
            with self.assertRaises(MergeConflictError):
                module.validate_merge_conflict_pr(f"https://github.com/{ACTIVE}/pull/5")
        git_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
