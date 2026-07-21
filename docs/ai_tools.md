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
