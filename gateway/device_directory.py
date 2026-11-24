from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .profiles import PROFILE_REGISTRY


Platform = Literal["windows", "macos", "linux", "posix", "unknown"]
DeviceLabel = Annotated[str, Field(max_length=32, pattern=r"^[a-z0-9][a-z0-9._-]*$")]
CapabilityId = Annotated[
    str, Field(max_length=129, pattern=r"^[a-z][a-z0-9_.-]*(/[a-z][a-z0-9_.-]*)?$")
]


def _device(agent: dict[str, Any]) -> dict[str, Any]:
    if agent["device"]:
        return agent["device"]
    declared_platforms = {
        declaration["runtime"].get(field)
        for declaration in agent["profile_declarations"]
        for profile_id, field in (("shell.v1", "os"), ("system_observe.v1", "platform"))
        if declaration["id"] == profile_id
        and declaration["runtime"].get(field) in ("windows", "macos", "linux", "posix")
    }
    platform = next(iter(declared_platforms)) if len(declared_platforms) == 1 else "unknown"
    return {"device_type": None, "platform": platform, "platform_version": None}


def _capability_ids(agent: dict[str, Any]) -> list[str]:
    capabilities = set()
    for profile_id in agent["profiles"]:
        profile = PROFILE_REGISTRY.get(profile_id)
        if profile is None:
            continue
        capabilities.add(profile_id)
        for action in profile["actions"]:
            capabilities.add(f"{profile_id}/{action['name']}")
            if action.get("plan_operation"):
                capabilities.add(action["plan_operation"])
    return sorted(capabilities)


class DeviceFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform | None = None
    environment: DeviceLabel | None = None
    tags: list[DeviceLabel] = Field(default_factory=list, max_length=12)
    capability: CapabilityId | None = None

    def matches(self, agent: dict[str, Any]) -> bool:
        return (
            (self.platform is None or self.platform == _device(agent)["platform"])
            and (self.environment is None or self.environment == agent["environment"])
            and set(self.tags).issubset(agent["tags"])
            and (self.capability is None or self.capability in _capability_ids(agent))
        )


def directory_fields(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "device": _device(agent),
        "environment": agent["environment"],
        "tags": agent["tags"],
        "capability_ids": _capability_ids(agent),
        "trust_level": "owner_enrolled",
    }
