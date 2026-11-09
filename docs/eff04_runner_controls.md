# EFF04 — Bounded repository reads and verification output

This change bounds what reaches the model's context from two directions the bot
actually controls: the **flags it passes to the implementer CLI** and the
**verification/failure text it writes into prompts**. It deliberately does *not*
claim to bound the tool results the model reads inside the CLI — the bot cannot
post-process those, so there bounding is limited to runner-supported
flags/settings plus a prompt contract.

## What the bot can and cannot bound

- The bot builds the implementer command and the prompt. It can bound those.
- The bot feeds CI/verification summaries into repair prompts. It can bound
  those (`ai_agent/verification_summary.py`).
- The model reads tool results (repository files, test output) *inside* the CLI
  on its own. The bot cannot truncate those; only a runner-provided control or
  the prompt contract applies there.

## Installed runner versions (Ops-reported, read-only)

The `ai-diagnostics` read-only evidence reported on 2026-11-09:

- **Claude Code** (npm `@anthropic-ai/claude-code`): `2.1.319` reported as the
  latest release (the `--version` line was not captured verbatim).
- **Codex** (npm `@openai/codex`): `codex-cli 0.166.0`.

Probe commands to re-verify on the VPS (do not run them from a coding run):

```sh
claude --version && claude --help
codex --version && codex exec --help
```

## Characterized runner controls and the gap

The controls below were characterized from the CLI help/settings surface. None
of them bounds the *size* of a tool result (a file read or a command's output)
that the model sees, so none is enabled here.

| Runner | Official control | What it actually bounds | Verdict |
| --- | --- | --- | --- |
| claude-code | `--max-turns <n>` | number of agentic turns, not read/output size | not a context/output bound; not enabled |
| claude-code | `--output-format` / `--input-format` | envelope shape, not size | not a bound; not enabled |
| claude-code | settings `MAX_THINKING_TOKENS`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | response/turn token limits, not tool-result size | not a read bound; not enabled |
| codex | `--sandbox` / `-s` | filesystem write policy | already used; not a context bound |
| codex | `--model`, `--json` | model and output envelope | not a context bound |

**Gap:** neither installed release exposes a documented flag or setting that
truncates repository reads or verification output entering the model's context.
Bounding those is therefore limited to (a) the prompt contract below, and (b)
the bot-side summarizer for the text the bot itself injects.

## Code: version-gated flags (`ai_agent/runner_controls.py`)

Mirrors `ai_agent/denial_loop.py`:

- `supports_context_control(runner, args, version)` returns
  `(supported, reason)` and is conservative: unknown runner, unparseable
  version, an unverified release (`MIN_VERSIONS[runner] is None`) and
  already-present flags all return `False` with an explicit reason.
- `context_control_args(runner, args)` adds candidate flags only when the
  version is verified, and **logs and omits** them otherwise.

`ai_agent/ai_tools.py` (`ClaudeApiTool.command`, `CodexTool.command`) and
`ai_agent/workflow.py` (`claude_context_control_args`,
`codex_context_control_args`) route through this gate. The candidate flag lists
(`_CONTEXT_CONTROL_FLAGS`) and the floors (`MIN_VERSIONS`) are **empty/`None`**,
so today the gate adds nothing and never disturbs `safe_claude_code_args` or
`claude_allowed_tools_args`.

**Not implemented (on purpose):** no context/output-control flag is enabled,
because no release has been verified to expose one. To close the gap: verify a
specific release's flag on the VPS, set the floor in `MIN_VERSIONS`, fill the
candidate flags, and update this document.

## Prompt contract (`ai_agent/ops_context.py`)

`OUTCOME_INSTRUCTIONS` (the single contract appended to every implementation and
repair prompt) now also asks the implementer to prefer targeted ranges over
whole-file reads, avoid re-reading large files, and report a short redacted
summary of test or lint output instead of pasting full logs. It lives with the
existing outcome JSON contract so the prompt stays one contract, not a growing
string of side paragraphs.

## Bot-side summarizer (`ai_agent/verification_summary.py`)

Deterministic, secret-free reduction of verification output:

- `summarize_verification(...)` keeps the identity, exit code, line/char counts,
  a **redacted** head/tail and an explicit truncation marker; the middle is
  elided. Full redacted diagnostics are retained in a private local artifact
  (`artifact_dir`, default `VERIFICATION_ARTIFACT_DIR` or
  `<USAGE_DETAILS_PATH>/verification`) and never reach a prompt.
- `file_excerpts(...)` reuses `repository_context.repository_excerpt` (and
  `_excerpt_block`), so targeted file ranges in verification summaries and
  repair prompts keep the pinned, symlink-safe `repository_read` path; oversize
  files are omitted fail-closed.
- `ai_agent/ci.py` `build_failure_context` now returns the bounded summary; raw
  CI job/comment logs never reach the repair prompt or a snapshot.

## Before/after harness (`ai_agent/verification_harness.py`)

- `measure_fixture(...)` measures one representative fixture through the
  summarizer and reports input vs summary `output_chars` (reusing
  `usage_details.bounded_output_chars`) plus whether key marker lines survived
  (the completion-quality proxy).
- `compare_usage(before, after)` aggregates `usage_details` token/cost/
  output_chars over two sets of records and returns regressions in a separate
  list.

Numbers must be re-measured on the same fixtures for every comparison; earlier
observations are never cited as fresh.
