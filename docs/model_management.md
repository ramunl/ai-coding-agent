# Model management

The Claude model string is a changeable, expiring thing. Anthropic retires
pinned snapshots on a schedule; a string that works today returns HTTP 404
later, breaking every /plan, /implement, and /bugfix at once. This is what
happened with the old default claude-sonnet-4-20250514 (retired 2026-06-15).

Three layers address it.

## 1. Current default

`config.py` defaults ANTHROPIC_MODEL to a current model. Override it per
machine in the env file; the default is only a fallback.

## 2. Retired models explain themselves (model_errors.py)

A 404 not_found_error on the model is detected and rewritten into an
actionable message naming the model and the fix. This applies to:
- planner API calls (via a wrapper around client.messages.create)
- the /limits command output

So a retirement presents as an instruction, not a cryptic 404 — the same
principle as making the credit-balance and rules-sync failures legible.

## 3. /model command (model_manager.py)

- `/model` — show every AI tool and its model
- `/model <tool>` — show one tool and live-probe it when manageable
- `/model <tool> set <name>` — switch a manageable tool

### The guardrail

Switching persists ANTHROPIC_MODEL to the boot env file and restarts. A bad
value there would crash every start. So `/model <tool> set <name>` **verifies
the candidate against the live API first** and only writes it if it responds.
An invalid or retired string is rejected at the command and never reaches disk.
Model names are also validated against a strict charset before any use, so
nothing shell-unsafe can be written to the env file.

Restart is detached (same mechanism as /pull), so the bot replies before the
process dies. Confirm the switch with /model after a few seconds.

## Note: Codex and Claude Code differ

This risk is specific to API calls with pinned model strings. Claude Code and
consumer Claude.ai select models automatically and are not affected the same
way. Only the API-based planner needs this handling.
