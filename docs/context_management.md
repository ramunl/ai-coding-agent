# Owner context management (Milestone 1)

Requires the CTX03/CTX04 lifecycle and packet integration. These commands manage
existing archive records; they do not change lifecycle recovery, snapshots, inbox
schemas or dashboard consumers. No restart or deployment is part of this change.

Commands require the configured owner's Telegram user **and** chat. Scope is
`owner` for owner-wide records or `repo:PROJECT` for an explicitly named registered
project. Repository scope uses the registry's complete captured identity (project,
path and GitHub repository), never the active project. A registry identity change
can make older records inaccessible through these controls; no scope fallback is
performed. Record kinds are `owner`, `initiative`, `repository` and `attempt`.

```
/context_inspect SCOPE KIND ID REV [KIND ID REV ...]
/context_link SCOPE KIND ID REV TARGET_KIND TARGET_ID TARGET_REV
/context_approve SCOPE KIND ID REV DECISION_ID
/context_reject SCOPE KIND ID REV DECISION_ID
```

For example, inspect `/context_inspect repo:my-project repository rules 3`, then
approve `/context_approve repo:my-project repository rules 3 choice` only after
reviewing that revision. Inspection reads pinned IDs, not an index or task-list
position. At most four records, eight decisions and eight links per record are
shown, with 300 characters per summary and 3500 bytes per JSON response. `truncated`
flags indicate omissions or shortening. Omitted content is not implicitly approved;
review the full proposal through the archive's trusted workflow before approval.
Output contains only IDs, revisions, summaries, decision status and pinned links;
credentials and local paths are redacted. Evidence locators, repository paths,
prompts, transcripts and approval metadata are excluded. No session state is kept.

Linking validates that the target pinned revision exists in exactly the source
scope. The source revision is an expected revision, checked by the store under its
existing lock. Attempt snapshots can be inspected or linked as targets but cannot
be edited. Indexes provide no mutation authority; existing archive scan limits
remain enforced.

Only proposed decisions can be approved or rejected. Approval provenance is built
server-side from the verified Telegram owner and update ID, with a server timestamp.
The service's `OwnerEvent` is trusted adapter input, never model output or parsed
command provenance. Storage validates provenance structure but does not authenticate
it; callers must preserve this boundary.

There is no `rejected` decision state and no schema migration. Rejection removes
the proposal from a newly committed revision. Its old revision remains historical;
existing pinned references still describe that history. Approved or superseded
decisions cannot be rejected by these controls.

Stale concurrent changes fail without retrying. Inspect again and explicitly submit
a new revision after review. Unknown records and unavailable archives receive the
same safe failure response; errors never disclose filesystem paths or create a
replacement record. A persistence error can occur after a durable commit, so inspect
before resubmitting. These commands never silently retry writes.
