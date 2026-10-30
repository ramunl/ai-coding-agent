# Context archive (CTX01)

This is a storage and contract foundation. CTX03 will connect it to planning,
approval, implementation and task lifecycle handlers. No bot, worker, dashboard,
provider or live service calls are added here. Existing tasks load unchanged.

## Contracts and scope

`context_models.py` defines frozen, provider-independent version-one contracts:

- `OwnerContext`: curated owner-wide preferences and decisions.
- `InitiativeContext`: scoped initiative summary and stable dependency IDs.
- `RepositoryContext`: curated repository knowledge.
- `AttemptSnapshot`: immutable task/attempt IDs, outcome, summary and packet refs.

Every record has a schema version, opaque record ID, revision, explicit scope,
summary, decisions, evidence pointers and pinned record references. Draft revision
zero means not persisted. `RepositoryIdentity` uses the existing planning fields
`project_name`, `repo_path`, and `github_repository`; it never resolves the active
project or uses a filesystem path as an archive key. IDs are 1–64 ASCII letters,
digits, underscores or hyphens. Dependencies name stable IDs, never array positions
or task titles. Their scheduling/resolution belongs to CTX03; they are not implicit
archive references.

A store is bound to one owner and exact repository identity (or owner-wide scope).
Repository and attempt records require repository scope; owner records prohibit it.
Initiatives can use either. References, including those inside packets, must have
exactly the same scope and pin an existing revision. Cross-repository and owner-wide
to repository reference inheritance is deliberately not implicit. A caller must
explicitly compose separately authorized context in future lifecycle integration.
Record IDs are unique per owner and kind, including across that owner's repositories.

Evidence stores only a stable ID, source category, locator and short summary.
Packets store only a stable ID, SHA-256 digest and revision references. Unknown
fields are rejected, including raw prompts, transcripts, environments and image
payloads. These must never be placed in summaries either. Limits are 4,000 characters
per string, 64 items per collection, and 64 KiB per serialized record; oversized
records fail rather than truncate. Integers reject booleans. Revisions are bounded
at 1,000,000.

Decisions start `proposed`, without approval or supersession metadata. `approved`
and `superseded` require the scoped owner's ID, event ID, positive Unix timestamp
and channel (`telegram`, `dashboard`, `manual`). A superseded decision points to
another approved/superseded decision in the same record; missing links and cycles
are rejected. Later revisions cannot remove or rewrite approved text/provenance,
reverse approval, or alter an already superseded decision. Approval provenance is
structurally checked, not authenticated: only a trusted owner-action handler may
supply it. A model's claim of approval is not authorization.

## Persistence and errors

`CONTEXT_ROOT` is read once at config import, defaulting to
`/var/lib/ai-coding-agent/context`. Import/construction performs no filesystem I/O.
Tests pass explicit temporary roots and synthetic scopes. Each owner directory
contains `<kind>.<record_id>.<revision>.json`, persistent per-record lock files and
an optional `index.json`. Archive directories use 0700; new files use 0600.
Paths must be absolute without traversal. Directory-descriptor walks and
`O_NOFOLLOW` reject symlink ancestors, owner directories, lock files and records;
reads also reject hardlinks and nonregular files without blocking on FIFOs.
The storage root must be dedicated to the archive and writable only by its owner.

`ContextStore.write(record, expected_revision=N)` validates and redacts, acquires
an exclusive per-record `flock`, checks the latest revision, validates transitions
and references, and writes revision N+1. Configured secrets use `redact_sensitive`;
common bearer and credential assignment forms receive conservative redaction.
Structural identity/provenance that would change under redaction is rejected.
Redaction cannot detect every secret: callers must curate inputs and exclude raw
execution material. No submitted contents are included in persistence diagnostics.

A temporary file is flushed and fsynced before atomic replacement; the directory
is then fsynced. Newly created directory entries are also fsynced. Old revisions
are retained. Attempts permit only the initial write. `RevisionConflictError`
requires reloading and reconciling; it must not trigger blind overwrites.
`ContextValidationError` rejects malformed contracts; `UnsupportedSchemaError`
means a compatible reader is needed. `ContextPersistenceError` reports I/O failure.
A failure after replacement can mean the new revision exists even though the call
failed: read the authoritative record before retrying. No index commit is included
in the record transaction.

## Indexing and recovery

`rebuild_index(store)` scans the archive and writes derived kind/ID/revision
metadata only. `load_index(store)` rebuilds a missing or corrupt cache. The cache
is explicitly stale after record writes until rebuilt; never use it for revision,
approval, dependency existence or completeness decisions. Concurrent rebuilds may
produce stale snapshots. One cache is shared per owner with a scope envelope;
loading for a different repository triggers a rebuild, filtering other scopes.

Scanning examines at most 100,000 directory entries, keeps at most 1,000 revision
entries and 100 record diagnostics (plus a limit/recovery diagnostic), and caps the
cache at 512 KiB. Hitting a bound returns an incomplete result. This is bounded
discovery, not a complete historical query API. Scan order is filesystem order.
Direct pinned reads remain available beyond the metadata cap. Latest-revision
reads/writes fail explicitly if the directory scan bound is exceeded.

Records remain authoritative if an index update fails. Corrupt and unknown-schema
records are preserved with bounded diagnostics, never repaired or overwritten by
index recovery. Unknown-schema indexes are also preserved; rebuilding returns
in-memory metadata with a diagnostic. Corrupt current revisions block updates and
latest reads instead of silently falling back; earlier pinned revisions remain
readable. Interrupted temporary files are ignored and retained for inspection.
There is no automatic deletion, salvage or schema migration. Recovery should
preserve the original archive, use a compatible reader for newer schemas, inspect
failures offline, and rebuild derived metadata. A symlink or permission error
requires correcting the storage setup outside this coding change.

## Retention and backup gap

The archive is independent of the 50-task pruning in `tasks.py`: pruning or loading
legacy task dictionaries cannot remove snapshots or referenced revisions. There is
no automatic archive TTL, garbage collection or pruning, even for unreferenced
records. Capacity management and any future deletion policy must preserve pinned
references and require a separate design; disk-full errors are explicit.

The default context directory is **outside current backup coverage**. CTX08 backup
inclusion and restore testing gate **CTX07 rollout**, not CTX01 acceptance. Retain
the existing **20 MiB backup limit**; this change does not increase it or change any
backup/deployment/service configuration. Backup-size handling and demonstrated
restoration of records, scopes and pinned references must be addressed before
rollout. A rebuilt index alone is not a backup of the archive.
