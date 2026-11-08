#!/usr/bin/env bash
# Characterize OpenCode as an optional DeepSeek executor — and keep it disabled.
#
# This is the Ops-owned, reversible, disposable trial harness recorded in
# docs/opencode.md. It never enables OpenCode in production: it verifies the
# reviewed npm pin and Node requirement, installs that exact build into a
# throwaway checkout, runs the six documented scenarios, sanitizes and writes
# JSON-event fixtures under tests/fixtures/opencode/, measures peak RAM, and
# confirms no orphan children remain and the production checkout is untouched.
#
# Gate: the owner's trial decision. Without OPENCODE_TRIAL_APPROVED=1 and an
# exact OPENCODE_PIN, every subcommand refuses to run.
#
#   OPENCODE_TRIAL_APPROVED=1 OPENCODE_PIN=1.2.3 scripts/opencode_characterize.sh verify
#   OPENCODE_TRIAL_APPROVED=1 OPENCODE_PIN=1.2.3 scripts/opencode_characterize.sh capture
set -euo pipefail

OPENCODE_PIN="${OPENCODE_PIN:-}"
OPENCODE_NODE_REQ="${OPENCODE_NODE_REQ:->=22}"
OPENCODE_MODEL="${OPENCODE_MODEL:-deepseek/deepseek-chat}"
FIXTURES_DIR="${FIXTURES_DIR:-tests/fixtures/opencode}"

# DeepSeek access is read from the operator's environment and never persisted.
DEEPSEEK_KEY="${DEEPSEEK_KEY:-${DEEPSEEK_API_KEY:-}}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/opencode-characterize.XXXXXX")"
BIN=""
CLEANED=0

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

cleanup() {
  if [ "$CLEANED" -eq 0 ]; then
    CLEANED=1
    # Remove the disposable worktree, then the install/scratch directory.
    if [ -n "${CHECKOUT:-}" ] && git -C "$REPO_ROOT" worktree list --porcelain | grep -q "$CHECKOUT"; then
      git -C "$REPO_ROOT" worktree remove --force "$CHECKOUT" >/dev/null 2>&1 || true
    fi
    rm -rf "$WORK_ROOT"
  fi
}
trap cleanup EXIT INT TERM

require_approval() {
  [ "${OPENCODE_TRIAL_APPROVED:-}" = "1" ] || die "owner trial not approved (set OPENCODE_TRIAL_APPROVED=1)"
  [ -n "$OPENCODE_PIN" ] || die "OPENCODE_PIN is empty; review and record an exact npm version first"
}

verify_runtime() {
  command -v node >/dev/null 2>&1 || die "node not found on PATH"
  command -v npm >/dev/null 2>&1 || die "npm not found on PATH"
}

# Resolve the installed binary path from the pinned package's own "bin" entry so
# the invocation does not depend on npm's .bin symlinks or any install script.
install_pinned() {
  local install_dir="$1"
  verify_runtime
  log "installing opencode-ai@${OPENCODE_PIN} into ${install_dir} (disposable)"
  npm install --prefix "$install_dir" --ignore-scripts --no-audit --no-fund \
    "opencode-ai@${OPENCODE_PIN}" >/dev/null
  local rel
  rel="$(node -e "const p=require('${install_dir}/node_modules/opencode-ai/package.json');const b=p.bin;const r=typeof b==='string'?b:(b&&(b.opencode||b['opencode-ai']||Object.values(b)[0]));if(!r)process.exit(1);process.stdout.write(r)")"
  [ -n "$rel" ] || die "could not resolve opencode bin entry for ${OPENCODE_PIN}"
  BIN="${install_dir}/node_modules/opencode-ai/${rel}"
  [ -f "$BIN" ] || die "resolved bin not present: ${BIN}"
}

verify() {
  require_approval
  verify_runtime
  local meta node_engines integrity attest
  meta="$(npm view "opencode-ai@${OPENCODE_PIN}" --json 2>/dev/null || true)"
  [ -n "$meta" ] || die "npm could not resolve opencode-ai@${OPENCODE_PIN}"
  node_engines="$(node -e 'const p=JSON.parse(process.argv[1]);process.stdout.write((p.engines&&p.engines.node)||"")' "$meta")"
  integrity="$(node -e 'const p=JSON.parse(process.argv[1]);process.stdout.write((p.dist&&p.dist.integrity)||"")' "$meta")"
  attest="$(node -e 'const p=JSON.parse(process.argv[1]);const a=p.dist&&p.dist.attestations;process.stdout.write((a&&a.url)||"")' "$meta")"
  [ -n "$node_engines" ] || die "opencode-ai@${OPENCODE_PIN} declares no engines.node"
  [ -n "$integrity" ] || die "opencode-ai@${OPENCODE_PIN} has no dist.integrity (registry integrity missing)"
  [ -n "$attest" ] || log "warning: no dist.attestations for ${OPENCODE_PIN}; review provenance manually"

  local node_version node_major req_major
  node_version="$(node --version)"   # e.g. v22.11.0
  node_major="$(printf '%s' "$node_version" | sed -E 's/^v?([0-9]+).*/\1/')"
  # The lower bound is the first integer in the engines range (">=22", "^20", "20").
  req_major="$(printf '%s' "$node_engines" | grep -oE '[0-9]+' | head -n1)"
  [ -n "$req_major" ] || die "could not parse a Node major from engines.node: ${node_engines}"
  [ "$node_major" -ge "$req_major" ] || die "VPS Node ${node_version} is below engines.node ${node_engines}"

  log "verified opencode-ai@${OPENCODE_PIN}"
  log "  engines.node = ${node_engines} (candidate: ${OPENCODE_NODE_REQ})"
  log "  dist.integrity = ${integrity}"
  log "  dist.attestations = ${attest:-<none>}"
  log "  local node = ${node_version}"
  log "PASS: provenance and Node requirement verified. Record PINNED_VERSION and"
  log "NODE_REQUIREMENT in ai_agent/providers/opencode.py and docs/opencode.md if changed."
}

# Sanitize captured events and write one versioned fixture, reusing the exact
# sanitizer the contract test asserts, so fixtures and tests cannot drift.
write_fixture() {
  local raw="$1" scenario="$2" exit_code="$3"
  local out="${REPO_ROOT}/${FIXTURES_DIR}/${scenario}.json"
  (
    cd "$REPO_ROOT"
    python3 - "$raw" "$out" "$scenario" "$exit_code" "$OPENCODE_MODEL" \
      "$OPENCODE_PIN" "$OPENCODE_NODE_REQ" <<'PY'
import json
import sys

from ai_agent.providers import opencode as oc

raw_path, out_path, scenario, exit_code, model, pin, node_req = sys.argv[1:8]
with open(raw_path, encoding="utf-8", errors="replace") as fh:
    events = oc.parse_events(fh.read())
fixture = {
    "format_version": 1,
    "scenario": scenario,
    "tool": "opencode",
    "pinned_version": pin,
    "node_requirement": node_req,
    "model": model,
    "exit_code": int(exit_code),
    "sanitized": True,
    "events": oc.sanitize_event(events),
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(fixture, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
  )
}

# Run one scenario inside the disposable checkout, measuring peak RSS, and write
# its sanitized fixture. The run's cwd is always the disposable checkout.
run_scenario() {
  local name="$1" model="$2" prompt="$3" extra_env="$4"
  local raw="${WORK_ROOT}/${name}.raw" time_file="${WORK_ROOT}/${name}.time"
  local exit_code=0
  log "scenario ${name}: running opencode in disposable checkout"
  # shellcheck disable=SC2086
  env $extra_env /usr/bin/time -v -o "$time_file" \
    node "$BIN" run --model "$model" --session "ds13-${name}-$$" "$prompt" \
    >"$raw" 2>&1 || exit_code=$?
  local rss_kb
  rss_kb="$(sed -n 's/.*Maximum resident set size (kbytes): //p' "$time_file" | tr -d ' ')"
  log "  scenario ${name}: exit=${exit_code} peak_rss_kb=${rss_kb:-unknown}"
  write_fixture "$raw" "$name" "$exit_code"
}

capture() {
  require_approval
  verify_runtime
  verify

  log "creating disposable worktree checkout"
  CHECKOUT="${WORK_ROOT}/checkout"
  git -C "$REPO_ROOT" worktree add --detach "$CHECKOUT" HEAD >/dev/null
  install_pinned "${WORK_ROOT}/install"

  mkdir -p "${REPO_ROOT}/${FIXTURES_DIR}"

  run_scenario success "$OPENCODE_MODEL" \
    "Make a tiny, safe change to this repository and report what you did." ""
  run_scenario error "$OPENCODE_MODEL" \
    "Trigger a provider error by asking for an impossible output." ""
  run_scenario auth "$OPENCODE_MODEL" \
    "Run once with an invalid key so DeepSeek returns a real auth error." \
    "OPENCODE_API_KEY=sk-invalid-characterization-key"
  run_scenario funds "$OPENCODE_MODEL" \
    "Run once against an account without spendable balance." \
    "OPENCODE_API_KEY=${DEEPSEEK_KEY:-sk-invalid-characterization-key}"
  run_scenario usage "$OPENCODE_MODEL" \
    "Make a tiny, safe change and then stop immediately." ""
  # Cancel: start, give it a moment, then interrupt the process group.
  local cancel_pid cancel_exit=0
  log "scenario cancel: running then interrupting"
  node "$BIN" run --model "$OPENCODE_MODEL" --session "ds13-cancel-$$" \
    "Keep working until cancelled." >"${WORK_ROOT}/cancel.raw" 2>&1 &
  cancel_pid=$!
  sleep 2
  kill -INT "$cancel_pid" 2>/dev/null || true
  wait "$cancel_pid" || cancel_exit=$?
  write_fixture "${WORK_ROOT}/cancel.raw" cancel "$cancel_exit"

  log "note: attachment capability is not probed by the six scenarios; record it"
  log "separately during the trial (OpenCode image support is still unverified)."

  # Containment: no orphan children, and the production checkout is untouched.
  if pgrep -f "$BIN" >/dev/null 2>&1; then
    die "orphan opencode children still running; aborting before cleanup"
  fi
  if [ -n "$(git -C "$REPO_ROOT" status --porcelain -- "${FIXTURES_DIR#./}" 2>/dev/null)" ]; then
    log "note: fixtures differ from HEAD (expected: capture wrote new fixtures)"
  fi
  if [ -n "$(git -C "$REPO_ROOT" status --porcelain | grep -v "${FIXTURES_DIR}/")" ]; then
    die "production checkout changed outside ${FIXTURES_DIR}/"
  fi

  log "capture complete: fixtures written to ${FIXTURES_DIR}"
  log "peak RSS per scenario recorded in ${WORK_ROOT} (see .time files before cleanup)"
}

main() {
  require_approval
  case "${1:-}" in
    verify) verify ;;
    capture) capture ;;
    *) die "usage: $0 {verify|capture}" ;;
  esac
}

main "$@"
