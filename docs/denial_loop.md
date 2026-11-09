# Denial-loop stop: repeated identical permission denials

A provider loop — the model retrying the same denied tool call — spends money
without progress. This change consumes **structured** permission-denial events
from the runner, counts identical denials per attempt, and stops the paid run
through the existing supervised stop/recovery path once a configurable
threshold is reached. The blocked attempt is marked terminal, partial edits are
preserved to recovery refs, and the owner gets a precise, secret-free reason.

## Config

- `DENIAL_LOOP_MAX_PER_ATTEMPT` (default `3`): how many *identical* structured
  denials are tolerated inside one attempt before the run is blocked.
  `0` disables detection; a negative value fails configuration validation.

## Structured denial contract (what is consumed)

Detection never reads the model's prose or a quoted log line. A line is a
denial only when it is a JSON object carrying an explicit structured marker.

- **Claude Code** (`claude --output-format stream-json`): an object with
  `"type": "tool_use_result"` whose `"denial"` object has `"denied": true`.
  The `tool`, `operation` and `policy` fields are read from that structured
  object.
- **Codex** (`codex exec --json`): an object with
  `"type"` in `{"tool_denied", "permission_denied"}` and `"denied": true`.

The denial identity is `(attempt, runner, tool, operation fingerprint,
policy)`. The operation is **hashed**, never stored, so a denied command that
contains a secret cannot leak into the report. A retry or a policy change
produces a new identity and a fresh counter.

## What is *not* a denial

- A `result` event with `is_error` (a failing test) is not a denial.
- An ordinary tool error without an explicit `denied: true` marker is not a
  denial.
- Free-form text such as "permission denied" in a log line is ignored.

## Version gating

The runner is probed with `<runner> --version`. The structured markers above
are consumed only when the version parses to at least the documented floor and
the command already requests the structured envelope. Current floors live in
`ai_agent/denial_loop.py` (`MIN_VERSIONS`): Claude Code `2.0.0`, Codex
`0.40.0`.

Enabling stream-json for non-image Claude runs (or `--json` for Codex) changes
the output shape and can alter cost/behavior, so it is **not** done here. When
the command does not already request the structured envelope, detection is a
no-op and the gap is logged.

## Capability gap

If the installed `claude`/`codex` does not emit a structured denial event, or
its version cannot be verified, **no signal is consumed** and the loop is not
blocked by this mechanism. That gap is deliberate: inferring denial from text
or quoted logs would misfire on failing tests and ordinary tool errors.

To close the gap for a runner, verify on the VPS that the installed release
emits the structured event shape above (for example with a tiny print-mode
turn under `--output-format stream-json` or `--json`), update the `MIN_VERSIONS`
floor to the reviewed release, and then — as a separate, owner-approved change —
enable the structured output flag for implementation runs, accounting for the
cost/behavior change.

## Behavior on a blocker

When the threshold is reached inside `provider_process.grouped_run`:

1. The supervised stop flag is set on the task-bound provider run.
2. The child process group is killed via the existing `killpg` path; stderr is
   drained throughout so the child cannot deadlock the parent.
3. `DenialLoopBlockedError` (a `NoCodeOutcomeError` with status `blocked`)
   propagates, so `git_recovery` preserves partial edits to recovery refs before
   the branch is restored, and the attempt journal is marked blocked/terminal.
4. `bot/execution.py` surfaces the precise reason; no further paid runs happen
   for that attempt, and a retry starts a fresh attempt (and counter).
