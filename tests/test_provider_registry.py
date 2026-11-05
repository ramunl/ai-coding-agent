"""Provider registry discovery: unknown tools and capabilities are unsupported."""

from types import SimpleNamespace

from ai_agent import provider_registry


def test_discovery_registers_known_providers() -> None:
    known = provider_registry.known_providers()
    assert {"claude", "codex", "claude-code", "opencode", "deepseek"} <= set(known)


def test_enabled_providers_match_production_defaults() -> None:
    enabled = {provider.name for provider in provider_registry.enabled_providers()}
    assert enabled == {"claude", "codex", "claude-code"}


def test_unknown_tool_is_unsupported() -> None:
    assert provider_registry.get_provider("gemini") is None


def test_unknown_capability_is_unsupported() -> None:
    fake = SimpleNamespace(name="fake", enabled=False)
    assert provider_registry.capability(fake, "images") == provider_registry.UNSUPPORTED
    assert provider_registry.capability(fake, "made_up") == provider_registry.UNSUPPORTED
    assert provider_registry.capability(fake, "enabled") is False


def test_registered_fake_provider_resolves_case_insensitively() -> None:
    provider_registry.reset()
    fake = SimpleNamespace(name="fake-tool", enabled=False)
    provider_registry.register("fake-tool", fake)
    try:
        assert provider_registry.get_provider("FAKE-TOOL") is fake
        assert (
            provider_registry.capability(fake, "roles")
            == provider_registry.UNSUPPORTED
        )
    finally:
        provider_registry.reset()
