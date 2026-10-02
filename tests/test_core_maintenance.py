"""Core pin updates preserve detached deployment checkouts."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.bot_fixtures import TelegramTestCase


def git(repo: Path, *arguments: str) -> str:
    """Run isolated local Git fixtures without network or user configuration."""
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class IsolatedCoreTests(TelegramTestCase):
    def test_bump_pushes_main_without_changing_live_checkout(self) -> None:
        from ai_agent.core_maintenance import bump_core_isolated

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core"
            core.mkdir()
            git(core, "init", "-b", "main")
            git(core, "config", "user.name", "Test")
            git(core, "config", "user.email", "test@example.test")
            (core / "VERSION").write_text("1")
            git(core, "add", ".")
            git(core, "commit", "-m", "old core")
            git(core, "tag", "v1.0")
            old_core = git(core, "rev-parse", "HEAD")
            (core / "VERSION").write_text("2")
            git(core, "commit", "-am", "new core")
            git(core, "tag", "v2.0")
            new_core = git(core, "rev-parse", "HEAD")
            remote = root / "remote.git"
            remote.mkdir()
            git(remote, "init", "--bare", "-b", "main")
            live = root / "live"
            live.mkdir()
            git(live, "init", "-b", "main")
            git(live, "config", "user.name", "Test")
            git(live, "config", "user.email", "test@example.test")
            git(
                live,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(core),
                "ai_agent_common",
            )
            git(live / "ai_agent_common", "checkout", old_core)
            git(live, "add", ".")
            git(live, "commit", "-m", "old pin")
            git(live, "remote", "add", "origin", str(remote))
            git(live, "push", "-u", "origin", "main")
            live_head = git(live, "rev-parse", "HEAD")
            git(live, "checkout", "--detach", live_head)
            with patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "protocol.file.allow",
                    "GIT_CONFIG_VALUE_0": "always",
                },
            ):
                changed, message = bump_core_isolated(live)
            self.assertTrue(changed, message)
            self.assertEqual(git(live, "rev-parse", "HEAD"), live_head)
            self.assertEqual(
                git(live / "ai_agent_common", "rev-parse", "HEAD"), old_core
            )
            self.assertEqual(git(live, "status", "--porcelain"), "")
            self.assertEqual(git(remote, "rev-parse", "main:ai_agent_common"), new_core)
