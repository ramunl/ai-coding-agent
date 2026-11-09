# Provider registry

`ai_agent/provider_registry.py` replaces the old model-only registry in
`ai_tools.py` with one contract per AI provider. Each provider is one module in
`ai_agent/providers/` and declares the same capability surface. The registry
discovers those modules and answers capability questions with a strict rule:
**a capability a provider does not declare is `unsupported`, never invented.**

## Capability surface

| Capability | Meaning | Type |
|---|---|---|
| `roles` | Which roles this provider serves | `tuple[str, ...]` of `planner` / `executor` |
| `adapters` | Planner/executor entry points | `dict[str, Callable]` keyed by role |
| `model` | The `/model` dial | `ModelControl` |
| `auth` | Runtime credential observation | async `() -> Reading` |
| `limits` | Last recorded quota reading | `() -> dict` |
| `errors` | Failure recognition + explanation | `ErrorAdapter` |
| `images` | Task images supported | `bool` |
| `structured_output` | Schema-enforced output supported | `bool` |
| `usage` | Token counting | `Callable[..., int | None]` |
| `enabled` | Offered in production | `bool` |

`name` is the registry key (also a module attribute); it is not a capability.
`capability(provider, name)` returns the declared value, or the string
`"unsupported"` when the attribute is absent or the name is outside
`CAPABILITIES`. A declared `False` boolean is returned as `False`, not
`unsupported` — only *undeclared* capabilities are unsupported.

`ModelControl` bundles `manageable`, `note`, `current()`, `args()` (model CLI
arguments), `verify()`, `list_models()` and `set()` (`None` for read-only
tools). `ErrorAdapter` bundles `classify()` and `message()`.

## Migration map

| Before | After |
|---|---|
| `ai_tools.ClaudeApiTool` + `model_manager` | `providers/claude.py` (API) |
| `ai_tools.CodexTool` + `codex_models` | `providers/codex.py` |
| `ai_tools.CliTool("claude-code")` | `providers/claude_code.py` (CLI) |
| — | `providers/opencode.py` (executor placeholder, disabled) |
| — | `providers/deepseek.py` (native API planner) |

`model_manager.py` and `codex_models.py` remain as the tested implementation
the provider modules wrap; `planning_provider.py`, `workflow.py`,
`provider_limits.py`, `token_usage.py`, `tool_auth_providers.py` and the
snapshot publisher keep their existing imports and dispatch unchanged.

## Compatibility rules

- `ai_tools.py` is a thin shim. Its `AITool`/`ModelInfo` surface, `known_tools`,
  `get_tool`, `all_info`, and the `model_manager`/`codex_models` module
  attributes are unchanged, so `/model`, the dashboard snapshot and existing
  tests keep working.
- **Aliases resolve exactly as before**: `claude` is the Anthropic API tool;
  `claude-code` is the Claude Code CLI tool. The registry never merges them.
- **`codex_prompt` is untouched.** It stays a persisted task/plan field
  (`tasks.py` `RUN_KEYS`) and the registry does not read or rename it.
- **Disabled providers declare only `name` and `enabled=False`.** OpenCode
  capabilities are unverified and resolve to `unsupported`. DeepSeek is an
  enabled `planner`-only provider: `images=False`, `structured_output=True`,
  a read-only model dial and non-inference model/balance discovery. It is not
  an executor and is gated off for the autonomous worker until the owner sets
  `AUTONOMOUS_DEEPSEEK_ENABLED`.
- The registry imports nothing from `anthropic`/`telegram` and reads `config`
  once at import like the rest of the agent; provider adapters import heavy
  dispatch modules lazily inside their functions to stay import-safe for tests.

## Discovery

`discover()` imports only the modules listed in the owner-approved manifest
(`ai_agent/providers/manifest.json`) and registers each under its `name`.
Unlisted modules are never imported; a missing manifest falls back to the
built-in `PROVIDER_MODULES` tuple, and a malformed manifest fails closed.
`get_provider` is case-insensitive and returns `None` for an unknown tool;
`enabled_providers()` returns only providers with `enabled` truthy. `reset()`
clears the registry (tests/reloads only). See `docs/provider-extension.md` for
the manifest schema and the one-module provider contract.
