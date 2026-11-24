from __future__ import annotations

from typing import Any

from remote_agent.profiles.base import ProfileContext, ProfileHandler

from .actions import HostInfoAction, NetworkAction, ProcessesAction, ResourcesAction
from .common import platform_id


class SystemObserveV1Profile(ProfileHandler):
    profile_id = "system_observe.v1"

    def __init__(self, context: ProfileContext):
        super().__init__(
            context,
            {
                "get_host_info": HostInfoAction(),
                "get_resources": ResourcesAction(context.workdir),
                "get_processes": ProcessesAction(),
                "get_network": NetworkAction(),
            },
        )

    def runtime_metadata(self) -> dict[str, Any]:
        return {
            "scope": "host",
            "mode": "read_only",
            "platform": platform_id(),
            "actions": sorted(self.actions),
        }
