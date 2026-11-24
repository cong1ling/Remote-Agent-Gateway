from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from remote_agent.profiles.base import ActionHandler, ProfileContext

from ..archive import inspect_zip, read_snapshot
from ..runner import run_verification

_DIGEST = re.compile(r"[0-9a-f]{64}")
_WINDOWS_SHARING_VIOLATION = 32
_CLEANUP_RETRY_SECONDS = 0.5
_CLEANUP_POLL_SECONDS = 0.02


def _cleanup_scratch(scratch: TemporaryDirectory) -> None:
    deadline = time.monotonic() + _CLEANUP_RETRY_SECONDS
    while True:
        try:
            scratch.cleanup()
            return
        except PermissionError as error:
            remaining = deadline - time.monotonic()
            if getattr(error, "winerror", None) != _WINDOWS_SHARING_VIOLATION or remaining <= 0:
                raise
            time.sleep(min(_CLEANUP_POLL_SECONDS, remaining))


class VerifyAction(ActionHandler):
    action_id = "verify"

    def __init__(self, context: ProfileContext):
        self._context = context
        self._requests: dict[str, tuple[threading.Event, asyncio.Task[dict[str, Any]]]] = {}

    async def execute(self, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict) or params.keys() != {"source_sha256"}:
            raise ValueError("build.invalid_params")
        digest = params["source_sha256"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("build.invalid_params")
        if request_id in self._requests:
            raise ValueError("build.runner_failed")

        cancelled = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(self._verify, digest, cancelled))
        self._requests[request_id] = (cancelled, worker)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await self.cancel(request_id)
            raise
        finally:
            self._requests.pop(request_id, None)

    async def cancel(self, request_id: str) -> None:
        active = self._requests.get(request_id)
        if active is not None:
            cancelled, worker = active
            cancelled.set()
            await asyncio.gather(asyncio.shield(worker), return_exceptions=True)

    def _verify(self, expected_digest: str, cancelled: threading.Event) -> dict[str, Any]:
        def check_cancelled() -> None:
            if cancelled.is_set():
                raise asyncio.CancelledError

        check_cancelled()
        data, digest = read_snapshot(self._context.build_archive, self._context.workdir)
        check_cancelled()
        if digest != expected_digest:
            raise ValueError("build.source_mismatch")

        try:
            scratch = TemporaryDirectory(prefix="rag-build-")
            try:
                project = Path(scratch.name) / "project"
                project.mkdir()
                inspect_zip(data, project, check_cancelled)
                check_cancelled()
                return {"source_sha256": digest, **run_verification(project, cancelled)}
            finally:
                _cleanup_scratch(scratch)
        except OSError:
            raise ValueError("build.runner_failed") from None
