"""Bounded, disposable metadata indexing; archive revisions remain authoritative."""

import json
from dataclasses import asdict, dataclass

from ai_agent.context_store import (
    ContextPersistenceError,
    ContextStore,
    atomic_write,
    read_bytes,
    record_names,
)
from ai_agent.context_validation import (
    ContextValidationError,
    ScopeMismatchError,
    UnsupportedSchemaError,
    sanitized_record,
)

MAX_INDEX_ENTRIES = 1000
MAX_DIAGNOSTICS = 100
MAX_INDEX_BYTES = 512 * 1024


@dataclass(frozen=True)
class IndexResult:
    """Metadata and bounded diagnostics; incomplete indexes are explicitly marked."""

    entries: tuple[dict, ...]
    diagnostics: tuple[str, ...]
    complete: bool


def _cache_supported(directory: int) -> bool:
    try:
        data = json.loads(read_bytes(directory, "index.json", MAX_INDEX_BYTES))
    except FileNotFoundError:
        return True
    except (ValueError, UnicodeError, RecursionError):
        return True  # Corrupt derived metadata can be rebuilt.
    return not isinstance(data, dict) or data.get("schema_version", 1) == 1


def rebuild_index(store: ContextStore) -> IndexResult:
    """Scan without repairing, deleting or overwriting any archive record."""
    entries, diagnostics = [], []
    complete = True
    with store.directory() as directory:
        try:
            for name in record_names(directory):
                try:
                    record = sanitized_record(store._read(directory, name))
                except ScopeMismatchError:
                    continue
                except (
                    ContextValidationError,
                    ContextPersistenceError,
                    OSError,
                ) as error:
                    complete = False
                    if len(diagnostics) < MAX_DIAGNOSTICS:
                        # Never include record contents, exceptions or arbitrary names.
                        diagnostics.append(
                            "Unsupported record schema preserved"
                            if isinstance(error, UnsupportedSchemaError)
                            else "Unreadable record preserved"
                        )
                    continue
                if len(entries) >= MAX_INDEX_ENTRIES:
                    complete = False
                    diagnostics.append("Metadata entry limit reached")
                    break
                entries.append(
                    {
                        "kind": record.kind,
                        "record_id": record.record_id,
                        "revision": record.revision,
                    }
                )
        except ContextPersistenceError:
            complete = False
            diagnostics.append("Archive scan limit reached")
        result = IndexResult(tuple(entries), tuple(diagnostics), complete)
        # One cache per owner: include scope in the envelope so other scopes reject it.
        payload = json.dumps(
            {
                "schema_version": 1,
                "scope": asdict(store.scope),
                "result": asdict(result),
            }
        ).encode()
        if len(payload) > MAX_INDEX_BYTES:
            raise ContextPersistenceError("Index exceeds size limit")
        if _cache_supported(directory):
            atomic_write(directory, "index.json", payload)
        else:
            result = IndexResult(
                result.entries,
                (
                    *result.diagnostics[:MAX_DIAGNOSTICS],
                    "Unsupported index schema preserved",
                ),
                False,
            )
    return result


def load_index(store: ContextStore) -> IndexResult:
    """Load a cache or rebuild it; callers explicitly rebuild after archive writes."""
    try:
        with store.directory() as directory:
            data = json.loads(read_bytes(directory, "index.json", MAX_INDEX_BYTES))
        result = data["result"]
        entries = result["entries"]
        diagnostics = result["diagnostics"]
        if (
            data["schema_version"] != 1
            or data["scope"] != asdict(store.scope)
            or type(entries) is not list
            or len(entries) > MAX_INDEX_ENTRIES
            or type(diagnostics) is not list
            or len(diagnostics) > MAX_DIAGNOSTICS + 1
            or type(result["complete"]) is not bool
        ):
            raise ValueError
        for entry in entries:
            if type(entry) is not dict or set(entry) != {
                "kind",
                "record_id",
                "revision",
            }:
                raise ValueError
            store._prefix(entry["kind"], entry["record_id"])
            if type(entry["revision"]) is not int or entry["revision"] < 1:
                raise ValueError
        if any(type(item) is not str or len(item) > 100 for item in diagnostics):
            raise ValueError
        return IndexResult(tuple(entries), tuple(diagnostics), result["complete"])
    except (
        OSError,
        ContextPersistenceError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
    ):
        return rebuild_index(store)
