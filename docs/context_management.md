# Owner context management (Milestones 1–2)

Requires the CTX03/CTX04 lifecycle and packet integration. These commands manage
existing archive records; they do not change lifecycle recovery, snapshots or
dashboard consumers. Milestone 2 adds the versioned inbox contract below. No restart or deployment is part of this change.

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

## Milestone 2: bounded inbox requests

Inbox files are untrusted requests, never authenticated owner events. Version 1
uses these action names and exact argument objects (all argument values are strings):

| Action | Required arguments |
| --- | --- |
| `context_inspect_v1` | `scope`, `kind`, `record_id`, `revision` |
| `context_link_v1` | the above plus `target_kind`, `target_id`, `target_revision` |
| `context_approve_v1` | the base arguments plus `decision_id` |
| `context_reject_v1` | the base arguments plus `decision_id` |

The exact envelope is `id`, `action`, `args`, `requested_at`. The request ID is
8–32 lowercase hexadecimal characters; `requested_at` is a finite JSON number
containing Unix seconds in `(0, 1000000000000]`, no more than 120 seconds old
and never in the future.
`scope` is `owner` or `repo:PROJECT`, where PROJECT matches `[A-Za-z0-9._-]{1,64}`.
Kinds are the four kinds listed above. Record, target and decision IDs match
`[A-Za-z0-9_-]{1,64}`. Revisions are canonical decimal strings from `1` to
`1000000`. Inspection requests pin one record; direct Telegram inspection still
supports four. Unknown keys, including actor, approval and provenance fields at
either level, are refused. Inbox files larger than 32 KiB are refused.

Example request:

```json
{"id":"01234567","action":"context_approve_v1","args":{"scope":"owner","kind":"owner","record_id":"notes","revision":"3","decision_id":"choice"},"requested_at":1793700000}
```

The bot resolves the explicit registered scope, persists a binding and sends a
plain-text review prompt to the owner. This does not access the archive. All four
inbox actions, including inspection, require an authenticated owner command:

```
/context_approve confirm TOKEN
```

Use the matching command (`context_inspect`, `context_link`, `context_approve`, or
`context_reject`) shown in the prompt. The random token binds the exact action,
complete captured scope, record IDs, revisions and decision ID. Review the pinned
records before confirming a mutation. The handler authenticates both Telegram
user and chat before resolving scope or opening storage. Edited messages and
invalid update IDs are rejected. Wrong-action tokens cannot consume the binding;
a registry scope change refuses archive access. Approval metadata comes only from
the authenticating confirmation update, never the inbox request or notification.

Bindings and consumption markers live in `context-confirmations.sqlite3` beside
`AGENT_STATE_FILE`, outside the inbox. This private bot-owned database must not be
writable by inbox producers. It uses SQLite transactions with full synchronous
commits and at most 256 live bindings. No new `user_data` keys are needed; PTB
persistence and rotating inbox results are not the replay authority.

A binding expires 600 seconds after the original request timestamp. Atomic
consumption commits **before** any archive read or write. Concurrent confirmations,
restarts and result rotation cannot repeat a claimed action. Even failed or
uncertain archive operations consume the binding. A crash between claiming and
writing, or between writing and reporting success, therefore requires inspection;
there are no automatic mutation retries. Notification failures likewise retain
the binding and cannot automatically send another confirmation for the same ID.
At capacity, new requests fail closed rather than evicting live claims.

Expired rows are removed when new requests arrive; their original request timestamp
is already outside the 120-second acceptance window. Reusing an ID with a fresh
timestamp after expiry creates a new request requiring a new random token and owner
confirmation. An old token never authorizes the new binding. Do not delete the
database to retry uncertain work. Inspect the archive and explicitly submit a new
request with the current revision after reviewing the result.

Inbox results report `pending` after prompt delivery; the confirmation result goes
to Telegram. Missing delivery support, unavailable confirmation storage, expiry,
replay and mismatched confirmations fail closed with bounded messages. Unknown
records and unavailable archives keep the existing safe response; errors do not
expose paths or synthesize replacement records. Snapshot/status-contract updates,
dashboard action mirroring and task compatibility remain milestone 3/consumer work.
CTX03/CTX04 storage, lifecycle and decision semantics are unchanged.
