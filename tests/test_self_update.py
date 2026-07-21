import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", *args],
        capture_output=True,
        check=True,
        cwd=cwd,
        text=True,
    )
    return result.stdout.strip()


class SelfUpdateTests(unittest.TestCase):
    """Exercises the update flow against real git repos on disk.

    Layout: `origin` is a bare repo playing GitHub; `local` plays the
    server checkout in /opt. AGENT_DIR is patched onto `local`.
    """

    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-secret"

        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.local = self.tmp / "local"

        seed = self.tmp / "seed"
        seed.mkdir()
        (seed / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(seed, "init", "-q", "-b", "main")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "init")
        _git(self.tmp, "clone", "-q", "--bare", str(seed), str(self.origin))
        _git(self.tmp, "clone", "-q", str(self.origin), str(self.local))

        sys.modules.pop("ai_agent.self_update", None)
        self.self_update = importlib.import_module("ai_agent.self_update")
        self.dir_patch = patch.object(self.self_update, "AGENT_DIR", self.local)
        self.dir_patch.start()

    def tearDown(self) -> None:
        self.dir_patch.stop()
        os.environ.clear()
        os.environ.update(self.previous_env)
        sys.modules.pop("ai_agent.self_update", None)

    def _push_remote_commit(self, content: str, message: str) -> str:
        """Simulate someone pushing a fix to GitHub."""
        work = self.tmp / f"work-{message.replace(' ', '-')}"
        _git(self.tmp, "clone", "-q", str(self.origin), str(work))
        (work / "module.py").write_text(content, encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", message)
        _git(work, "push", "-q", "origin", "main")
        return _git(work, "rev-parse", "HEAD")

    # ---- flows ----

    def test_already_up_to_date(self) -> None:
        result = self.self_update.check_and_apply_update()
        self.assertTrue(result.ok)
        self.assertFalse(result.restart_pending)
        self.assertIn("Already up to date", result.message)

    def test_dirty_working_copy_refuses_update(self) -> None:
        self._push_remote_commit("VALUE = 2\n", "good fix")
        (self.local / "module.py").write_text("VALUE = 999  # local hack\n", encoding="utf-8")

        result = self.self_update.check_and_apply_update()

        self.assertFalse(result.ok)
        self.assertFalse(result.restart_pending)
        self.assertIn("local changes", result.message)
        # Local edit is preserved, not clobbered.
        self.assertIn("999", (self.local / "module.py").read_text())

    def test_failing_tests_roll_back_and_do_not_restart(self) -> None:
        before = _git(self.local, "rev-parse", "HEAD")
        bad = self._push_remote_commit("VALUE = broken\n", "bad fix")

        with patch.object(
            self.self_update, "_run_tests_on_new_code", return_value=(False, "1 failed")
        ):
            result = self.self_update.check_and_apply_update()

        self.assertFalse(result.ok)
        self.assertFalse(result.restart_pending)
        self.assertIn("REJECTED", result.message)
        self.assertIn("Rolled back", result.message)
        # Disk is back on the old commit, not the bad one.
        self.assertEqual(_git(self.local, "rev-parse", "HEAD"), before)
        self.assertNotEqual(_git(self.local, "rev-parse", "HEAD"), bad)
        self.assertIn("VALUE = 1", (self.local / "module.py").read_text())

    def test_passing_tests_apply_update_and_request_restart(self) -> None:
        good = self._push_remote_commit("VALUE = 2\n", "good fix")

        with patch.object(
            self.self_update, "_run_tests_on_new_code", return_value=(True, "all passed")
        ):
            result = self.self_update.check_and_apply_update()

        self.assertTrue(result.ok)
        self.assertTrue(result.restart_pending)
        self.assertIn("tests passed", result.message)
        self.assertIn("good fix", result.message)
        self.assertEqual(_git(self.local, "rev-parse", "HEAD"), good)

    def test_restart_is_never_requested_without_an_update(self) -> None:
        with patch.object(self.self_update, "schedule_restart") as restart:
            result = self.self_update.check_and_apply_update()
        self.assertFalse(result.restart_pending)
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
