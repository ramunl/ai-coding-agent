# Task image storage and dashboard part 2 contract

## Configuration and ownership

`IMAGE_DIR` defaults to `/var/lib/ai-coding-agent/images`. It must be absolute,
outside any Git checkout, and owned/writable only by the coding bot (directory
0700, files 0600). Do not put it under a repository that may later be registered.
The cap defaults to exactly 5,000,000,000 bytes (`5GB`) and must cover the maximum
image. Byte settings accept plain bytes or units (`parse_size` in `config.py`).
Image intake settings may be lowered but cannot exceed 10,000,000 bytes or five
images per task. Decoder limits are 16,000,000 pixels, 8000 per side, and one
frame. The actual decoded format must be PNG, JPEG or WebP; extensions and MIME
headers are not trusted. Install Pillow from requirements before accepting images.

The owner must use Ops to verify free disk space and configure staging ownership
and quotas. This implementation does not change server settings. It does not
infer free space from the configured cap. Writes that fail, including ENOSPC,
return a failed upload and remove their partial files.

## Request schema

Existing `create_task` requests without images remain valid. New example:

```json
{
  "id": "aabbccdd12345678",
  "requested_at": 1792139119,
  "action": "create_task",
  "args": {
    "repo": "registered-project",
    "text": "Implement the attached screen",
    "todo": "-",
    "images": [{"name": "screen.png"}]
  }
}
```

Part 2 must extend ai-dashboard's `agent_actions.py` validation and implement the
upload endpoint. The existing action name is unchanged. The dashboard authenticates
the owner server-side and streams uploads to
`INBOX_DIR/uploads/<request-id>/<name>`. The request ID must match the request
JSON filename. Names are unique ASCII alphanumeric-leading basenames, up to 128
characters, using letters, digits, dots, underscores or hyphens, with no `..`.
Each image entry has exactly `name`; never accept client-supplied absolute paths.
The optional list, when present, contains 1–5 entries. `todo` is still required;
use `-` for none. No inline image bytes, URLs or base64 are accepted in requests.

The dashboard must enforce byte limits while streaming (including unknown-length
bodies), at most four simultaneous staging batches and a total staging budget of
`4 * IMAGE_MAX_PER_TASK * IMAGE_MAX_BYTES` (200 MB by default). It must reject
excess intake before writing more bytes. Enforce a filesystem quota through Ops
if protection against a compromised staging writer is required. The bot cannot
prevent another process writing beyond its upload contract.

Upload all files and close them before atomically publishing `<id>.json`. Publish
within 120 seconds of `requested_at`. Never modify a published directory. Each
batch contains only its declared files. Use fresh cryptographically random IDs
for retries. Staging must be accessible only to the authenticated dashboard
service and coding bot: dedicated shared group, directories 0770 and files 0660
(or equivalent ACL). The private image store and `.image-claims` ledger remain
bot-only. Do not make either world-writable or expose an image-serving route.

The bot uses no-follow directory/file descriptors, rejects symlinks, hard links,
FIFOs and nonregular files, and bounds actual reads. An exclusive claim marker
and durable `.image-claims/<id>` ledger prevent replay after result rotation or
restart. Ledger markers contain no image content; keep them across restarts.
The bot removes consumed, failed and expired staging directories. A periodic
sweep removes unpublished directories older than 120 seconds and excess batches.
The dashboard must also clean disconnected/failed uploads promptly. A restart
may leave an interrupted request; retry using a new ID, not the old claim.

## Results and privacy

The normal action-results channel reports `done`, `failed`, `rejected` or
`expired`. Failures cover unsupported/undecodable/animated/oversized images,
missing or changed files, unsafe names/links, duplicate/replayed claims, an
unavailable decoder, and storage errors. A failed image batch creates no task;
already stored images from that batch are deleted. The UI should show the
returned message and permit retry with a new batch. Do not log file URLs, bytes,
base64, local storage paths or raw provider exceptions.

Snapshots add `image_storage: {used_bytes, cap_bytes, count}` and task
`image_count`. Counts describe references; evicted references stay on tasks.
The snapshot never includes image IDs, paths, bytes, or previews. No dashboard
image browsing endpoint is included in part 1.

## Eviction and provider delivery

Atomic writes, reads and eviction use a process lock plus `flock`. The image ID
contains a persisted monotonic creation timestamp (including clock rollback
handling); mtime and last use do not change eviction order. The cap includes
atomic-write staging reservations. Startup removes interrupted `.part` writes and files absent from restored
tasks, queues and pending work (including abandoned albums), then evicts to the
current cap. There are no pins for queued or active tasks.

Each provider invocation resolves references anew under the storage lock.
Bounded in-memory bytes can survive concurrent eviction, without open unlinked
disk files or disk copies escaping cap accounting. Codex receives RAM-backed
Linux memfd paths via `--image` before `--` and the prompt. These are read by the
CLI host before tool sandbox execution; no wider sandbox access is requested.
Image runs use ephemeral sessions. Claude API receives native base64 image blocks. Images above 4 MB are
resized in memory to fit 1568 pixels and encoded as JPEG, keeping five blocks
within Claude image/request byte limits; stored originals remain unchanged.
Claude Code uses the SDK user envelope on stream-json stdin, no session
persistence, and only retains the final result event. Private command diagnostics
omit arguments, stdin and failure output. Missing images produce a chat warning
and provider warning rather than stopping work. Plan revisions, queue entries,
retries, split tasks and automatic provider fallback preserve references.

Verified against local CLI help and official references:
[Codex CLI reference](https://learn.chatgpt.com/docs/cli/reference),
[Codex byte-based format detection](https://github.com/openai/codex/blob/main/codex-rs/utils/image/src/lib.rs),
[Claude image limits](https://platform.claude.com/docs/en/build-with-claude/vision),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference), and
[Claude streaming image input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode).
Provider contract tests inspect real payloads without sending private images or
making billable network calls. Live authenticated provider behavior and VPS disk
capacity are not established by unit tests.
