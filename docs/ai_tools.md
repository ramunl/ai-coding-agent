# ai_tools.py — common model interface across AI tools

The agent uses more than one AI tool (Claude API, Codex CLI, Claude Code CLI)
and may add more. This module gives them a uniform interface for the one thing
worth unifying: the model dial.

## Deliberately narrow scope

Only model selection is unified. Auth, keys, and how each tool is invoked stay
tool-specific — unifying those would mean building a provider console in a
chat bot, which is not worth it.

## Honest capability, not fake uniformity

The tools genuinely differ, and the interface represents that rather than
hiding it:

| Tool | Model source | manageable | verify |
|---|---|---|---|
| claude | ANTHROPIC_MODEL (this agent) | yes | live API probe |
| codex | Codex CLI's own login/config | no | n/a |
| claude-code | Claude CLI's own login/config | no | n/a |

`claude` is fully controllable: set + live-verify (built on model_manager).
The CLIs are read-only here — their model lives in their own config, adjacent
to the auth concern that is out of scope. `/model` shows them and points the
user at the right place instead of pretending it can set them.

## Interface

```
AITool
  .name
  .manageable            # can this agent change it?
  .current_model()
  .set_model(name)       # manageable tools only
  .verify(name)          # manageable tools only
  .info() -> ModelInfo   # tool, model, manageable, note
```

## Adding a future tool

Write one AITool subclass, register it in _TOOLS. If the new tool exposes a
model your agent can set and check, make it manageable; otherwise subclass
CliTool for a read-only entry. The /model command, registry, and existing
tests do not change.

## /model command grammar

```
/model                     list every tool and its model
/model <tool>              show one tool; live-verify if manageable
/model <tool> set <name>   switch a manageable tool (verified before saving)
```

Switching a manageable tool verifies the candidate against the live API before
writing it to the env file, then restarts — a bad model never reaches the boot
file. Read-only tools reject `set` with a message naming where their model is
configured.

## Read-only authentication observations

`tool_auth_providers.py` checks the bot process's runtime environment (same
user, HOME, PATH and provider configuration as its CLIs). No credentials are
copied from Ops evidence or another user's home. `claude` remains the API tool;
`claude-code` remains the CLI tool.

- Codex: verify `codex login --help` advertises `status`, then run
  `codex login status`. Only recognized login lines count; stored API keys are
  `configured_unverified`, not validated credentials.
- Claude Code: verify `claude auth --help` advertises `status`, and its status
  help advertises `--json`, then run `claude auth status --json`. OAuth login
  reported by the CLI is `authenticated`; keys, helpers and third-party
  backends are `configured_unverified`.
- Claude API: inspect the import-time `ANTHROPIC_KEY` setting used by the bot.
  Present means `configured_unverified`; absent means `logged_out` for this
  API integration. No validation request is sent.

These are local status observations, not guarantees of server acceptance,
quota, or future access. Unknown output and unsupported interfaces never mean
logged out. CLI probes use only the documented status/help interfaces; they do
not invoke login, refresh, inference, logout, or credential helpers directly.
No credential files or JWTs are parsed. Probe output and exceptions are never
logged; diagnostics contain only a fixed message and a known tool ID.

Interface references: [Codex CLI](https://developers.openai.com/codex/cli/reference)
and [Claude Code CLI](https://code.claude.com/docs/en/cli-reference).
Neither status interface supplies a documented mandatory relogin deadline.
All currently implemented providers therefore publish null countdowns, even
when other output includes token expiry. See the
[snapshot contract](snapshot_publisher.py.md#authentication-contract).
