"""A test-only third provider module proving the generic provider contract.

Loaded only through ``tests/fake_manifest.json`` via the
``AI_AGENT_PROVIDER_MANIFEST`` override, never through the production manifest.
It declares the full capability surface — roles, adapters (planner/executor plus
the generic command/image_run sub-adapters), model catalog, auth, limits,
errors, images, structured output and usage — so the contract tests can prove a
new provider routes through snapshot, planner, executor, auth, limits, errors,
usage and images without any central dispatch edit.
"""

from __future__ import annotations

from ai_agent.provider_registry import ErrorAdapter, ModelControl
from ai_agent.tool_auth_providers import Reading

name = "fake-provider"
enabled = True
roles = ("planner", "executor")
images = True
structured_output = True

MODEL_DEFAULT = "fake-model-v1"
NOTE = "Fake provider model, fixed for the provider-extension contract test."


def _current_model() -> str:
    """Return the configured fake model."""
    return MODEL_DEFAULT


def _verify(model: str) -> tuple[bool, str]:
    """Verify a model id against the fake catalog without inference."""
    if model == MODEL_DEFAULT:
        return True, ""
    return False, f"Unknown fake model: {model}"


def _list_models() -> tuple[bool, list[dict[str, str]] | str]:
    """List the fake model catalog without running inference."""
    return True, [{"id": MODEL_DEFAULT, "display_name": "Fake Model"}]


def _set_model(model: str) -> None:
    """No-op model writer so the dial is complete but side-effect free."""


model = ModelControl(
    manageable=True,
    note=NOTE,
    current=_current_model,
    args=lambda: [],
    verify=_verify,
    list_models=_list_models,
    set=_set_model,
)


async def auth() -> Reading:
    """Observe the fake credential state without running a subprocess."""
    return Reading("authenticated", "oauth")


def limits() -> dict:
    """Return a marker reading proving the registry dispatched here."""
    return {"status": "ok", "windows": []}


def _classify(error: BaseException) -> bool:
    """Whether an exception is the fake provider's own capacity failure."""
    return isinstance(error, RuntimeError) and "fake-capacity" in str(error)


def _message(error: BaseException) -> str:
    """A secret-free explanation for a recognized fake failure."""
    return "Fake provider is at capacity."


errors = ErrorAdapter(classify=_classify, message=_message)


def usage(reported: object) -> int | None:
    """Return a marker token count proving the registry dispatched here."""
    return 42


def _planner(prompt: str, schema_name: str, max_tokens: int) -> str:
    """Return a marker proving the registry dispatched here."""
    return f"fake-plan:{schema_name}"


def _executor(prompt: str) -> object:
    """A role adapter marker; the generic tool never calls it directly."""
    return f"fake-exec:{prompt}"


def _command(prompt: str) -> list[str]:
    """The implementation command builder for the generic ProviderTool."""
    return ["fake-provider", "run", prompt]


def _image_run(
    command: list[str],
    run: object,
    *,
    images: list[tuple[str, bytes]],
    missing: int,
    **kwargs: object,
) -> object:
    """Return a marker proving the registry dispatched here."""
    return ("fake-image", len(images), missing)


adapters = {
    "planner": _planner,
    "executor": _executor,
    "command": _command,
    "image_run": _image_run,
}
