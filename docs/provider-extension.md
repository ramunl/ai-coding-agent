# Adding a provider without editing shared routing

A new AI provider is **one module plus one manifest entry**. The module declares
the same capability surface as the built-in providers, and the owner-approved
manifest (`ai_agent/providers/manifest.json`) is the only allow-list that lets
the registry import it. Nothing in `planning_provider`, `workflow`,
`image_delivery`, `provider_limits`, `tool_auth`, `inbox`, the worker's tool
pickers, or the retry/fallback lists has to know the provider exists.

## The one-module contract

Put the provider in `ai_agent/providers/<name>.py` and declare the registry
surface (see `docs/provider_registry.md` for the full capability table):

- `name` — the registry key, e.g. `"my-provider"`.
- `enabled` — `bool`; `False` keeps the provider out of every role.
- `roles` — `tuple` of `"planner"` / `"executor"`.
- `model` — a `ModelControl` (read-only dials set `manageable=False` and
  `set=None`).
- `auth` — async `() -> Reading` from `ai_agent.tool_auth_providers`.
- `limits` — `() -> dict` returning the last recorded reading.
- `errors` — an `ErrorAdapter` whose `classify(error) -> bool` and
  `message(error) -> str` take **one** argument (the generic tool contract).
- `images` — `bool`.
- `structured_output` — `bool`.
- `usage` — `(reported) -> int | None` token counter.
- `adapters` — `dict` of role and executor sub-adapters:

  | Key | Signature | Used by |
  |---|---|---|
  | `planner` | `(prompt, schema_name, max_tokens) -> str` | `ProviderTool.planner` |
  | `executor` | `(prompt) -> object` | published as a wired role; not called by the generic tool |
  | `command` | `(prompt) -> list[str]` | `ProviderTool.command` (implementation command builder) |
  | `image_run` | `(command, run, *, images, missing, **kwargs) -> Any` | `ProviderTool.image_run` |

`ai_tools.install_provider_tools()` adapts any discovered provider into a
`ProviderTool` and registers it, idempotently and lazily. It **never replaces**
the hand-written built-in tools (`claude`, `codex`, `claude-code`, `deepseek`,
`opencode`), so their exact CLI arguments and ordering cannot regress. A
disabled provider is still registered but serves no role, so the worker and the
role pickers refuse it exactly like a gated built-in.

## The manifest

`ai_agent/providers/manifest.json` is owner-approved data read from the checkout
via `Path(__file__).with_name("providers") / "manifest.json"` (deployment is a
git checkout + restart, so no wheel `package-data` config is needed):

```json
{
  "providers": [
    {
      "module": "ai_agent.providers.my_provider",
      "package": "example-package",
      "pinned_version": "1.2.3",
      "gated": false
    }
  ]
}
```

- `module` — a **dotted** Python module path. Slashes, dashes, spaces and shell
  metacharacters are rejected, so a manifest entry can never import a filesystem
  path or an arbitrary module.
- `package`, `pinned_version`, `gated` — secret-free maintenance metadata
  published in the dashboard snapshot under each provider's `maintenance` block.
  The runtime gate is still the module's `enabled` flag, never this metadata.

Validation fails closed: a non-dotted path, a duplicate module, or a malformed
entry raises `ValueError` and no module is imported. Unlisted modules are never
imported. When the manifest file is missing, discovery falls back to the
built-in `PROVIDER_MODULES` tuple.

The test override is `AI_AGENT_PROVIDER_MANIFEST` (see
`tests/test_provider_extension.py`); production never reads it.

## What metadata alone cannot do

Manifest metadata and the generic `ProviderTool` adapt the *routing surface*
(roles, adapters, model, auth, limits, errors, images, usage). They do **not**
invent provider behaviour:

- **Wire-protocol parsers are still adapter work.** A provider that streams a
  new event format (for example the OpenCode JSON event stream) needs a real,
  characterized parser and an executor runner behind its `command`/`image_run`
  adapters. The registry never learns a CLI's output format from the manifest.
- **Security policy is still adapter work.** Sandboxing, permission modes,
  secret redaction and the worker's fallback/retry rules are written per runner
  in the existing modules, not derived from metadata.
- **Auth and limits are still adapter work.** `auth` and `limits` are the
  provider's own functions; the manifest only describes where they live.

In short: the manifest makes a provider *discoverable and routable*; the module
makes it *correct and safe*.
