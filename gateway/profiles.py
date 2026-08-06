from __future__ import annotations

from typing import Any


PROFILE_REGISTRY: dict[str, dict[str, Any]] = {
    "shell.v1": {
        "id": "shell.v1",
        "name": "远程 Shell",
        "description": "在远程环境中执行 Shell 命令，并返回标准输出、错误输出和退出码。",
        "status": "stable",
        "implementations": {
            "python": {
                "source": "shell_v1.py",
                "minimum_python": "3.11",
            }
        },
        "actions": [
            {
                "name": "exec",
                "description": "执行一条 Shell 命令并等待其完成。",
                "parameters": ["command", "timeout"],
            }
        ],
    }
}


def validate_profiles(profiles: object) -> list[str]:
    if not isinstance(profiles, list):
        raise ValueError("profiles 必须是数组")
    normalized: list[str] = []
    for profile in profiles:
        if not isinstance(profile, str) or profile not in PROFILE_REGISTRY:
            raise ValueError(f"不支持的 Profile: {profile}")
        if profile not in normalized:
            normalized.append(profile)
    if not normalized:
        raise ValueError("Agent 至少需要实现一个标准 Profile")
    return normalized


def describe_profiles(profile_ids: list[str]) -> list[dict[str, Any]]:
    return [PROFILE_REGISTRY[profile] for profile in profile_ids if profile in PROFILE_REGISTRY]


def profile_names(profile_ids: list[str]) -> list[str]:
    return [PROFILE_REGISTRY[profile]["name"] for profile in profile_ids if profile in PROFILE_REGISTRY]


def public_profile_catalog() -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for definition in PROFILE_REGISTRY.values():
        catalog.append(
            {
                "id": definition["id"],
                "name": definition["name"],
                "description": definition["description"],
                "status": definition["status"],
                "runtimes": sorted(definition["implementations"]),
                "actions": definition["actions"],
            }
        )
    return catalog
