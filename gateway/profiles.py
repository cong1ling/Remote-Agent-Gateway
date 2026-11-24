from __future__ import annotations

import re
from copy import deepcopy
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from remote_agent.core.limits import MAX_CONCURRENT_REQUESTS_PER_AGENT
from remote_agent.profiles.common.managed_contracts import (
    BUILD_BACKEND, BUILD_PROFILE, IDENTIFIER, LOCAL_PLAN_PROFILES, SERVICE_BACKEND, SERVICE_PROFILE,
)

from .agent_protocol import version_tuple

SHELL_MAX_CAPTURE_BYTES = 2 * 1024 * 1024
SHELL_TRUNCATION_MARKER = "\n[output truncated by remote agent]\n"
WORKSPACE_MAX_FILE_BYTES = 8 * 1024 * 1024
WORKSPACE_MAX_READ_LINES = 2000
WORKSPACE_MAX_READ_BYTES = 50 * 1024
WORKSPACE_MAX_WRITE_BYTES = 2 * 1024 * 1024


def _plan_parameter(name: str, description: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "type": "string", "description": description, "required": required}


def _plan_action(
    name: str, operation: str, risk: str, description: str, parameters: list[dict[str, Any]],
    returns: list[dict[str, str]], errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "name": name, "mcp_tool": "create_operation_plan", "plan_operation": operation, "risk_level": risk,
        "description": description, "parameters": parameters, "returns": returns, "errors": errors,
    }


_SERVICE_ID_PARAM = _plan_parameter("service_id", "设备本地登记的服务 ID。")
_SERVICE_STATUS_FIELDS = [
    {"name": "generation", "type": "string", "description": "服务进程代际。"},
    {"name": "state", "type": "string", "description": "ready、degraded、stopped 或 starting。"},
    {"name": "recent_events", "type": "array", "description": "最近的类型化诊断记录，不含自由文本和凭据。"},
]
_SOURCE_SHA256_FIELD = {"name": "source_sha256", "type": "string", "description": "本地源码 ZIP 的完整 SHA-256。"}

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
                "source": "shell_v1",
                "minimum_python": "3.11",
            }
        },
        "actions": [
            {
                "name": "exec",
                "mcp_tool": "remote_exec",
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
                    {
                        "name": "stdout",
                        "type": "string",
                        "description": "命令的标准输出，使用替换字符处理无法解码的字节。",
                    },
                    {
                        "name": "stderr",
                        "type": "string",
                        "description": "命令的错误输出，使用替换字符处理无法解码的字节。",
                    },
                    {
                        "name": "exit_code",
                        "type": "integer",
                        "description": "Shell 进程退出码；通常 0 表示成功。",
                    },
                ],
                "errors": [
                    {
                        "name": "timeout",
                        "description": "超过 timeout 后终止本次命令的整个进程树。",
                    },
                    {
                        "name": "cancelled",
                        "description": "网关取消、连接断开或 Agent 离线时终止任务。",
                    },
                    {
                        "name": "invalid_result",
                        "description": "返回值不符合契约时由网关拒绝结果。",
                    },
                ],
            }
        ],
        "runtime_contract": {
            "selection": "Agent 启动时检测一次可用 Shell，并在该进程生命周期内固定使用。",
            "fields": [
                {"name": "os", "type": "string", "description": "远程设备操作系统。"},
                {
                    "name": "dialect",
                    "type": "string",
                    "description": "命令必须遵循的 Shell 方言。",
                },
                {
                    "name": "executable",
                    "type": "string",
                    "description": "Agent 实际选择的 Shell 可执行文件名。",
                },
            ],
            "dialects": [
                {
                    "id": "powershell",
                    "label": "PowerShell",
                    "systems": ["Windows"],
                    "detection": ["pwsh", "powershell.exe"],
                    "example": "Get-Location",
                },
                {
                    "id": "cmd",
                    "label": "CMD",
                    "systems": ["Windows"],
                    "detection": ["cmd.exe"],
                    "example": "cd",
                },
                {
                    "id": "posix-sh",
                    "label": "POSIX Shell",
                    "systems": ["Linux", "macOS"],
                    "detection": ["/bin/sh", "PATH 中的 sh"],
                    "example": "pwd",
                },
            ],
        },
        "limits": {
            "capture_bytes_per_stream": SHELL_MAX_CAPTURE_BYTES,
            "truncation_marker": SHELL_TRUNCATION_MARKER.strip(),
            "timeout_terminates_process_tree": True,
            "max_concurrent_requests_per_agent": MAX_CONCURRENT_REQUESTS_PER_AGENT,
        },
    },
    "workspace.v1": {
        "id": "workspace.v1",
        "name": "远程工作区",
        "description": (
            "在 Agent 启动时指定的工作目录内，以 UTF-8 文本契约读取、整体写入和精确编辑文件。"
        ),
        "status": "stable",
        "minimum_agent_version": "0.2.1",
        "implementations": {
            "python": {
                "source": "workspace_v1",
                "minimum_python": "3.11",
            }
        },
        "actions": [
            {
                "name": "read",
                "mcp_tool": "remote_read_file",
                "description": "读取工作目录内的 UTF-8 文本文件，可按行分段。",
                "parameters": [
                    {
                        "name": "path",
                        "type": "string",
                        "required": True,
                        "description": "工作目录下的相对路径，使用 / 分隔。",
                    },
                    {
                        "name": "offset",
                        "type": "integer",
                        "required": False,
                        "default": 1,
                        "minimum": 1,
                        "description": "从 1 开始的起始行号。",
                    },
                    {
                        "name": "limit",
                        "type": "integer",
                        "required": False,
                        "default": WORKSPACE_MAX_READ_LINES,
                        "minimum": 1,
                        "maximum": WORKSPACE_MAX_READ_LINES,
                        "description": "最多读取的行数，超出时返回 truncated。",
                    },
                ],
                "returns": [
                    {"name": "path", "type": "string", "description": "规范化后的工作区相对路径。"},
                    {"name": "content", "type": "string", "description": "读取到的 UTF-8 文本。"},
                    {"name": "offset", "type": "integer", "description": "实际起始行号。"},
                    {"name": "lines", "type": "integer", "description": "本次返回的行数。"},
                    {"name": "total_lines", "type": "integer", "description": "文件总行数。"},
                    {"name": "truncated", "type": "boolean", "description": "是否因范围或输出上限未返回完整内容。"},
                    {"name": "size", "type": "integer", "description": "文件 UTF-8 字节数。"},
                    {"name": "sha256", "type": "string", "description": "整个文件内容的 SHA-256，用于后续编辑冲突检测。"},
                ],
                "errors": [
                    {"name": "invalid_path", "description": "path 不是工作目录下的相对路径。"},
                    {"name": "not_found", "description": "目标文件不存在。"},
                    {"name": "invalid_encoding", "description": "文件不是有效的 UTF-8 文本。"},
                    {"name": "too_large", "description": "文件或读取范围超过协议限制。"},
                ],
            },
            {
                "name": "write",
                "mcp_tool": "remote_write_file",
                "description": "按显式文件版本前置条件，原子地创建或整体覆盖 UTF-8 文本文件。",
                "parameters": [
                    {
                        "name": "path",
                        "type": "string",
                        "required": True,
                        "description": "工作目录下的相对路径，使用 / 分隔。",
                    },
                    {
                        "name": "content",
                        "type": "string",
                        "required": True,
                        "description": "要写入的 UTF-8 文本；写入会整体替换文件。",
                    },
                    {
                        "name": "expected_sha256",
                        "type": "string",
                        "required": True,
                        "nullable": True,
                        "description": "传 null 时只允许新建；传当前文件 SHA-256 时只允许覆盖该版本。",
                    },
                ],
                "returns": [
                    {"name": "path", "type": "string", "description": "规范化后的工作区相对路径。"},
                    {"name": "bytes_written", "type": "integer", "description": "写入的 UTF-8 字节数。"},
                    {"name": "sha256", "type": "string", "description": "写入后整个文件内容的 SHA-256。"},
                    {"name": "changed", "type": "boolean", "description": "文件内容是否实际发生变化。"},
                ],
                "errors": [
                    {"name": "invalid_path", "description": "path 不是工作目录下的相对路径。"},
                    {"name": "permission_denied", "description": "目标或父目录不可写。"},
                    {"name": "conflict", "description": "文件存在性或当前版本与写入前置条件不一致。"},
                    {"name": "too_large", "description": "写入内容超过协议限制。"},
                ],
            },
            {
                "name": "edit",
                "mcp_tool": "remote_edit_file",
                "description": "按当前文件版本在工作目录内精确替换唯一一处文本，并返回 diff。",
                "parameters": [
                    {
                        "name": "path",
                        "type": "string",
                        "required": True,
                        "description": "工作目录下的相对路径，使用 / 分隔。",
                    },
                    {
                        "name": "old_text",
                        "type": "string",
                        "required": True,
                        "description": "必须在文件中恰好匹配一次的原文本。",
                    },
                    {
                        "name": "new_text",
                        "type": "string",
                        "required": True,
                        "description": "替换后的文本。",
                    },
                    {
                        "name": "expected_sha256",
                        "type": "string",
                        "required": True,
                        "description": "调用 read 得到的当前文件版本；不匹配时拒绝修改。",
                    },
                ],
                "returns": [
                    {"name": "path", "type": "string", "description": "规范化后的工作区相对路径。"},
                    {"name": "changed", "type": "boolean", "description": "文件内容是否发生变化。"},
                    {"name": "diff", "type": "string", "description": "修改前后的 unified diff。"},
                    {"name": "diff_truncated", "type": "boolean", "description": "diff 是否因输出上限被截断。"},
                    {"name": "first_changed_line", "type": "integer", "description": "首个修改行号。"},
                    {"name": "sha256", "type": "string", "description": "修改后整个文件内容的 SHA-256。"},
                ],
                "errors": [
                    {"name": "invalid_path", "description": "path 不是工作目录下的相对路径。"},
                    {"name": "conflict", "description": "文件版本已变化，拒绝覆盖新内容。"},
                    {"name": "not_found", "description": "old_text 未找到或文件不存在。"},
                    {"name": "ambiguous_match", "description": "old_text 匹配了多处内容，拒绝修改。"},
                    {"name": "invalid_encoding", "description": "文件不是有效的 UTF-8 文本。"},
                ],
            },
        ],
        "runtime_contract": {
            "selection": "Agent 启动时固定一个工作目录；所有 Action 只能访问该目录下的相对路径。",
            "fields": [
                {"name": "scope", "type": "string", "description": "固定为 workdir，不暴露绝对路径。"},
                {"name": "path_format", "type": "string", "description": "固定使用 relative-posix 路径。"},
                {"name": "encoding", "type": "string", "description": "固定使用 UTF-8 文本。"},
                {"name": "max_file_bytes", "type": "integer", "description": "单文件最大字节数。"},
            ],
        },
        "limits": {
            "max_file_bytes": WORKSPACE_MAX_FILE_BYTES,
            "max_read_lines": WORKSPACE_MAX_READ_LINES,
            "max_read_bytes": WORKSPACE_MAX_READ_BYTES,
            "max_write_bytes": WORKSPACE_MAX_WRITE_BYTES,
            "atomic_write": True,
            "optimistic_concurrency": True,
        },
    },
    "system_observe.v1": {
        "id": "system_observe.v1",
        "name": "系统只读观察",
        "description": (
            "通过固定、只读的观察 Action 获取主机、资源、进程和网络元数据，"
            "不接受任意命令或远程探测参数。"
        ),
        "status": "preview",
        "minimum_agent_version": "0.2.3",
        "implementations": {
            "python": {
                "source": "system_observe_v1",
                "minimum_python": "3.11",
            }
        },
        "actions": [
            {
                "name": "get_host_info",
                "mcp_tool": "remote_get_host_info",
                "description": "读取主机操作系统、架构、主机名和 Agent 运行时信息。",
                "parameters": [],
                "returns": [
                    {"name": "os", "type": "string", "description": "归一化操作系统 ID。"},
                    {"name": "hostname", "type": "string", "description": "设备主机名。"},
                    {"name": "architecture", "type": "string", "description": "设备 CPU 架构。"},
                    {"name": "observed_at", "type": "string", "description": "UTC 观察时间。"},
                    {
                        "name": "privileges", "type": "object", "required": False,
                        "description": "is_admin 是 Windows 当前令牌的已启用管理员组状态，is_root 是 POSIX 有效 UID 是否为 0；null 为不适用或未知。",
                    },
                    {
                        "name": "linux_distribution", "type": "object", "required": False, "nullable": True,
                        "description": "仅 Linux：os-release 中的 id 和可空 version_id；无法读取为 null。",
                    },
                    {
                        "name": "linux_tools", "type": "object", "required": False,
                        "description": "仅 Linux：sudo_on_path、docker_on_path、podman_on_path。只表示 PATH 上找到可执行文件，不测试权限、守护进程或远程访问。",
                    },
                ],
                "errors": [{"name": "observe_failed", "description": "读取主机信息失败。"}],
            },
            {
                "name": "get_resources",
                "mcp_tool": "remote_get_resources",
                "description": "读取 CPU、内存和 Agent 工作目录所在磁盘的资源状态。",
                "parameters": [],
                "returns": [
                    {"name": "cpu", "type": "object", "description": "CPU 数量和可用负载信息。"},
                    {
                        "name": "memory", "type": "object", "nullable": True,
                        "description": (
                            "物理内存字节和 utilization_percent。macOS 用匿名页、wired 页和实际压缩页估算；"
                            "采样不可用时没有利用率，不代表健康。"
                        ),
                    },
                    {"name": "disks", "type": "array", "description": "磁盘空间统计。"},
                    {"name": "observed_at", "type": "string", "description": "UTC 观察时间。"},
                ],
                "errors": [{"name": "observe_failed", "description": "读取资源信息失败。"}],
            },
            {
                "name": "get_processes",
                "mcp_tool": "remote_get_processes",
                "description": "读取有界的本机进程摘要，不返回命令参数或环境变量。",
                "parameters": [],
                "returns": [
                    {"name": "available", "type": "boolean", "description": "进程观察是否可用。"},
                    {"name": "processes", "type": "array", "description": "最多 100 个进程摘要。"},
                    {"name": "truncated", "type": "boolean", "description": "是否超过返回上限。"},
                    {"name": "observed_at", "type": "string", "description": "UTC 观察时间。"},
                ],
                "errors": [{"name": "observe_failed", "description": "读取进程信息失败。"}],
            },
            {
                "name": "get_network",
                "mcp_tool": "remote_get_network",
                "description": "读取本机接口、地址和 TCP 监听表；不做 DNS、连接或端口扫描。",
                "parameters": [],
                "returns": [
                    {"name": "hostname", "type": "string", "description": "设备主机名。"},
                    {"name": "interfaces", "type": "array", "description": "网络接口名称和索引。"},
                    {"name": "addresses", "type": "array", "description": "本机接口上的数字 IP 地址。"},
                    {"name": "observed_at", "type": "string", "description": "UTC 观察时间。"},
                    {
                        "name": "tcp_listeners", "type": "object", "required": False, "nullable": True,
                        "description": (
                            "Agent 可见的本机 IPv4/IPv6 TCP 监听端口：ports 去重升序，最多 128 个，"
                            "超过时 truncated 为 true。null 表示未知，空列表表示采样时未发现监听。"
                        ),
                    },
                ],
                "errors": [{"name": "observe_failed", "description": "读取网络信息失败。"}],
            },
        ],
        "runtime_contract": {
            "selection": "Agent 启动时绑定当前设备平台；所有 Action 固定为只读观察。",
            "fields": [
                {"name": "scope", "type": "string", "description": "固定为 host。"},
                {"name": "mode", "type": "string", "description": "固定为 read_only。"},
                {"name": "platform", "type": "string", "description": "windows、macos、linux 或 posix。"},
                {"name": "actions", "type": "array", "description": "完整的四个观察 Action。"},
            ],
        },
        "limits": {
            "processes": 100,
            "network_entries": 128,
            "tcp_listening_ports": 128,
            "read_only": True,
            "action_interception": "full",
        },
    },
    SERVICE_PROFILE: {
        "id": SERVICE_PROFILE, "name": "登记应用诊断与恢复", "status": "preview", "plan_only": True,
        "description": "诊断和恢复设备本地登记的应用服务。不允许指定地址、系统服务或任意命令。",
        "minimum_agent_version": "0.2.3",
        "implementations": {"python": {"source": "service_control_v1", "minimum_python": "3.11"}},
        "actions": [
            _plan_action(
                "inspect", "service.inspect", "L0", "读取服务状态和限定的诊断记录。",
                [_SERVICE_ID_PARAM], _SERVICE_STATUS_FIELDS,
                [{"name": "service.unavailable", "description": "登记的本地服务管理接口不可访问。"}],
            ),
            _plan_action(
                "restart", "service.restart", "L2", "审批后恢复当前故障代际，并验证新的代际达到就绪状态。",
                [_SERVICE_ID_PARAM, _plan_parameter("expected_generation", "最近一次观察到的故障代际。")],
                [*_SERVICE_STATUS_FIELDS, {"name": "verified", "type": "boolean", "description": "新代际已就绪。"}],
                [{"name": "service.generation_changed", "description": "服务已变化；重新观察，不能重放。"}],
            ),
        ],
        "runtime_contract": {
            "selection": "以设备端 --service-config 显式绑定，只访问本机回环地址。",
            "fields": [
                {"name": "backend", "type": "string", "description": SERVICE_BACKEND},
                {"name": "service_ids", "type": "array", "description": "设备所有者登记的服务别名。"},
            ],
        },
        "limits": {"service_count": 16, "events": 12, "restart_requires_approval": True},
    },
    BUILD_PROFILE: {
        "id": BUILD_PROFILE, "name": "固定源码包构建验证", "status": "preview", "plan_only": True,
        "description": "核验设备预置的可信 Python 源码 ZIP；只通过受控计划完成编译检查和单元测试。",
        "minimum_agent_version": "0.2.3",
        "implementations": {"python": {"source": "build_verify_v1", "minimum_python": "3.11"}},
        "actions": [
            _plan_action(
                "source", "build.source", "L0", "读取源码包 SHA-256；可要求吻合已批准的摘要。",
                [_plan_parameter("source_sha256", "要求吻合的可选小写 SHA-256。", required=False)],
                [_SOURCE_SHA256_FIELD],
                [{"name": "build.source_mismatch", "description": "设备当前源码包与计划不一致。"}],
            ),
            _plan_action(
                "verify", "build.verify", "L1", "人工审批后对完全相同的源码字节运行固定编译检查和 unittest。",
                [_plan_parameter("source_sha256", "必须吻合的源码 ZIP 小写 SHA-256。")],
                [_SOURCE_SHA256_FIELD,
                 {"name": "compiled", "type": "boolean", "description": "Python 编译检查是否通过。"},
                 {"name": "tests_run", "type": "integer", "description": "运行的测试数和失败、错误、跳过计数。"}],
                [{"name": "build.source_mismatch", "description": "摘要不符，禁止启动验证进程。"}],
            ),
        ],
        "runtime_contract": {
            "selection": "所有者以 --build-archive 在工作区外预置单一可信源码 ZIP。",
            "fields": [{"name": "backend", "type": "string", "description": BUILD_BACKEND}],
        },
        "limits": {"verify_requires_approval": True, "remote_commands": False, "raw_output": False},
    },
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
    if "shell.v1" in normalized and LOCAL_PLAN_PROFILES.intersection(normalized):
        raise ValueError("登记服务或构建 Profile 不得与旧版直连 Shell 混装")
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
        elif profile_id == "workspace.v1":
            runtime = _validate_workspace_runtime(runtime)
        elif profile_id == "system_observe.v1":
            runtime = _validate_system_observe_runtime(runtime)
        elif profile_id in LOCAL_PLAN_PROFILES:
            runtime = _validate_local_plan_runtime(profile_id, runtime)
        elif not isinstance(runtime, dict):
            raise ValueError(f"Profile {profile_id} 缺少 runtime 声明")
        normalized.append({"id": profile_id, "runtime": runtime})
        seen.add(profile_id)
    validate_profiles(list(seen))
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


def _validate_workspace_runtime(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("workspace.v1 必须声明工作区运行时")
    if value.get("scope") != "workdir":
        raise ValueError("workspace.v1 runtime.scope 必须是 workdir")
    if value.get("path_format") != "relative-posix":
        raise ValueError("workspace.v1 runtime.path_format 必须是 relative-posix")
    if value.get("encoding") != "utf-8":
        raise ValueError("workspace.v1 runtime.encoding 必须是 utf-8")
    if value.get("max_file_bytes") != WORKSPACE_MAX_FILE_BYTES:
        raise ValueError("workspace.v1 runtime.max_file_bytes 与协议限制不一致")
    return {
        "scope": "workdir",
        "path_format": "relative-posix",
        "encoding": "utf-8",
        "max_file_bytes": WORKSPACE_MAX_FILE_BYTES,
    }


def _validate_system_observe_runtime(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("system_observe.v1 必须声明运行时")
    if value.get("scope") != "host":
        raise ValueError("system_observe.v1 runtime.scope 必须是 host")
    if value.get("mode") != "read_only":
        raise ValueError("system_observe.v1 runtime.mode 必须是 read_only")
    operating_system = value.get("platform")
    if operating_system not in {"windows", "macos", "linux", "posix"}:
        raise ValueError("system_observe.v1 runtime.platform 无效")
    actions = value.get("actions")
    required_actions = {
        "get_host_info",
        "get_resources",
        "get_processes",
        "get_network",
    }
    if (
        not isinstance(actions, list)
        or not all(isinstance(action, str) for action in actions)
        or not required_actions.issubset(actions)
    ):
        raise ValueError("system_observe.v1 runtime.actions 不完整")
    return {
        "scope": "host",
        "mode": "read_only",
        "platform": operating_system,
        "actions": sorted(set(actions)),
    }


def _validate_local_plan_runtime(profile_id: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Profile {profile_id} 缺少 runtime 声明")
    if profile_id == BUILD_PROFILE:
        if value != {"backend": BUILD_BACKEND}:
            raise ValueError("构建验证 Profile 声明了未知后端")
        return {"backend": BUILD_BACKEND}
    service_ids = value.get("service_ids")
    if (
        set(value) != {"backend", "service_ids"} or value.get("backend") != SERVICE_BACKEND
        or not isinstance(service_ids, list) or not 1 <= len(service_ids) <= 16
        or not all(isinstance(item, str) and IDENTIFIER.fullmatch(item) for item in service_ids)
        or len(set(service_ids)) != len(service_ids)
    ):
        raise ValueError("应用服务 Profile 的本地登记声明无效")
    return {"backend": SERVICE_BACKEND, "service_ids": sorted(service_ids)}


def profile_ids(declarations: list[dict[str, Any]]) -> list[str]:
    return [declaration["id"] for declaration in declarations]


def validate_profile_agent_compatibility(
    declarations: list[dict[str, Any]], version: dict[str, Any]
) -> None:
    agent_version = version_tuple(version.get("agent_version"))
    if agent_version is None:
        raise ValueError("Agent agent_version 无效")
    for declaration in declarations:
        profile_id = declaration["id"]
        minimum = PROFILE_REGISTRY[profile_id].get("minimum_agent_version")
        if not isinstance(minimum, str):
            continue
        minimum_version = version_tuple(minimum)
        if minimum_version is None:
            raise RuntimeError(f"Profile {profile_id} 的最低 Agent 版本配置无效")
        if agent_version < minimum_version:
            raise ValueError(
                f"{profile_id} 需要 Agent {minimum} 或更高版本；"
                f"当前版本为 {version['agent_version']}，请重新从工作台组装 Agent"
            )
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
                "plan_only": definition.get("plan_only", False),
                "minimum_agent_version": definition.get("minimum_agent_version"),
                "runtimes": sorted(definition["implementations"]),
                "actions": deepcopy(definition["actions"]),
                "runtime_contract": deepcopy(definition.get("runtime_contract", {})),
                "limits": deepcopy(definition.get("limits", {})),
            }
        )
    return catalog


def profile_mcp_tools(*, plan_only: bool = False) -> set[str]:
    return {
        action["mcp_tool"]
        for definition in PROFILE_REGISTRY.values()
        if definition.get("plan_only", False) == plan_only
        for action in definition.get("actions", [])
        if isinstance(action.get("mcp_tool"), str) and action["mcp_tool"]
    }


def profile_action_mcp_tool(profile_id: str, action: str) -> str:
    definition = PROFILE_REGISTRY.get(profile_id)
    if definition is None:
        raise ValueError(f"不支持的 Profile: {profile_id}")
    for action_definition in definition.get("actions", []):
        if action_definition.get("name") == action:
            tool = action_definition.get("mcp_tool")
            if isinstance(tool, str) and tool:
                return tool
            break
    raise ValueError(f"Profile {profile_id} 不支持操作: {action}")


def validate_profile_result(
    profile_id: str, action: str, result: dict[str, Any]
) -> dict[str, Any]:
    if profile_id == "workspace.v1":
        return _validate_workspace_result(action, result)
    if profile_id == "system_observe.v1":
        return _validate_system_observe_result(action, result)
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


def _validate_workspace_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    path = result.get("path")
    sha256 = result.get("sha256")
    path_object = PurePosixPath(path) if isinstance(path, str) else None
    windows_path = PureWindowsPath(path) if isinstance(path, str) else None
    if (
        not isinstance(path, str)
        or not path
        or path_object is None
        or path_object.is_absolute()
        or windows_path is None
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in path_object.parts
    ):
        raise ValueError("Agent 返回的 workspace path 无效")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in sha256)
    ):
        raise ValueError("Agent 返回的 workspace sha256 无效")
    if action == "read":
        content = result.get("content")
        offset = result.get("offset")
        lines = result.get("lines")
        total_lines = result.get("total_lines")
        truncated = result.get("truncated")
        size = result.get("size")
        if not isinstance(content, str):
            raise ValueError("Agent 返回的 workspace content 必须是字符串")
        if len(content.encode("utf-8")) > WORKSPACE_MAX_READ_BYTES:
            raise ValueError("Agent 返回的 workspace content 超过限制")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 1
            or not isinstance(lines, int)
            or isinstance(lines, bool)
            or lines < 0
            or not isinstance(total_lines, int)
            or isinstance(total_lines, bool)
            or total_lines < 0
            or not isinstance(truncated, bool)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ValueError("Agent 返回的 workspace read 字段无效")
        return {
            "path": path,
            "content": content,
            "offset": offset,
            "lines": lines,
            "total_lines": total_lines,
            "truncated": truncated,
            "size": size,
            "sha256": sha256.lower(),
        }
    if action == "write":
        bytes_written = result.get("bytes_written")
        changed = result.get("changed")
        if (
            not isinstance(bytes_written, int)
            or isinstance(bytes_written, bool)
            or bytes_written < 0
            or bytes_written > WORKSPACE_MAX_WRITE_BYTES
            or not isinstance(changed, bool)
        ):
            raise ValueError("Agent 返回的 workspace write 字段无效")
        return {
            "path": path,
            "bytes_written": bytes_written,
            "sha256": sha256.lower(),
            "changed": changed,
        }
    if action == "edit":
        changed = result.get("changed")
        diff = result.get("diff")
        diff_truncated = result.get("diff_truncated")
        first_changed_line = result.get("first_changed_line")
        if (
            not isinstance(changed, bool)
            or not isinstance(diff, str)
            or len(diff.encode("utf-8")) > WORKSPACE_MAX_READ_BYTES
            or not isinstance(diff_truncated, bool)
            or not isinstance(first_changed_line, int)
            or isinstance(first_changed_line, bool)
            or first_changed_line < 1
        ):
            raise ValueError("Agent 返回的 workspace edit 字段无效")
        return {
            "path": path,
            "changed": changed,
            "diff": diff,
            "diff_truncated": diff_truncated,
            "first_changed_line": first_changed_line,
            "sha256": sha256.lower(),
        }
    raise ValueError(f"不支持的 workspace.v1 操作: {action}")


def _validate_system_observe_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
    observed_at = result.get("observed_at")
    observed_at_unix = result.get("observed_at_unix")
    if (
        not isinstance(observed_at, str)
        or len(observed_at) > 64
        or not observed_at.endswith("Z")
        or not isinstance(observed_at_unix, (int, float))
        or isinstance(observed_at_unix, bool)
    ):
        raise ValueError("Agent 返回的 system_observe observed_at 无效")

    if action == "get_host_info":
        required = ("os", "system", "release", "version", "architecture", "hostname", "python")
        if any(not isinstance(result.get(field), str) for field in required):
            raise ValueError("Agent 返回的 system_observe host_info 字段无效")
        pid = result.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 0:
            raise ValueError("Agent 返回的 system_observe pid 无效")
        _validate_system_observe_host_metadata(result)
        return dict(result)

    if action == "get_resources":
        cpu = result.get("cpu")
        disks = result.get("disks")
        if not isinstance(cpu, dict) or not isinstance(disks, list) or len(disks) > 16:
            raise ValueError("Agent 返回的 system_observe resources 字段无效")
        logical_count = cpu.get("logical_count")
        if logical_count is not None and (
            not isinstance(logical_count, int) or isinstance(logical_count, bool) or logical_count < 1
        ):
            raise ValueError("Agent 返回的 system_observe cpu 字段无效")
        if not isinstance(result.get("memory"), (dict, type(None))):
            raise ValueError("Agent 返回的 system_observe memory 字段无效")
        for disk in disks:
            if not isinstance(disk, dict):
                raise ValueError("Agent 返回的 system_observe disk 字段无效")
            if any(
                not isinstance(disk.get(field), int)
                or isinstance(disk.get(field), bool)
                or disk.get(field) < 0
                for field in ("total_bytes", "used_bytes", "free_bytes")
            ):
                raise ValueError("Agent 返回的 system_observe disk 字段无效")
        return dict(result)

    if action == "get_processes":
        processes = result.get("processes")
        if (
            not isinstance(result.get("available"), bool)
            or not isinstance(processes, list)
            or len(processes) > 100
            or not isinstance(result.get("truncated"), bool)
        ):
            raise ValueError("Agent 返回的 system_observe processes 字段无效")
        for process in processes:
            if (
                not isinstance(process, dict)
                or not isinstance(process.get("pid"), int)
                or isinstance(process.get("pid"), bool)
                or not isinstance(process.get("name"), str)
            ):
                raise ValueError("Agent 返回的 system_observe process 摘要无效")
        return dict(result)

    if action == "get_network":
        interfaces = result.get("interfaces")
        addresses = result.get("addresses")
        if (
            not isinstance(result.get("hostname"), str)
            or not isinstance(interfaces, list)
            or len(interfaces) > 128
            or not isinstance(addresses, list)
            or len(addresses) > 128
            or not all(isinstance(address, str) and len(address) <= 128 for address in addresses)
        ):
            raise ValueError("Agent 返回的 system_observe network 字段无效")
        for interface in interfaces:
            if (
                not isinstance(interface, dict)
                or not isinstance(interface.get("index"), int)
                or isinstance(interface.get("index"), bool)
                or not isinstance(interface.get("name"), str)
            ):
                raise ValueError("Agent 返回的 system_observe interface 摘要无效")
        _validate_system_observe_tcp_listeners(result.get("tcp_listeners"))
        return dict(result)

    raise ValueError(f"不支持的 system_observe.v1 操作: {action}")


def _validate_system_observe_host_metadata(result: dict[str, Any]) -> None:
    if "privileges" in result:
        privileges = result["privileges"]
        if (
            not isinstance(privileges, dict)
            or set(privileges) != {"is_admin", "is_root"}
            or any(flag is not None and not isinstance(flag, bool) for flag in privileges.values())
        ):
            raise ValueError("Agent 返回的 system_observe privileges 无效")

    distribution = result.get("linux_distribution")
    if distribution is not None:
        if not isinstance(distribution, dict) or set(distribution) != {"id", "version_id"}:
            raise ValueError("Agent 返回的 system_observe linux_distribution 无效")
        distribution_id, version_id = distribution["id"], distribution["version_id"]
        if not _is_os_release_id(distribution_id) or (version_id is not None and not _is_os_release_id(version_id)):
            raise ValueError("Agent 返回的 system_observe linux_distribution 无效")

    if "linux_tools" in result:
        tools = result["linux_tools"]
        if (
            not isinstance(tools, dict)
            or set(tools) != {"sudo_on_path", "docker_on_path", "podman_on_path"}
            or not all(isinstance(found, bool) for found in tools.values())
        ):
            raise ValueError("Agent 返回的 system_observe linux_tools 无效")


def _is_os_release_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9._-]{1,64}", value) is not None


def _validate_system_observe_tcp_listeners(listeners: object) -> None:
    if listeners is None:
        return
    if not isinstance(listeners, dict) or set(listeners) != {"ports", "truncated"}:
        raise ValueError("Agent 返回的 system_observe tcp_listeners 无效")
    ports = listeners["ports"]
    truncated = listeners["truncated"]
    if (
        not isinstance(ports, list)
        or len(ports) > 128
        or not all(type(port) is int and 1 <= port <= 65535 for port in ports)
        or ports != sorted(set(ports))
        or not isinstance(truncated, bool)
        or (truncated and len(ports) != 128)
    ):
        raise ValueError("Agent 返回的 system_observe tcp_listeners 无效")
