from __future__ import annotations

import asyncio
from typing import Any

from remote_agent.profiles.base import ActionHandler, ProfileContext

from ..archive import inspect_zip, read_snapshot


class SourceAction(ActionHandler):
    action_id = "source"

    def __init__(self, context: ProfileContext):
        self._context = context

    async def execute(self, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict) or params:
            raise ValueError("build.invalid_params")
        return await asyncio.to_thread(self._source)

    def _source(self) -> dict[str, str]:
        data, digest = read_snapshot(self._context.build_archive, self._context.workdir)
        inspect_zip(data)
        return {"source_sha256": digest}
