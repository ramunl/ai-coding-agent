# Context lifecycle (CTX03)

New chat plans, implementation requests and bugfixes capture explicit owner and
repository identities. Task, inbox, worker and Autopilot entry points share those
handlers. Existing saved work without `context_spec` remains archive-free.

Trusted persistent `context_sources` supplies pinned constraints, prerequisites,
candidates and dependency IDs. Model responses never populate these fields.
There is no implicit owner-wide archive discovery. Reopened tasks retain their
handoff references. Explicit archived prerequisites require successful handoffs;
legacy `after` links without archives retain their existing behavior until CTX04.
CTX02's existing scope restrictions remain enforced: cross-repository prerequisite
composition is blocked rather than implicitly authorizing another repository.

Approval seals the exact plan, images, repository, planning packet and current
source revisions. Confirmation and queue dispatch revalidate the seal; provider
selection can change for retries. Sealed run prompts are retained exactly up to
256,000 characters; larger inputs are refused before dispatch rather than silently
truncated. Stale approvals require review, and Autopilot
cannot clear `review_required`. Legacy boolean approvals remain readable.
Implementation and all repair operations build packets after checkout/pull/fetch,
with application-observed HEAD. No summarization provider is used.

## Effects and handoffs

`context_attempts.py` maintains one private, fsynced lifecycle journal per attempt
beside CTX01 records. Intent checkpoints precede provider execution, commit, push
and PR creation. Application observations record diff digests, changed files,
commit SHA, PR identifiers and CI results. Before provider exceptions reach Git
cleanup, bounded changed paths and a tracked-diff digest are captured; untracked
paths are listed and their contents are not claimed to be covered by that digest.
Terminal handoffs include the observed checkout HEAD, final commit, paths/digest,
last phase and unresolved work. Model test claims remain unverified.
Checkpoint identities are content-addressed; a separate latest-observation pointer
preserves ordering when the same CI result recurs. Prompts, diffs and transcripts
are not journal fields. Journals have a 512 KiB bound and are retained.

Terminal handoff drafts are journaled before writing immutable CTX01 attempts.
An interrupted write can be retried with the exact same record. Terminal records
are never updated. Complete packet references live in the journal; CTX01 records
retain packet digests and references within their own scope, as required by CTX01.
No-code results retain their blocked/Ops classification in journal checkpoints and
existing task outcome state.

Cancellation records the request but waits for actual threaded completion,
including repeated cancellation. A successful run can therefore have a successful
handoff despite a cancellation request. No second attempt is permitted while its
journal remains unfinished.

## Recovery

Startup scans authoritative journals even if PTB never saved the running task.
`context_recovery_pending` is persistent and exposed in the snapshot. The worker
reconciles before scheduling more work. Terminal drafts recover without rerunning
Git or GitHub effects. Other unfinished attempts are reconciled against the local
branch, same-repository PR head and CI. Existing PR results are retained even when
context persistence fails; PR creation also checks for an existing matching PR.

Reconciliation resolves each journal's exact registered repository without changing
the active selection, takes its Git execution lease and verifies no provider writer
remains. Known pre-publication exceptions produce a failed or blocked incomplete
handoff, allowing explicit Resume through the separate Git checkpoint. A crashed
pre-publication attempt can also close as failed once the repository is quiescent
and a complete bounded GitHub query finds no PR, provided no PR-creation intent was
recorded. This records interruption, never completion, and performs no Git reset,
checkout, commit, push or PR creation.

Absent or inaccessible evidence, live writers, changed repository registrations,
mismatched local/remote heads, or ambiguous PR-creation intent keeps recovery pending.
Unknown PR effects require operator inspection of journals, Git recovery refs and
GitHub; no automatic force-clear action exists. Archive propagation matches exact
task ID, repository and branch, never a reused branch alone. Successful PR results
remain visible even if archive persistence fails, while automatic dependent work
waits for reconciliation. No service/deployment configuration is changed; CTX08
backup/restore requirements gate rollout separately from this code-only change.
