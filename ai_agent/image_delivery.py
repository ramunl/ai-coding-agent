"""Deliver bounded attachments to providers without exposing disk paths or bytes."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any

from ai_agent.ai_tools import get_tool
from ai_agent.task_images import resolve

_references: ContextVar[list[dict]] = ContextVar("task_images", default=[])
_open_fds: ContextVar[tuple[int, ...]] = ContextVar("task_image_fds", default=())
WARNING = "Some task images were evicted or unavailable; continuing without them."


def open_fds() -> tuple[int, ...]:
    """Memfd descriptors currently exposed to a provider child via pass_fds."""
    return _open_fds.get()


def invoke(images: list[dict], work: Callable, *args: Any) -> Any:
    """Scope attachments to this call, including provider fallback and retries."""
    token = _references.set(images)
    try:
        return work(*args)
    finally:
        _references.reset(token)


def attachments() -> tuple[list[tuple[str, bytes]], int]:
    """Resolve again immediately before each provider invocation."""
    return resolve(_references.get())


def blocks(prompt: str, images: list[tuple[str, bytes]]) -> list[dict]:
    """Build native Anthropic content blocks, never local-path placeholders."""
    images = [_claude_image(mime, data) for mime, data in images]
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.b64encode(data).decode("ascii"),
            },
        }
        for mime, data in images
    ] + [{"type": "text", "text": prompt}]


def _claude_image(mime: str, data: bytes) -> tuple[str, bytes]:
    """Keep five base64 images under Claude's per-image and request limits."""
    if len(data) <= 4_000_000:
        return mime, data
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as original:
        image = ImageOps.exif_transpose(original)
        image.thumbnail((1568, 1568))
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=85)
    encoded = output.getvalue()
    if len(encoded) > 4_000_000:
        raise RuntimeError("Image cannot fit the provider input limit.")
    return "image/jpeg", encoded


def private_failure(output: str) -> str:
    """Retain only a safe capacity signal so worker fallback still functions."""
    if re.search(
        r"usage limit|limit reached|hit your (usage )?limit|rate.?limit|quota exceeded",
        output,
        re.I,
    ):
        return "Image provider usage limit reached; retry or use another provider."
    return "Image provider command failed."


@contextlib.contextmanager
def codex_files(images: list[tuple[str, bytes]]) -> Iterator[list[str]]:
    """Expose RAM-backed files to the CLI host, without sandbox permission changes.

    Codex loads initial --image attachments in its host process before tool
    sandboxing. The descriptors are handed to the provider child with pass_fds
    and referenced through /proc/self/fd, so they stay readable after the child
    drops to the worker user (a different uid cannot read /proc/<bot-pid>/fd).
    No disk copy or open, unlinked storage file remains outside cap accounting.
    """
    with contextlib.ExitStack() as stack:
        paths = []
        fds = []
        for _, data in images:
            fd = os.memfd_create("task-image", os.MFD_CLOEXEC)
            output = stack.enter_context(os.fdopen(fd, "w+b"))
            output.write(data)
            output.flush()
            fds.append(fd)
            paths.extend(["--image", f"/proc/self/fd/{fd}"])
        token = _open_fds.set(tuple(fds))
        try:
            yield paths
        finally:
            _open_fds.reset(token)


def _codex_image_run(
    command: list[str],
    run: Callable,
    *,
    images: list[tuple[str, bytes]],
    missing: int,
    **kwargs: Any,
) -> Any:
    """Deliver images to Codex through RAM-backed --image flags."""
    prompt = command[-1]
    if missing:
        prompt += "\n" + WARNING
    with codex_files(images) as flags:
        if "--ephemeral" not in command:
            flags.append("--ephemeral")
        return run([*command[:-1], *flags, "--", prompt], private=True, **kwargs)


def _claude_image_run(
    command: list[str],
    run: Callable,
    *,
    images: list[tuple[str, bytes]],
    missing: int,
    **kwargs: Any,
) -> Any:
    """Deliver images to Claude Code through the stream-json envelope."""
    prompt = command[2]
    if missing:
        prompt += "\n" + WARNING
    # Claude Code accepts the SDK user envelope on stream-json stdin.
    content = blocks(prompt, images)
    payload = (
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
                "session_id": "",
            }
        )
        + "\n"
    )
    result = run(
        [
            *command[:2],
            *command[3:],
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
        ],
        input_text=payload,
        private=True,
        **kwargs,
    )
    # Never retain streamed user blocks (which may echo image bytes).
    output = ""
    for line in result.output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            output = str(event.get("result") or "")
            if event.get("is_error"):
                raise RuntimeError(private_failure(output))
    if not output:
        raise RuntimeError("Claude Code returned no image-run result.")
    return type(result)(args=["claude"], returncode=result.returncode, output=output)


def run_with_images(command: list[str], run: Callable, **kwargs: Any) -> Any:
    """Run a registered tool with native image input and private diagnostics."""
    images, missing = attachments()
    if not images and not missing:
        return run(command, **kwargs)
    tool = get_tool(command[0])
    if tool is None:
        raise RuntimeError(f"Unknown image tool: {command[0]}")
    if not tool.images:
        raise RuntimeError(f"{tool.name} does not support image input")
    return tool.image_run(command, run, images=images, missing=missing, **kwargs)
