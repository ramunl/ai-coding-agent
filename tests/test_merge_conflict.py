"""Validation tests for merge-conflict PR pinning."""

import importlib
import types
import unittest
from unittest.mock import patch

from ai_agent.merge_conflict import MergeConflictError, MergeConflictTarget

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
    def _validate(self, text: str, payload=None):
        module = importlib.import_module("ai_agent.merge_conflict")
        with (
            patch.object(
                module,
                "active_project",
                return_value=types.SimpleNamespace(github_repository=ACTIVE),
            ),
            patch.object(module, "ensure_github_configured"),
            patch.object(module, "github_request", return_value=payload),
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
                base_sha="base-sha",
                head_ref="fix/branch",
                head_sha="head-sha",
                merge_commit_sha="merge-sha",
            ),
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
