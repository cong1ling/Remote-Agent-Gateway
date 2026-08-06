from __future__ import annotations

from copy import deepcopy
from typing import Any


SHELL_MAX_CAPTURE_BYTES = 2 * 1024 * 1024
SHELL_TRUNCATION_MARKER = "\n[output truncated by remote agent]\n"

PROFILE_REGISTRY: dict[str, dict[str, Any]] = {
    "shell.v1": {
        "id": "shell.v1",
        "name": "远程 Shell",
        "description": (
            "通过 Agent 启动时自动检测并声明的系统 Shell 执行命令，"
            "并返回标准输出、错误输出和退出码。"
        ),
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
                "parameters": [
                    {
                        "name": "command",
                        "type": "string",
                        "required": True,
                        "description": "符合该 Agent 所声明 Shell 方言的命令字符串。",
                    },
                    {
                        "name": "timeout",
                        "type": "integer",
                        "required": False,
                        "default": 300,
                        "minimum": 1,
                        "description": "命令超时秒数，最大值由网关策略限制。",
                    },
                ],
                "returns": [
                    {"name": "stdout", "type": "string"},
                    {"name": "stderr", "type": "string"},
                    {"name": "exit_code", "type": "integer"},
                ],
            }
        ],
        "limits": {
            "capture_bytes_per_stream": SHELL_MAX_CAPTURE_BYTES,
            "truncation_marker": SHELL_TRUNCATION_MARKER.strip(),
            "timeout_terminates_process_tree": True,
        },
    }
}

SHELL_DIALECTS: dict[str, set[str]] = {
    "powershell": {"windows"},
    "cmd": {"windows"},
    "posix-sh": {"linux", "macos", "posix"},
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


def validate_profile_declarations(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("profiles 必须是结构化数组，请重新下载新版 Agent")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for declaration in value:
        if not isinstance(declaration, dict):
            raise ValueError("Agent Profile 声明格式已升级，请重新下载新版 Agent")
        profile_id = declaration.get("id")
        if not isinstance(profile_id, str) or profile_id not in PROFILE_REGISTRY:
            raise ValueError(f"不支持的 Profile: {profile_id}")
        if profile_id in seen:
            continue
        runtime = declaration.get("runtime")
        if profile_id == "shell.v1":
            runtime = _validate_shell_runtime(runtime)
        elif not isinstance(runtime, dict):
            raise ValueError(f"Profile {profile_id} 缺少 runtime 声明")
        normalized.append({"id": profile_id, "runtime": runtime})
        seen.add(profile_id)
    if not normalized:
        raise ValueError("Agent 至少需要实现一个标准 Profile")
    return normalized


def _validate_shell_runtime(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("shell.v1 必须声明运行时 Shell")
    operating_system = value.get("os")
    dialect = value.get("dialect")
    executable = value.get("executable")
    if not isinstance(operating_system, str) or operating_system not in {
        "windows",
        "linux",
        "macos",
        "posix",
    }:
        raise ValueError("shell.v1 runtime.os 无效")
    if not isinstance(dialect, str) or dialect not in SHELL_DIALECTS:
        raise ValueError("shell.v1 runtime.dialect 无效")
    if operating_system not in SHELL_DIALECTS[dialect]:
        raise ValueError("shell.v1 的操作系统与 Shell 方言不匹配")
    if not isinstance(executable, str) or not executable.strip() or len(executable) > 128:
        raise ValueError("shell.v1 runtime.executable 无效")
    return {
        "os": operating_system,
        "dialect": dialect,
        "executable": executable.strip(),
    }


def profile_ids(declarations: list[dict[str, Any]]) -> list[str]:
    return [declaration["id"] for declaration in declarations]


def describe_profile_declarations(
    declarations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    descriptions: list[dict[str, Any]] = []
    for declaration in declarations:
        profile_id = declaration.get("id")
        if profile_id not in PROFILE_REGISTRY:
            continue
        description = deepcopy(PROFILE_REGISTRY[profile_id])
        description["runtime"] = deepcopy(declaration.get("runtime", {}))
        descriptions.append(description)
    return descriptions


def profile_names(profile_ids: list[str]) -> list[str]:
    return [
        PROFILE_REGISTRY[profile]["name"]
        for profile in profile_ids
        if profile in PROFILE_REGISTRY
    ]


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
                "actions": deepcopy(definition["actions"]),
                "limits": deepcopy(definition.get("limits", {})),
            }
        )
    return catalog


def validate_profile_result(
    profile_id: str, action: str, result: dict[str, Any]
) -> dict[str, Any]:
    if profile_id != "shell.v1" or action != "exec":
        raise ValueError(f"不支持的 Profile 操作: {profile_id}/{action}")
    stdout = result.get("stdout")
    stderr = result.get("stderr")
    exit_code = result.get("exit_code")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise ValueError("Agent 返回的 stdout/stderr 必须是字符串")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ValueError("Agent 返回的 exit_code 必须是整数")
    # Invalid source bytes may expand to the three-byte UTF-8 replacement character.
    maximum = SHELL_MAX_CAPTURE_BYTES * 3 + len(SHELL_TRUNCATION_MARKER.encode("utf-8"))
    if len(stdout.encode("utf-8")) > maximum or len(stderr.encode("utf-8")) > maximum:
        raise ValueError("Agent 返回的 Shell 输出超过协议限制")
    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}
