"""Prepare shared-core pin changes without modifying a deployed checkout."""

from pathlib import Path
from tempfile import TemporaryDirectory

from ai_agent.shell import run
from ai_agent_common import bump_to_latest


def bump_core_isolated(repo: Path) -> tuple[bool, str]:
    """Push a core pin from a temporary main checkout, preserving the live HEAD."""
    git = ["git"]
    if repo.name == "ai-coding-agent":
        git.extend(
            [
                "-c",
                "core.sshCommand=ssh -i /root/.ssh/ai_agent_deploy "
                "-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new",
            ]
        )
    remote = run([*git, "remote", "get-url", "origin"], cwd=repo).output.strip()
    with TemporaryDirectory(prefix="ai-core-update-") as directory:
        checkout = Path(directory) / "repo"
        run(
            [
                *git,
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                str(repo),
                str(checkout),
            ],
            cwd=repo,
        )
        run([*git, "remote", "set-url", "origin", remote], cwd=checkout)
        if len(git) > 1:
            run(
                ["git", "config", "core.sshCommand", git[2].split("=", 1)[1]],
                cwd=checkout,
            )
        run([*git, "fetch", "origin", "main"], cwd=checkout)
        run([*git, "checkout", "-B", "main", "origin/main"], cwd=checkout)
        run([*git, "branch", "--set-upstream-to=origin/main", "main"], cwd=checkout)
        run([*git, "submodule", "update", "--init", "--recursive"], cwd=checkout)
        return bump_to_latest(checkout / "ai_agent_common", checkout, "ai_agent_common")
