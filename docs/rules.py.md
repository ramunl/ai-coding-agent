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

## Config

| Env var | Default | Meaning |
|---|---|---|
| RULES_ENABLED | true | Master switch for rule injection |
| RULES_REPO_PATH | /opt/ai-rules | Local clone location |
| RULES_REPO_URL | git@github.com:ramunl/ai-rules.git | Remote to clone/pull |
| RULES_PROJECT_NAME | channel-cast | Which projects/<name>/ folder to include |

## Injection points (planner.py)

- `plan_feature` - feature planning prompt
- `revise_feature_plan` - plan revision prompt
- `build_bugfix_prompt` - the prompt Codex executes for bugfixes

`assess_bugfix_report` is intentionally left rule-free: it only decides whether
enough info exists to start, so coding rules are irrelevant there and would
just cost tokens.
