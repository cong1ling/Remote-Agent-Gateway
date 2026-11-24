from __future__ import annotations

from typing import Any

from ..base import ProfileContext, ProfileHandler
from ..common.managed_contracts import SERVICE_BACKEND, SERVICE_PROFILE
from .actions import InspectServiceAction, RestartServiceAction
from .supervisor import load_local_supervisors


class ServiceControlV1Profile(ProfileHandler):
    profile_id = SERVICE_PROFILE
    plan_only = True

    def __init__(self, context: ProfileContext):
        supervisors = load_local_supervisors(context.service_config, context.workdir)
        self.service_ids = tuple(sorted(supervisors))
        super().__init__(context, {"inspect": InspectServiceAction(supervisors), "restart": RestartServiceAction(supervisors)})

    def runtime_metadata(self) -> dict[str, Any]:
        return {"backend": SERVICE_BACKEND, "service_ids": list(self.service_ids)}
