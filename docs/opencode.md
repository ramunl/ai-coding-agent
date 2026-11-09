# OpenCode: pinned, characterized, and disabled

OpenCode (npm `opencode-ai`) is characterized here as an **optional DeepSeek
executor**. It stays **disabled** in production: `ai_agent/providers/opencode.py`
keeps `enabled=False`, so the worker can never route an implementation run
through it and the dashboard snapshot reports it as unavailable. The provider
still *declares* the executor contract (roles, adapters, a read-only model dial,
auth, limits, errors, usage) so that, once the owner flips `enabled`, the
registry, worker, dashboard and usage accounting pick it up without a code
change. This document records the pinned build, the JSON-event contract, and the
peak-RAM/attachment evidence produced by `scripts/opencode_characterize.sh`.
Nothing here enables OpenCode.

Activation is gated on an owner trial decision. The trial's install route is
ai-ops-agent's VPS maintenance and stays a **reversible, disposable** action in
a throwaway checkout — never a production change. DS07/DS12 prerequisites live
outside this repo; this work must not, and does not, block the native planner,
dashboard, or accounting milestones.

## Reviewed pin and Node requirement

The exact npm release is pinned and verified before any disposable run. The
checked-in value is recorded in two places that must stay in sync:

- `ai_agent/providers/opencode.py` — `PINNED_VERSION` and `NODE_REQUIREMENT`
- `tests/fixtures/opencode/*.json` — each fixture's `pinned_version` and
  `node_requirement`

`tests/test_opencode_contract.py` asserts the two stay equal.

| Field | Value | Status |
|---|---|---|
| Pinned version | *(empty until reviewed)* | The `verify` step refuses to run without an exact `OPENCODE_PIN` |
| Node requirement | `>=22` | Candidate; re-verified against `npm view opencode-ai@<pin> engines.node` |

Do **not** assume Node 20: the VPS Node/runtime may reject the current release,
in which case `verify` fails and a pinned older version (or a Node upgrade via
Ops) is chosen before anything runs. The pin is never silently substituted.

## Event contract

`opencode run` is captured as one JSON object per line; each event is a plain
object with a `type` and a `properties` object. The recognized types:

| `type` | `properties` | Meaning |
|---|---|---|
| `message` | `role`, `text` | assistant text |
| `result` | `text` | final assistant result (clean stop) |
| `usage` | `input_tokens`, `output_tokens`, `total_tokens`, `reasoning_tokens?` | token accounting |
| `error` | `message`, `code?` | provider/CLI failure |
| `cancelled` | `reason?` | user or limit cancellation |

Anything not in this table is ignored; a stray log line never breaks parsing.

## Classification contract

`opencode.classify_run(events, exit_code)` reduces a run to the four documented
categories:

| Category | Rule |
|---|---|
| exit | the process exit code (`0` success, non-zero failure) |
| error | the first `error` event's message; a non-zero exit with no error event becomes `opencode exited with status N` |
| usage | the first usable `usage` event, normalized (reasoning tokens kept separate, never totaled) |
| cancel | any `cancelled` event; suppresses the synthetic exit error |

`opencode.error_kind(code, message)` classifies an error event as `auth`,
`funds`, `transient`, or `execution`; `ai_agent/opencode_runner.py` consumes this
classification to reduce a stream to a bounded outcome, and
`ai_agent/workflow.py` enforces the result-event contract (a zero exit alone is
a false success). The worker's own failure vocabulary stays in
`ai_agent/provider_availability.py`.

## Executor wiring

The provider is owner-gated in two independent steps:

- `ai_agent/providers/opencode.py` — `enabled=False` today. Flipping it to
  `True` (after a reviewed pin) exposes the executor role, so
  `tools_for_role("executor")` and the registry pick it up.
- `ai_agent/config.py` — `AUTONOMOUS_OPENCODE_ENABLED` (default `false`) gates
  the worker's *autonomous* use through `ai_agent/worker.py`, matching the
  `AUTONOMOUS_DEEPSEEK_ENABLED` gate. Manual runs are unaffected.

`ai_agent/opencode_runner.py` runs `opencode run --format json --model
<OPENCODE_MODEL>` through the shared cancellation-aware `grouped_run`, bounds
captured output to `OPENCODE_MAX_OUTPUT_CHARS` (tail kept, truncation marked),
and reduces the stream to an `OpenCodeRun`. `workflow.run_implementation_agent`
then records usage/cost and requires a `result` event plus repository changes: a
zero exit without a `result` is rejected as a false success. Usage accounting
adds the `opencode` runner key (provider `deepseek`); saved stores stay
backward-compatible.

## Six scenarios captured

`scripts/opencode_characterize.sh` runs, with a fresh session and explicit
`--model deepseek/deepseek-chat`, these six and writes a sanitized fixture per
scenario under `tests/fixtures/opencode/`:

1. `success` — a clean run ending in a `result` event with `usage` recorded.
2. `error` — a provider error (`error` event) classified `transient`/`execution`.
3. `auth` — a rejected key (`error` event) classified `auth`.
4. `funds` — no spendable balance (`error` event) classified `funds`.
5. `usage` — a run whose `usage` event is asserted against the recorded totals.
6. `cancel` — a run interrupted after start (`cancelled` event).

`success.json` is the checked-in canonical example; the script refreshes the
rest when run. Auth/funds fixtures require real DeepSeek API error responses,
so the sanitizer strips every key and balance before anything is committed.

## Sanitization

The script sanitizes every captured event through `opencode.sanitize_event`
before writing a fixture: credential-like keys (`api_key`, `authorization`,
`secret`, `password`, `balance`, `credit`, …) are dropped and `sk-…`/`Bearer …`
values are replaced with `<redacted>`. Usage-token keys (`input_tokens`, …) are
deliberately kept. `test_opencode_contract.py` asserts re-sanitizing the
checked-in fixture changes nothing, so no key or balance data can leak.

## Peak RAM and attachment capability

Peak RAM is measured per scenario with `/usr/bin/time -v` (Maximum resident set
size) while the pinned build runs in the disposable checkout. Attachment
capability (whether OpenCode reads image attachments) is recorded per run.

| Metric | Measured value | Decision |
|---|---|---|
| Peak RSS (success) | *pending a run on the VPS* | — |
| Peak RSS (worst scenario) | *pending* | — |
| Attachments | *pending* | — |
| 1 GiB resize | — | **Deferred.** The service itself peaks ~280 MB, leaving headroom; no resize is requested until the measured worst-case RSS plus margin justifies it. |

The watch-out is that the 1 GiB VPS may OOM during a run; peak RAM is captured
before any promise of "no resize" is made.

## Disposability and containment

After every scenario the script confirms:

- no orphan children remain (`pgrep` for the pinned binary's command line), and
- the run's working directory was the disposable checkout, so outside writes are
  denied and the production checkout is untouched.

The disposable checkout and installed build are removed on exit. A failed run
never mutates the production repository or service state.

## Running it (Ops-owned)

```sh
# 1. Review and record the exact release to pin (trial decision).
OPENCODE_TRIAL_APPROVED=1 OPENCODE_PIN=<reviewed-version> \
  scripts/opencode_characterize.sh verify

# 2. Capture fixtures, RAM and attachment evidence in a disposable checkout.
OPENCODE_TRIAL_APPROVED=1 OPENCODE_PIN=<reviewed-version> \
  scripts/opencode_characterize.sh capture
```

`verify` checks `npm view opencode-ai@<pin>` provenance/`engines.node` against
the VPS Node runtime and refuses to continue on mismatch. `capture` runs the six
scenarios, sanitizes and writes fixtures, reports peak RAM, and cleans up.
Neither step enables OpenCode; enabling it remains a separate owner decision
outside this script.
