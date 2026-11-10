"""Repository test dependencies never come from the coding controller environment."""

import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent import provider_process, verification
from ai_agent import verification_environment as setup
from ai_agent.run_outcomes import NoCodeOutcomeError
from tests.test_verification import environment as environment


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements-dev.txt").write_text(
        "-r requirements.txt\npytest>=9\nPyYAML>=6\nruff==0.16.8\n"
    )
    (worktree / "requirements.txt").write_text("python-telegram-bot>=21\n")
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(provider_process, "grouped_run", execute)
    monkeypatch.setattr(setup, "WORKER_ISOLATION_ENABLED", False)
    return worktree, calls


def test_prepares_repository_requirements_and_removes_temporary_environment(prepared):
    worktree, calls = prepared
    with setup.python_environment(
        worktree, (("python", "-m", "pytest", "-q"),)
    ) as python:
        root = python.parent.parent
        assert root.parent == worktree.parent
        assert root != worktree
        assert root.exists()
        assert calls[0][0][1:3] == ["-m", "venv"]
        assert calls[1][0][0] == str(python)
        assert calls[1][0][-2:] == ["-r", "requirements-dev.txt"]
        assert calls[1][1]["cwd"] == worktree
        assert calls[2][0] == [str(python), "-m", "pip", "check"]
        assert all(
            kwargs["timeout"] == 600 and kwargs["max_output_chars"] == 8192
            for _, kwargs in calls
        )
        assert all(not kwargs["isolated"] for _, kwargs in calls)
    assert not root.exists()


@pytest.mark.parametrize(
    "failure", ["venv", "install", "consistency", "timeout", "missing"]
)
def test_setup_failure_is_actionable_and_cleans_up(prepared, monkeypatch, failure):
    worktree, calls = prepared
    roots = []
    count = 0

    def execute(args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            roots.append(Path(args[-1]))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 600)
        if failure == "missing":
            raise FileNotFoundError("python")
        code = 1 if count == {"venv": 1, "install": 2, "consistency": 3}[failure] else 0
        return SimpleNamespace(returncode=code, stdout="dependency error", stderr="")

    monkeypatch.setattr(provider_process, "grouped_run", execute)
    with pytest.raises(NoCodeOutcomeError, match="environment setup failed"):
        with setup.python_environment(worktree, (("python", "-m", "pytest"),)):
            pytest.fail("Failed dependency setup must not dispatch the test suite")
    assert roots and not roots[0].exists()


def test_cleanup_after_test_failure_or_cancellation(prepared):
    worktree, _ = prepared
    with pytest.raises(RuntimeError, match="cancelled"):
        with setup.python_environment(worktree, (("ruff", "check"),)) as python:
            root = python.parent.parent
            raise RuntimeError("cancelled")
    assert not root.exists()


def test_js_only_profile_never_creates_or_installs_python(prepared):
    worktree, calls = prepared
    with setup.python_environment(worktree, (("npm", "test"),)) as python:
        assert python is None
    assert calls == []


def test_missing_manifest_blocks_instead_of_using_controller_dependencies(prepared):
    worktree, calls = prepared
    (worktree / "requirements-dev.txt").unlink()
    with pytest.raises(NoCodeOutcomeError, match="requirements-dev.txt is missing"):
        with setup.python_environment(worktree, (("ruff", "check"),)):
            pytest.fail("No controller-Python fallback is permitted")
    assert calls == []


def test_commands_use_only_the_prepared_interpreter():
    python = Path("/temporary/project-env/bin/python")
    assert setup.verification_argv(("python", "-m", "pytest", "-q"), python) == [
        str(python),
        "-m",
        "pytest",
        "-q",
    ]
    assert setup.verification_argv(("ruff", "check", "tests"), python) == [
        str(python),
        "-m",
        "ruff",
        "check",
        "tests",
    ]
    assert setup.verification_argv(("npm", "test"), None) == ["npm", "test"]
    with pytest.raises(RuntimeError, match="not prepared"):
        setup.verification_argv(("python", "-m", "pytest"), None)


def test_setup_and_checks_use_worker_identity(prepared, monkeypatch):
    worktree, calls = prepared
    monkeypatch.setattr(setup, "WORKER_ISOLATION_ENABLED", True)
    owners = []
    monkeypatch.setattr(setup, "_worker_owns", owners.append)
    with setup.python_environment(worktree, (("python", "-m", "pytest"),)) as python:
        assert owners == [python.parent.parent]
        assert all(kwargs["isolated"] for _, kwargs in calls)


def test_test_environment_strips_model_credentials_and_python_redirection(monkeypatch):
    monkeypatch.setattr(
        setup,
        "provider_env",
        lambda **kwargs: {
            "ANTHROPIC_API_KEY": "secret",
            "ANTHROPIC_AUTH_TOKEN": "secret",
            "PYTHONPATH": "/controller",
            "PYTHONHOME": "/controller",
            "PIP_TARGET": "/production",
            "PIP_PREFIX": "/production",
            "PIP_USER": "true",
            "PATH": "/usr/bin",
        },
    )
    python = Path("/temporary/test-env/bin/python")
    env = setup.command_environment(python)
    assert set(env) == {
        "PATH",
        "PYTHONNOUSERSITE",
        "PIP_REQUIRE_VIRTUALENV",
        "VIRTUAL_ENV",
    }
    assert env["VIRTUAL_ENV"] == str(python.parent.parent)
    assert env["PATH"].startswith(str(python.parent))


def test_fixed_verification_uses_prepared_python_for_ruff_and_pytest(environment):
    env = environment
    verification.run_verification.__wrapped__("ai-dashboard:baseline-v1")
    argv = [args for args, _ in env.calls]
    assert argv[0][:4] == [str(env.test_python), "-m", "ruff", "check"]
    assert argv[1][:4] == [str(env.test_python), "-m", "ruff", "format"]
    assert argv[2] == [str(env.test_python), "-m", "pytest", "-q"]
    assert argv[3] == ["npm", "test"]


def test_dependency_setup_failure_never_records_success(environment, monkeypatch):
    env = environment

    @contextmanager
    def failure(*args):
        raise NoCodeOutcomeError(
            "Could not install repository development requirements"
        )
        yield

    monkeypatch.setattr(verification, "python_environment", failure)
    with pytest.raises(NoCodeOutcomeError):
        verification.run_verification.__wrapped__("ai-dashboard:baseline-v1")
    assert not env.calls
    assert "verification" not in env.attempt.state.get("latest", {})
