from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, get_args

from .filesystem import WorkspaceError
from .managed_contracts import (
    BUILD_PROFILE, GENERATION, IDENTIFIER, LOCAL_PLAN_PROFILES, SERVICE_PROFILE, SOURCE_SHA256,
    project_managed_result,
)


PlannedOperation = Literal[
    "workspace.hash", "workspace.create", "workspace.edit", "shell.version",
    "service.inspect", "service.restart", "build.source", "build.verify",
]
OPERATIONS = get_args(PlannedOperation)
MANAGED_PROFILES = frozenset({"workspace.v1", "shell.v1"}) | LOCAL_PLAN_PROFILES
_VERSIONS = {
    "git": ("git --version", r"git version ([0-9][0-9A-Za-z.+-]{0,39})"),
    "node": ("node --version", r"v([0-9][0-9A-Za-z.+-]{0,39})"),
    "python": ("python --version", r"Python ([0-9][0-9A-Za-z.+-]{0,39})"),
}
_PLAIN_SUFFIXES = frozenset({".md", ".txt"})
_CONFIG_SUFFIXES = frozenset({".ini", ".json", ".toml", ".yaml", ".yml"})
_BLOCKED_NAMES = re.compile(
    r"(?:^|[_. -])(?:agents|claude|gemini|instructions|auth|credentials?|secrets?|passwords?|tokens?|id_rsa|id_ed25519)"
    r"(?:$|[_. -])", re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"-----BEGIN [^\r\n]*PRIVATE KEY-----|(?:\b(?:sk-|gh[pousr]_|mcp_|enroll_|agent_|session_)[A-Za-z0-9_-]{20,})"
    r"|(?:\b(?:password|passwd|credential|secret|token|private[_-]?key|api[_-]?key|authorization)\b"
    r"[\"']?\s*[:=])", re.IGNORECASE,
)
_WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", re.IGNORECASE)
_PATH_PART = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_. -]{0,94}[A-Za-z0-9_-])?")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def digest_json(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class PlannedAction:
    operation: str
    profile: str
    action: str
    risk_level: str
    params: dict[str, str]
    handler_params: dict[str, Any]

    @property
    def scope(self) -> dict[str, str]:
        return {"operation": self.operation, "input_hash": digest_json(self.params)}


def planned_workspace_risk(path: str) -> str:
    if len(path) > 240 or any(
        not _PATH_PART.fullmatch(part) or _WINDOWS_DEVICE.fullmatch(part) or _BLOCKED_NAMES.search(part)
        for part in path.split("/")
    ):
        raise ValueError("风险 L3：只允许普通工作区相对路径，禁止敏感路径")
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in _PLAIN_SUFFIXES | _CONFIG_SUFFIXES:
        raise ValueError("风险 L3：施工范围只包含文本和受限配置文件")
    return "L2" if suffix in _CONFIG_SUFFIXES else "L1"


def parse_planned_action(operation: object, params: object) -> PlannedAction:
    if not isinstance(operation, str) or operation not in OPERATIONS or not isinstance(params, dict):
        raise ValueError("当前计划不支持此操作，只能使用已定义的受控动作")
    if not all(isinstance(value, str) for value in params.values()):
        raise ValueError("操作参数必须是字符串")
    if operation in {"service.inspect", "service.restart"}:
        restart = operation == "service.restart"
        keys = {"service_id", "expected_generation"} if restart else {"service_id"}
        if (
            params.keys() != keys or IDENTIFIER.fullmatch(params["service_id"]) is None
            or (restart and GENERATION.fullmatch(params["expected_generation"]) is None)
        ):
            raise ValueError("服务动作需要登记的服务 ID；恢复必须绑定观察到的服务代际")
        return PlannedAction(operation, SERVICE_PROFILE, "restart" if restart else "inspect", "L2" if restart else "L0",
                             params, params.copy())
    if operation in {"build.source", "build.verify"}:
        verify = operation == "build.verify"
        if (verify or params) and (
            params.keys() != {"source_sha256"} or SOURCE_SHA256.fullmatch(params["source_sha256"]) is None
        ):
            raise ValueError("构建验证只能绑定本地预置源码包的小写 SHA-256")
        return PlannedAction(operation, BUILD_PROFILE, "verify" if verify else "source", "L1" if verify else "L0",
                             params, params.copy() if verify else {})
    if operation == "shell.version":
        if params.keys() != {"tool"} or params["tool"] not in _VERSIONS:
            raise ValueError("只允许检查 git、node 或 python 版本，不接受任意命令")
        command = _VERSIONS[params["tool"]][0]
        return PlannedAction(operation, "shell.v1", "exec", "L0", params, {"command": command, "timeout": 5})
    expected_keys = {
        "workspace.hash": {"path"},
        "workspace.create": {"path", "content"},
        "workspace.edit": {"path", "old_text", "new_text", "expected_sha256"},
    }
    if params.keys() != expected_keys[operation]:
        raise ValueError("文件操作参数与契约不符")
    path = params["path"]
    risk = planned_workspace_risk(path)
    if operation == "workspace.hash":
        return PlannedAction(operation, "workspace.v1", "read", "L0", params, {"path": path, "limit": 1})
    text_keys = ("content",) if operation == "workspace.create" else ("old_text", "new_text")
    for field in text_keys:
        text = params[field]
        try:
            valid = len(text.encode("utf-8")) <= 8192 and "\0" not in text
        except UnicodeEncodeError:
            valid = False
        if not valid or _SECRET_TEXT.search(text):
            raise ValueError("文本必须为不含凭据的 UTF-8 字符串，每段最多 8 KiB")
    if operation == "workspace.create":
        return PlannedAction(operation, "workspace.v1", "write", risk, params, {**params, "expected_sha256": None})
    if not _DIGEST.fullmatch(params["expected_sha256"]) or not params["old_text"] or params["old_text"] == params["new_text"]:
        raise ValueError("编辑需要当前文件的小写 SHA-256 和不同的替换前后文本")
    return PlannedAction(operation, "workspace.v1", "edit", risk, params, params.copy())


def check_workspace_target(workdir: Path, path: str, *, create: bool = False) -> None:
    root = workdir.resolve(strict=True)
    candidate = root
    parts = path.split("/")
    for index, part in enumerate(parts):
        candidate = candidate / part
        final = index == len(parts) - 1
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if final and create:
                return
            raise WorkspaceError("not_found", "文件或父目录不存在") from None
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1 if final else stat.S_ISDIR(info.st_mode)
        if not regular or reparse:
            raise WorkspaceError("invalid_path", "计划目标必须为普通文件，不能经过链接或重解析点")


def public_planned_result(action: PlannedAction, result: dict[str, Any]) -> dict[str, Any]:
    if action.profile in LOCAL_PLAN_PROFILES:
        return project_managed_result(action.operation, action.params, result)
    if action.operation == "shell.version":
        tool = action.params["tool"]
        stdout = result.get("stdout")
        line = stdout.strip() if isinstance(stdout, str) and len(stdout) < 256 else ""
        matched = re.fullmatch(_VERSIONS[tool][1], line)
        if result.get("exit_code") != 0 or not matched:
            raise ValueError("verification_failed: 未得到固定工具的版本输出")
        return {"tool": tool, "version": matched[1]}
    path = action.params["path"]
    digest = result.get("sha256")
    if result.get("path") != path or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("verification_failed: 文件路径或摘要不匹配")
    if action.operation == "workspace.hash":
        return {"path": path, "sha256": digest}
    if result.get("changed") is not True:
        raise ValueError("verification_failed: 文件未确认发生变更")
    return {"path": path, "sha256": digest, "changed": True}
