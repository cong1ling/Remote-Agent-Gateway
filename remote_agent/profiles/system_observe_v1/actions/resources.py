from __future__ import annotations

import asyncio
import os
from typing import Any

from remote_agent.profiles.base import ActionHandler

from ..common import disk_snapshot, memory_snapshot, observation_metadata


class ResourcesAction(ActionHandler):
    action_id = "get_resources"

    def __init__(self, workdir):
        self.workdir = workdir

    async def execute(
        self,
        request_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        del request_id
        if params:
            raise ValueError("system_observe.v1 观察动作不接受参数")
        result = observation_metadata()
        result["cpu"] = {
            "logical_count": os.cpu_count(),
            "load_average": _load_average(),
        }
        result["memory"] = await asyncio.to_thread(memory_snapshot)
        result["disks"] = [disk_snapshot(self.workdir)]
        return result


def _load_average() -> list[float] | None:
    try:
        return [round(float(value), 3) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None
