# Deterministic context packets (CTX02)

`ContextRequest` consumes the CTX01 contracts. A trusted caller supplies the
owner authorization, exact repository scope, initiative ID, task ID and attempt
ID. Authorization is an input from the owner-action boundary, never a claim in
model output. Each requested scope requires exactly one explicitly supplied
`ContextStore`; owner-wide inheritance is not automatic. All scopes are checked
before any archive read. No active-project lookup selects historical records.

Supply approved constraint references in `constraints`, successful prerequisite
attempt references in `prerequisites`, and bounded discovery results in
`candidates`. `dependency_ids` names prerequisite task IDs. Selected initiative
records also contribute their dependency IDs. Pinned record references must be
included as mandatory sources; incomplete reference graphs block. The archive
index is not used to establish completeness or freshness. Lifecycle handlers
must supply the complete mandatory set, not treat candidate discovery as proof
that no constraints exist.

Selection includes mandatory sources first, same-task attempts next, then at
most two related records. Related relevance counts exact token overlap with the
explicit query; ties use canonical scoped reference identity. Approved decisions
in candidates are also mandatory. Different approved summaries sharing a decision
ID block for owner reconciliation. Arbitrary semantic contradictions cannot be
detected without an explicit conflict identity; there is no model or embedding
call. Stale pinned revisions block, including optional candidates whose newer
revision could carry an approved constraint.

Packets default to 12,000 serialized characters and five attempt handoffs.
Both limits can be lowered, never raised. Metadata, omissions, document excerpts
and identities count toward the character limit. Optional records are omitted
whole, with reasons; nothing is silently shortened. Mandatory records and trace
metadata that cannot fit block. `truncations` is empty because packet assembly
never truncates a source. Each included source has a digest, and the result has
a CTX01 `PacketReference` containing the packet digest and selected archive refs.

Repository evidence is explicitly supplied as `RepositoryEvidence(path, sha256)`.
Files are complete UTF-8 content with matching digests, read through bounded
file-descriptor walks rejecting symlink ancestors, traversal, nonregular files
and hardlinks. They are redacted and labelled untrusted data. Archive summaries,
approval provenance and evidence pointers are allowlisted; raw transcripts,
prompts and reasoning are not packet fields. CTX01 callers must still curate
summaries rather than putting execution transcripts into a summary string.

Pass the same request using `context=` to `plan_feature`, `revise_feature_plan`,
`assess_bugfix_report`, `planner_message`, `implement`, `repair_implementation`,
`repair_pull_request_branch`, or `run_implementation_agent`. For a retry/repair
callback use `functools.partial(function, context=request)` so explicit identity
survives the callback. A fresh task attempt requires a fresh attempt ID. Planner
and workflow repository operations check the captured identity against the
selected repository; workflow validation runs before Git recovery or checkout.
Codex, Claude Code and the Anthropic API receive identical historical JSON.
`ContextBlockedError` contains a reason to surface to the owner; callers must not
retry without context after this exception.

Task/rules prompt strings are preserved. Supply existing legacy attempt metadata
in `reopen_history`; it is allowlisted and redacted by the shared reopen renderer.
If its exact canonical block is already in the original prompt, the packet records
its digest/location and reserves its character count instead of duplicating it.
Otherwise the packet includes it. No identity is inferred from prompt text. Existing callers without `context=` remain archive-free;
this is not automatic CTX03 lifecycle/archive rollout or persistence integration.
Explicit-context planning reads repository briefs strictly: required instruction
files that exceed their limit or cannot be safely read block. Legacy brief previews
remain bounded; repository search is optional, bounded and labelled untrusted.
