# rules.py

Loads coding rules from the [ai-rules](https://github.com/ramunl/ai-rules)
repo and formats them for injection into Claude planning prompts and the Codex
bugfix prompt. This is what makes the coding agent actually follow the rules
managed by ai-pm-agent.

## Behaviour

- `sync_rules()` - clones ai-rules if missing, otherwise `git pull`. Never
  raises; on failure it logs a warning and falls back to any cached copy.
- `load_rules_text()` - concatenates `global/*.md` plus
  `projects/<RULES_PROJECT_NAME>/*.md`. Returns `""` when rules are disabled
  or unavailable.
- `rules_prompt_block()` - wraps the rules in a "MANDATORY CODING RULES"
  header ready to embed in a prompt, or `""` when there are none.

## Design decision: fail open

If the rules repo can't be reached, planning proceeds **without** rules rather
than failing. Rules improve output; they are not a hard dependency of being
able to work. Every failure path returns an empty string, never an exception.
