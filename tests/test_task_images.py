"""Storage limits, intake safety and provider delivery for task attachments."""

import asyncio
import base64
import contextlib
import errno
import importlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent import image_delivery, image_uploads, task_images
from ai_agent.shell import CommandResult


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(task_images, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(task_images, "IMAGE_STORAGE_CAP_BYTES", 10)
    monkeypatch.setattr(task_images, "IMAGE_MAX_BYTES", 10)
    # Storage tests are independent of codecs; actual decoder tests below use PIL.
    monkeypatch.setattr(task_images, "decode", lambda data: ("png", 1, 1))
    monkeypatch.setattr(image_delivery, "resolve", task_images.resolve)
    monkeypatch.setattr(image_uploads, "save", task_images.save)
    monkeypatch.setattr(image_uploads, "remove", task_images.remove)
    return task_images


def test_oldest_creation_not_mtime_or_task_ownership(store):
    first = store.save(b"1111")
    second = store.save(b"2222")
    os.utime(store.IMAGE_DIR / first["id"], (9999999999, 9999999999))
    third = store.save(b"3333")
    assert store.resolve([first, second, third]) == (
        [("image/png", b"2222"), ("image/png", b"3333")],
        1,
    )
    assert store.usage() == {"used_bytes": 8, "cap_bytes": 10, "count": 2}


def test_startup_reclaims_parts_and_enforces_lowered_cap(store, monkeypatch):
    first = store.save(b"1111")
    second = store.save(b"2222")
    (store.IMAGE_DIR / "interrupted.part").write_bytes(b"xx")
    monkeypatch.setattr(store, "IMAGE_STORAGE_CAP_BYTES", 4)
    store.initialize()
    assert not list(store.IMAGE_DIR.glob("*.part"))
    assert store.resolve([first, second])[1] == 1
    assert store.usage()["used_bytes"] == 4


def test_concurrent_saves_keep_strict_cap(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda _: store.save(b"1234"), range(30)))
    assert store.usage()["used_bytes"] == 8
    existing = sorted(
        ref["id"] for ref in refs if (store.IMAGE_DIR / ref["id"]).exists()
    )
    assert existing == sorted(ref["id"] for ref in refs)[-2:]


@pytest.mark.parametrize("operation", ["usage", "remove"])
def test_resolve_releases_lock_before_decoding(store, monkeypatch, operation):
    refs = [store.save(b"1234"), store.save(b"5678")]
    decoding, resume = threading.Event(), threading.Event()

    def decode(data):
        decoding.set()
        assert resume.wait(5), "Decoding was not resumed"
        return "png", 1, 1

    def concurrent_operation():
        if operation == "remove":
            store.remove(refs)
        return store.usage()["count"]

    monkeypatch.setattr(store, "decode", decode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolved = pool.submit(store.resolve, refs)
        try:
            assert decoding.wait(3), "Decoding did not start"
            changed = pool.submit(concurrent_operation)
            assert changed.result(timeout=3) == (0 if operation == "remove" else 2)
        finally:
            resume.set()
        assert resolved.result(timeout=3) == (
            [("image/png", b"1234"), ("image/png", b"5678")],
            0,
        )


def test_resolve_protects_reads_and_closes_descriptors(store, monkeypatch, tmp_path):
    ref = store.save(b"1234")
    link = store.save(b"5678")
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    (store.IMAGE_DIR / link["id"]).unlink()
    (store.IMAGE_DIR / link["id"]).symlink_to(outside)
    (store.IMAGE_DIR / ref["id"]).write_bytes(b"x" * 100)
    opened, decoded = [], []
    original_open = os.open

    def tracked_open(path, flags):
        assert flags & os.O_NOFOLLOW
        fd = original_open(path, flags)
        opened.append(fd)
        return fd

    def decode(data):
        decoded.append(data)
        raise store.ImageError("Image is empty or exceeds the image byte limit.")

    monkeypatch.setattr(store.os, "open", tracked_open)
    monkeypatch.setattr(store, "decode", decode)
    assert store.resolve([ref, link, {"id": "../outside"}, {"id": None}]) == ([], 4)
    assert decoded == [b"x" * (store.IMAGE_MAX_BYTES + 1)]
    assert len(opened) == 1
    for fd in opened:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF


def test_resolve_preserves_order_validation_and_count_limit(tmp_path, monkeypatch):
    import io

    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(task_images, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(task_images, "IMAGE_MAX_BYTES", 10_000)
    monkeypatch.setattr(task_images, "IMAGE_STORAGE_CAP_BYTES", 100_000)
    monkeypatch.setattr(task_images, "IMAGE_MAX_PER_TASK", 4)
    refs, expected = [], []
    for fmt in ("PNG", "JPEG"):
        buffer = io.BytesIO()
        pil.new("RGB", (2, 2)).save(buffer, format=fmt)
        data = buffer.getvalue()
        refs.append(task_images.save(data))
        expected.append((f"image/{fmt.lower()}", data))
    invalid = task_images.save(expected[0][1])
    (task_images.IMAGE_DIR / invalid["id"]).write_bytes(b"invalid")
    missing = task_images.save(expected[0][1])
    task_images.remove([missing])
    assert task_images.resolve([refs[1], invalid, missing, refs[0], refs[1]]) == (
        expected[::-1],
        3,
    )


def test_inflight_codex_bytes_survive_eviction_without_disk_copy(store):
    ref = store.save(b"12345678")
    ready, evicted = threading.Event(), threading.Event()

    def run(command, **kwargs):
        path = command[command.index("--image") + 1]
        ready.set()
        assert evicted.wait(3)
        assert Path(path).read_bytes() == b"12345678"
        assert store.usage()["used_bytes"] == 8
        assert kwargs["private"]
        assert "--" in command
        return "ok"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            image_delivery.invoke,
            [ref],
            image_delivery.run_with_images,
            ["codex", "exec", "prompt"],
            run,
        )
        assert ready.wait(3)
        store.save(b"abcdefgh")
        evicted.set()
        assert future.result() == "ok"
    assert store.resolve([ref])[1] == 1
    assert not list(store.IMAGE_DIR.glob("*.part"))


def test_disk_full_cleans_temporary_files(store, monkeypatch):
    def full(_):
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(store.os, "fsync", full)
    with pytest.raises(store.ImageError, match="full"):
        store.save(b"1234")
    assert store.usage()["count"] == 0
    assert not list(store.IMAGE_DIR.glob("*.part"))


def test_claude_code_receives_native_blocks_on_stdin(store):
    ref = store.save(b"1234")

    def run(command, **kwargs):
        payload = json.loads(kwargs["input_text"])
        content = payload["message"]["content"]
        assert base64.b64decode(content[0]["source"]["data"]) == b"1234"
        assert content[-1] == {"type": "text", "text": "prompt"}
        assert "--input-format" in command and "--no-session-persistence" in command
        assert "prompt" not in command and kwargs["private"]
        return SimpleNamespace(
            args=command,
            returncode=0,
            output=json.dumps({"type": "result", "result": "done"}),
        )

    result = image_delivery.invoke(
        [ref], image_delivery.run_with_images, ["claude", "-p", "prompt"], run
    )
    assert result.output == "done"
    assert image_delivery.attachments() == ([], 0)


def test_missing_images_continue_with_visible_provider_warning(store):
    ref = store.save(b"1234")
    store.remove([ref])
    called = []
    image_delivery.invoke(
        [ref],
        image_delivery.run_with_images,
        ["codex", "exec", "prompt"],
        lambda command, **kwargs: called.append(command),
    )
    assert image_delivery.WARNING in called[0][-1]
    assert "--image" not in called[0]


@pytest.mark.parametrize(
    "value",
    [
        [],
        [{"name": "../a"}],
        [{"name": "/a"}],
        [{"name": "a", "path": "x"}],
        [{"name": "a"}] * 2,
        [{"name": "x"}] * 6,
    ],
)
def test_rejects_unsafe_upload_schema(value):
    with pytest.raises(task_images.ImageError):
        image_uploads.validate_names(value)


def stage(tmp_path, names=("a.png",)):
    path = tmp_path / "uploads" / "aabbccdd"
    path.mkdir(parents=True)
    for name in names:
        (path / name).write_bytes(b"1234")
    return path


def test_claim_consumes_staging_and_replay_fails(store, tmp_path):
    path = stage(tmp_path)
    refs = image_uploads.claim(tmp_path, "aabbccdd", [{"name": "a.png"}])
    assert len(refs) == 1 and not path.exists()
    with pytest.raises(task_images.ImageError):
        image_uploads.claim(tmp_path, "aabbccdd", [{"name": "a.png"}])


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_claim_rejects_nonregular_or_linked_files(store, tmp_path, kind):
    path = stage(tmp_path, ())
    external = tmp_path / "outside"
    external.write_bytes(b"private")
    target = path / "a.png"
    if kind == "symlink":
        target.symlink_to(external)
    elif kind == "hardlink":
        os.link(external, target)
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    with pytest.raises(task_images.ImageError):
        image_uploads.claim(tmp_path, "aabbccdd", [{"name": "a.png"}])
    assert external.read_bytes() == b"private"
    assert not path.exists()


def test_expire_removes_abandoned_staging_without_following_links(tmp_path):
    path = stage(tmp_path)
    os.utime(path, (1, 1))
    image_uploads.expire(tmp_path, now=200)
    assert not path.exists()


def test_api_receives_bytes_not_paths(store, monkeypatch):
    provider = importlib.import_module("ai_agent.planning_provider")
    monkeypatch.setattr(provider, "attachments", image_delivery.attachments)
    monkeypatch.setattr(provider, "diagnostic_context", lambda: "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    received = []
    monkeypatch.setattr(
        provider,
        "_create_message",
        lambda **kwargs: (
            received.append(kwargs)
            or SimpleNamespace(content=[SimpleNamespace(type="text", text="plan")])
        ),
    )
    ref = store.save(b"1234")
    result = image_delivery.invoke(
        [ref], provider.planner_message, "task", "claude", "feature_plan.json", 100
    )
    assert result == "plan"
    block = received[0]["messages"][0]["content"][0]
    assert block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == b"1234"
    assert str(store.IMAGE_DIR) not in json.dumps(received)


def test_inbox_reports_done_replay_and_expired_cleanup(store, tmp_path):
    from ai_agent import inbox

    stage(tmp_path)
    request = {
        "id": "aabbccdd",
        "action": "create_task",
        "requested_at": 100,
        "args": {
            "repo": "test",
            "text": "fix",
            "todo": "-",
            "images": [{"name": "a.png"}],
        },
    }
    log = inbox.ResultLog(tmp_path / "results.log")
    seen = []

    async def work(action, args):
        seen.append(args)
        return "added"

    def run():
        (tmp_path / "aabbccdd.json").write_text(json.dumps(request))
        asyncio.run(inbox.process_inbox(tmp_path, log, {}, now=101, work=work))

    run()
    assert log.results[0]["status"] == "done"
    assert seen[0]["images"][0]["id"]
    stage(tmp_path)
    run()
    assert log.results[0]["status"] == "failed" and len(seen) == 1
    stage(tmp_path)
    request["requested_at"] = -100
    run()
    assert log.results[0]["status"] == "expired"
    assert not (tmp_path / "uploads" / "aabbccdd").exists()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_actual_decoded_formats_and_truncation(fmt, monkeypatch):
    import io

    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(task_images, "IMAGE_MAX_BYTES", 10_000_000)
    buffer = io.BytesIO()
    pil.new("RGB", (32, 32), "red").save(buffer, format=fmt)
    data = buffer.getvalue()
    assert task_images.decode(data) == (fmt.lower(), 32, 32)
    with pytest.raises(task_images.ImageError):
        task_images.decode(data[:20])


def test_decoder_rejects_pixels_animation_and_wrong_format(monkeypatch):
    import io

    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(task_images, "MAX_PIXELS", 4)
    for fmt, size in [("PNG", (3, 3)), ("GIF", (1, 1))]:
        buffer = io.BytesIO()
        pil.new("RGB", size).save(buffer, format=fmt)
        with pytest.raises(task_images.ImageError):
            task_images.decode(buffer.getvalue())


def test_task_removal_and_automatic_pruning_clean_unshared_images(store, monkeypatch):
    from ai_agent import tasks

    monkeypatch.setattr(tasks, "remove", store.remove)
    monkeypatch.setattr(tasks, "MAX_TASKS", 2)
    ref = store.save(b"1234")
    data = {}
    first = tasks.add_task(data, "first", "repo", None, [ref])
    second = tasks.add_task(data, "second", "repo", None, [ref])
    first["stage"] = "done"
    tasks.add_task(data, "third", "repo", None)
    assert first not in data["tasks"]
    assert store.resolve([ref])[1] == 0
    tasks.remove_task(data, second)
    assert store.resolve([ref])[1] == 1


def test_image_config_rejects_git_and_excessive_limits(tmp_path, monkeypatch):
    from ai_agent import config

    exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: (
            exists(path) if path.name != ".git" or path == tmp_path / ".git" else False
        ),
    )
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "store")
    monkeypatch.setattr(config, "IMAGE_MAX_BYTES", 10_000_001)
    with pytest.raises(RuntimeError, match="IMAGE_MAX_BYTES"):
        config.validate_image_config()
    monkeypatch.setattr(config, "IMAGE_MAX_BYTES", 10_000_000)
    (tmp_path / ".git").mkdir()
    with pytest.raises(RuntimeError, match="outside git"):
        config.validate_image_config()


def test_worker_fallback_keeps_image_references(store):
    from ai_agent import worker

    ref = store.save(b"1234")
    task = {"id": "abc", "title": "task", "stage": "todo", "images": [ref]}
    data = {
        "planning_agent": "codex",
        "worker": {"enabled": True, "out_until": {"codex": 1000}},
    }
    decision = worker.decide(data, [task], {}, 100)
    assert decision.tool == "claude" and decision.task["images"] == [ref]
    assert "usage limit" in image_delivery.private_failure(
        "private bytes: usage limit reached"
    )
    assert "private bytes" not in image_delivery.private_failure(
        "private bytes: usage limit reached"
    )


def test_codex_planner_receives_attachment_flags(store, monkeypatch):
    provider = importlib.import_module("ai_agent.planning_provider")
    monkeypatch.setattr(provider, "run_with_images", image_delivery.run_with_images)
    monkeypatch.setattr(provider, "model_args", lambda: [])

    def run(command, **kwargs):
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert Path(command[command.index("--image") + 1]).read_bytes() == b"1234"
        Path(command[command.index("--output-last-message") + 1]).write_text("plan")
        return CommandResult(command, 0, "")

    monkeypatch.setattr(provider, "run", run)
    ref = store.save(b"1234")
    assert (
        image_delivery.invoke(
            [ref], provider._codex_message, "prompt", "feature_plan.json"
        )
        == "plan"
    )


def test_large_claude_images_normalized_without_disk_staging(monkeypatch):
    import io

    pil = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pil.new("RGB", (1600, 1600)).save(buffer, format="BMP")
    mime, data = image_delivery._claude_image("image/png", buffer.getvalue())
    assert mime == "image/jpeg" and len(data) < 4_000_000
    with pil.open(io.BytesIO(data)) as image:
        assert max(image.size) <= 1568


def test_startup_removes_abandoned_album_but_keeps_restored_references(store):
    abandoned = store.save(b"1234")
    retained = store.save(b"5678")
    refs = store.references_in({"task_queue": [{"images": [retained]}]})
    store.initialize(refs)
    assert store.resolve([abandoned, retained]) == ([("image/png", b"5678")], 1)


def test_memfd_attachment_is_readable_by_provider_child_process():
    import subprocess
    import sys

    with image_delivery.codex_files([("image/png", b"1234")]) as flags:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; print(len(open(sys.argv[1], 'rb').read()))",
                flags[1],
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    assert child.stdout.strip() == "4"


def test_private_command_failure_omits_paths_bytes_but_preserves_capacity(
    monkeypatch, caplog, tmp_path
):
    from ai_agent import shell

    monkeypatch.setattr(
        shell.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="private-payload usage limit reached",
            stderr="/proc/123/fd/7",
        ),
    )
    with pytest.raises(RuntimeError, match="usage limit") as error:
        shell.run(["codex", "--image", "/proc/123/fd/7"], cwd=tmp_path, private=True)
    assert "private-payload" not in str(error.value) + caplog.text
    assert "/proc/123/fd/7" not in str(error.value) + caplog.text


def test_usage_does_not_wait_for_image_lock(store):
    store.save(b"1234")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.locked():
            future = pool.submit(store.usage)
            assert future.result(timeout=2) == {
                "used_bytes": 4,
                "cap_bytes": 10,
                "count": 1,
            }


def test_usage_missing_storage_does_not_create_it(store):
    assert store.usage() == {"used_bytes": 0, "cap_bytes": 10, "count": 0}
    assert not store.IMAGE_DIR.exists()


def test_usage_tolerates_unlink_after_listing(store, monkeypatch):
    removed = store.save(b"1234")
    store.save(b"5678")
    scandir = os.scandir

    @contextlib.contextmanager
    def disappearing_entry(path):
        with scandir(path) as entries:
            captured = list(entries)
        (store.IMAGE_DIR / removed["id"]).unlink()
        yield iter(captured)

    monkeypatch.setattr(store.os, "scandir", disappearing_entry)
    assert store.usage() == {"used_bytes": 4, "cap_bytes": 10, "count": 1}
