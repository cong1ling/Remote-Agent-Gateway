from __future__ import annotations

from typing import Any

from ...base import ActionHandler
from ..supervisor import LocalSupervisor, select_supervisor


class InspectServiceAction(ActionHandler):
    action_id = "inspect"

    def __init__(self, supervisors: dict[str, LocalSupervisor]):
        self.supervisors = supervisors

    async def execute(self, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) != {"service_id"}:
            raise ValueError("service.invalid_params")
        return await select_supervisor(self.supervisors, params["service_id"]).inspect()
