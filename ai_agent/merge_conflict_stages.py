"""Model and parse the unmerged index stages of a conflicted merge."""

from __future__ import annotations

from dataclasses import dataclass

STAGE_BASE = 1
STAGE_HEAD = 2
STAGE_INCOMING = 3


@dataclass(frozen=True)
class IndexStage:
    """One staged blob of a path: its Git mode and object id.

    Modes are kept verbatim (``100644`` regular, ``100755`` executable,
    ``120000`` symlink, ``160000`` gitlink) so a resolver can reconstruct
    binary, symlink and submodule entries without guessing from the blob.
    """

    mode: str
    blob: str


@dataclass(frozen=True)
class IndexEntry:
    """One parsed index record: path, stage, mode, and blob."""

    path: str
    stage: int
    mode: str
    blob: str


@dataclass(frozen=True)
class Conflict:
    """One conflicted path and whichever index stages Git recorded.

    Add/add and modify/delete conflicts legitimately omit stages, so each of
    the three stages is optional. Stage 1 is the merge base, stage 2 the PR
    head (``ours``), and stage 3 the incoming base (``theirs``).
    """

    path: str
    stage1: IndexStage | None = None
    stage2: IndexStage | None = None
    stage3: IndexStage | None = None

    def stages(self) -> dict[int, IndexStage]:
        """Return only the stages Git recorded, keyed by their number."""
        recorded = {
            STAGE_BASE: self.stage1,
            STAGE_HEAD: self.stage2,
            STAGE_INCOMING: self.stage3,
        }
        return {
            number: entry for number, entry in recorded.items() if entry is not None
        }


def parse_index(records: str) -> list[IndexEntry]:
    """Parse NUL-delimited ``git ls-files --stage -z`` records.

    Each record is ``<mode> <blob> <stage>\\t<path>`` terminated by NUL. Modes
    and blobs are preserved as text; the path is everything after the first
    tab, so tabs inside a path survive.
    """
    entries: list[IndexEntry] = []
    for record in records.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        if not path:
            continue
        mode, blob, stage_text = meta.split()
        entries.append(
            IndexEntry(path=path, stage=int(stage_text), mode=mode, blob=blob)
        )
    return entries


def parse_unmerged(records: str) -> list[Conflict]:
    """Group unmerged index records by path into conflict data.

    Accepts ``git ls-files --unmerged -z`` output (or the full ``--stage -z``
    output, whose stage-0 entries are ignored) and returns one ``Conflict``
    per path in first-seen order.
    """
    by_path: dict[str, dict[int, IndexStage]] = {}
    order: list[str] = []
    for entry in parse_index(records):
        if entry.stage == 0:
            continue
        if entry.path not in by_path:
            by_path[entry.path] = {}
            order.append(entry.path)
        by_path[entry.path][entry.stage] = IndexStage(mode=entry.mode, blob=entry.blob)
    return [
        Conflict(
            path=path,
            stage1=by_path[path].get(STAGE_BASE),
            stage2=by_path[path].get(STAGE_HEAD),
            stage3=by_path[path].get(STAGE_INCOMING),
        )
        for path in order
    ]
