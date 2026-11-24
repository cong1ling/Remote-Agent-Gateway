from __future__ import annotations

import re
from typing import Any

from remote_agent.core.version import (
    AGENT_VERSION as CURRENT_AGENT_VERSION,
    CORE_VERSION as CURRENT_CORE_VERSION,
    PROTOCOL_VERSION as AGENT_PROTOCOL_VERSION,
)

_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_BUILD_ID_PATTERN = re.compile(r"^[0-9A-Za-z._:-]{1,80}$")
_IDENTIFIER_PATTERN = re.compile(r"^[0-9A-Za-z._:-]{1,80}$")
_DEVICE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

ACTION_INTERCEPTION_MODES = frozenset({"full", "limited", "none"})
RUNTIME_STATUSES = frozenset({"available", "unavailable", "error"})
RUNTIME_MODES = frozenset({"managed_full", "managed_limited", "unmanaged"})

DEFAULT_AGENT_FEATURES: dict[str, Any] = {
    "task_protocol": 0,
    "event_stream": False,
    "runtime_adapter": False,
    "planned_actions": False,
    "planned_l1_policy": False,
    "health_step_cancel": False,
    "action_interception": "none",
    "evidence": False,
}


def validate_agent_version(value: object, protocol_version: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Agent 缺少版本信息，请重新下载最新版 Agent")
    agent_version = value.get("agent_version")
    core_version = value.get("core_version")
    declared_protocol = value.get("protocol_version")
    build_id = value.get("build_id")
    if not isinstance(agent_version, str) or not _VERSION_PATTERN.fullmatch(agent_version):
        raise ValueError("Agent agent_version 无效")
    if not isinstance(core_version, int) or isinstance(core_version, bool) or core_version < 1:
        raise ValueError("Agent core_version 无效")
    if declared_protocol != protocol_version:
        raise ValueError("Agent version.protocol_version 与握手协议版本不一致")
    if not isinstance(build_id, str) or not _BUILD_ID_PATTERN.fullmatch(build_id):
        raise ValueError("Agent build_id 无效")
    return {
        "agent_version": agent_version,
        "core_version": core_version,
        "protocol_version": protocol_version,
        "build_id": build_id,
    }


def validate_device_declaration(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Agent device 必须是对象")
    device_type = value.get("device_type")
    platform = value.get("platform")
    version = value.get("platform_version")
    if not isinstance(device_type, str) or not _DEVICE_TYPE_PATTERN.fullmatch(device_type):
        raise ValueError("Agent device.device_type 无效")
    if platform not in ("windows", "macos", "linux", "posix", "unknown"):
        raise ValueError("Agent device.platform 无效")
    if version is not None and (
        not isinstance(version, str) or not version.strip() or len(version) > 128 or not version.isprintable()
    ):
        raise ValueError("Agent device.platform_version 无效")
    return {
        "device_type": device_type,
        "platform": platform,
        "platform_version": version.strip() if version is not None else None,
    }


def validate_agent_features(value: object) -> dict[str, Any]:
    """Normalize optional feature declarations without breaking legacy Agents."""
    if value is None:
        return dict(DEFAULT_AGENT_FEATURES)
    if not isinstance(value, dict):
        raise ValueError("Agent features 必须是对象")

    task_protocol = value.get("task_protocol", 0)
    if (
        not isinstance(task_protocol, int)
        or isinstance(task_protocol, bool)
        or task_protocol < 0
        or task_protocol > 1
    ):
        raise ValueError("Agent features.task_protocol 无效")

    normalized: dict[str, Any] = {
        "task_protocol": task_protocol,
        "event_stream": value.get("event_stream", False),
        "runtime_adapter": value.get("runtime_adapter", False),
        "planned_actions": value.get("planned_actions", False),
        "planned_l1_policy": value.get("planned_l1_policy", False),
        "health_step_cancel": value.get("health_step_cancel", False),
        "action_interception": value.get("action_interception", "none"),
        "evidence": value.get("evidence", False),
    }
    for field_name in ("event_stream", "runtime_adapter", "planned_actions", "planned_l1_policy", "health_step_cancel", "evidence"):
        if not isinstance(normalized[field_name], bool):
            raise ValueError(f"Agent features.{field_name} 无效")
    if normalized["action_interception"] not in ACTION_INTERCEPTION_MODES:
        raise ValueError("Agent features.action_interception 无效")
    if task_protocol == 1 and not normalized["event_stream"]:
        raise ValueError("启用 task_protocol 时必须启用 event_stream")
    if normalized["planned_actions"] and task_protocol != 1:
        raise ValueError("planned_actions 需要 Task Protocol v1")
    if normalized["health_step_cancel"] and task_protocol != 1:
        raise ValueError("health_step_cancel 需要 Task Protocol v1")
    if normalized["planned_l1_policy"] and not normalized["planned_actions"]:
        raise ValueError("planned_l1_policy 需要 planned_actions")
    return normalized


def validate_runtime_declarations(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Agent runtimes 必须是数组")
    if len(value) > 16:
        raise ValueError("Agent runtimes 不能超过 16 项")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for runtime in value:
        if not isinstance(runtime, dict):
            raise ValueError("Agent runtime 声明必须是对象")
        provider_id = runtime.get("provider_id")
        runtime_version = runtime.get("runtime_version")
        adapter_version = runtime.get("adapter_version")
        status = runtime.get("status", "available")
        mode = runtime.get("mode")
        if (
            not isinstance(provider_id, str)
            or not _IDENTIFIER_PATTERN.fullmatch(provider_id)
            or provider_id in seen
        ):
            raise ValueError("Agent runtime.provider_id 无效或重复")
        if (
            not isinstance(runtime_version, str)
            or not runtime_version.strip()
            or len(runtime_version) > 80
        ):
            raise ValueError("Agent runtime.runtime_version 无效")
        if (
            not isinstance(adapter_version, str)
            or not adapter_version.strip()
            or len(adapter_version) > 80
        ):
            raise ValueError("Agent runtime.adapter_version 无效")
        if status not in RUNTIME_STATUSES:
            raise ValueError("Agent runtime.status 无效")
        if mode is not None and mode not in RUNTIME_MODES:
            raise ValueError("Agent runtime.mode 无效")
        supported_profiles = runtime.get("supported_profiles", [])
        if not isinstance(supported_profiles, list) or not all(
            isinstance(profile, str) and _IDENTIFIER_PATTERN.fullmatch(profile)
            for profile in supported_profiles
        ):
            raise ValueError("Agent runtime.supported_profiles 无效")
        metadata = runtime.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Agent runtime.metadata 必须是对象")
        normalized_runtime = {
            "provider_id": provider_id,
            "runtime_version": runtime_version.strip(),
            "adapter_version": adapter_version.strip(),
            "status": status,
            "supported_profiles": list(dict.fromkeys(supported_profiles)),
            "metadata": dict(metadata),
        }
        if mode is not None:
            normalized_runtime["mode"] = mode
        normalized.append(normalized_runtime)
        seen.add(provider_id)
    return normalized


def agent_version_status(version: dict[str, Any]) -> str:
    if not version:
        return "unknown"
    if version.get("protocol_version") != AGENT_PROTOCOL_VERSION:
        return "incompatible"
    agent_version = version_tuple(version.get("agent_version"))
    current_version = version_tuple(CURRENT_AGENT_VERSION)
    core_version = version.get("core_version")
    if agent_version is None or not isinstance(core_version, int):
        return "unknown"
    if agent_version > current_version or core_version > CURRENT_CORE_VERSION:
        return "newer"
    if agent_version < current_version or core_version < CURRENT_CORE_VERSION:
        return "update_available"
    return "current"


def version_tuple(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        return None
    return tuple(int(part) for part in value.split("."))
