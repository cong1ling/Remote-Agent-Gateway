from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from .base import ActionDecision, RuntimeEvent, RuntimeSession, RuntimeTask
from .health import HEALTH_PROFILE, OBSERVATION_TOOLS
from .jsonrpc import JsonRpcClient, JsonRpcError, sanitized_environment


CODEX_VERSION = "0.169.0"
CODEX_ADAPTER_VERSION = "0.1.0"
_DENIED_APPROVALS = {
    "item/commandExecution/requestApproval": {"decision": "cancel"},
    "item/fileChange/requestApproval": {"decision": "cancel"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
}
_APP_SERVER_FLAGS = (
    "app-server", "--stdio",
    "-c", "features.shell_tool=false",
    "-c", "features.hooks=false",
    "-c", "features.plugins=false",
    "-c", "features.apps=false",
    "-c", "features.multi_agent=false",
    "-c", "features.goals=false",
    "-c", "features.view_image=false",
    "-c", "features.image_generation=false",
    "-c", "mcp_servers={}",
    "-c", 'web_search="disabled"',
    "-c", 'shell_environment_policy.inherit="none"',
)


class CodexRuntime:
    provider_id = "codex"
    mode = "managed_limited"

    def __init__(
        self,
        executable: Path,
        home: Path,
        *,
        config_env: str | None = None,
        client_factory: Callable[..., Any] = JsonRpcClient.spawn,
    ):
        self.command = [str(executable), *_APP_SERVER_FLAGS]
        self.environment = {**sanitized_environment(exclude=config_env), "CODEX_HOME": str(home)}
        self.client_factory = client_factory
        self._clients: dict[str, JsonRpcClient] = {}
        self._directories: dict[str, tempfile.TemporaryDirectory[str]] = {}
        self._requests: dict[tuple[str, str], tuple[object, str]] = {}

    @classmethod
    def from_installation(
        cls, home: Path, executable: Path | None, *, config_env: str | None = None,
    ) -> CodexRuntime:
        home = home.expanduser().resolve()
        candidate = executable or shutil.which("codex.exe" if os.name == "nt" else "codex")
        if not home.is_dir() or not candidate:
            raise ValueError("启用 Codex 需要已存在的专用 --codex-home 和本机 Codex 可执行文件")
        path = Path(candidate).expanduser().resolve()
        if not path.is_file() or (os.name == "nt" and path.suffix.lower() != ".exe"):
            raise ValueError("--codex-bin 必须指向本机 Codex 可执行文件（Windows 使用 .exe）")
        runtime = cls(path, home, config_env=config_env)
        try:
            check = subprocess.run(
                [str(path), "--version"],
                cwd=home,
                env=runtime.environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                check=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ValueError("无法验证本机 Codex 版本") from exc
        if check.stdout.strip() != f"codex-cli {CODEX_VERSION}":
            raise ValueError(f"当前健康检查适配器仅支持 Codex CLI {CODEX_VERSION}")
        return runtime

    def declaration(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "runtime_version": CODEX_VERSION,
            "adapter_version": CODEX_ADAPTER_VERSION,
            "status": "available",
            "mode": self.mode,
            "supported_profiles": [HEALTH_PROFILE],
        }

    async def start(self, task: RuntimeTask) -> RuntimeSession:
        if not task.actions or set(task.actions) - OBSERVATION_TOOLS.keys():
            raise ValueError("Codex 仅支持固定 L0 健康检查")
        session = RuntimeSession(
            session_id=f"runtime_{uuid.uuid4().hex}",
            provider_id=self.provider_id,
            provider_version=CODEX_VERSION,
            adapter_version=CODEX_ADAPTER_VERSION,
            mode=self.mode,
        )
        directory = tempfile.TemporaryDirectory(prefix="rag-codex-")
        self._directories[session.session_id] = directory
        try:
            client = await self.client_factory(self.command, cwd=directory.name, env=self.environment)
            self._clients[session.session_id] = client
            initialize = await client.request(
                "initialize",
                {
                    "clientInfo": {"name": "remote-agent-gateway", "version": CODEX_ADAPTER_VERSION},
                    "capabilities": {"experimentalApi": True, "explicitGatewayOauth": True},
                },
            )
            user_agent = initialize.get("userAgent")
            version = re.match(r"^[^/]+/(\S+)", user_agent) if isinstance(user_agent, str) else None
            if not version or version[1] != CODEX_VERSION:
                raise JsonRpcError(f"Codex app-server 与已验证的 {CODEX_VERSION} 协议不匹配")
            await client.notify("initialized")
            thread = await client.request(
                "thread/start",
                {
                    "cwd": directory.name,
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "environments": [],
                    "dynamicTools": [
                        {
                            "type": "function",
                            "name": action,
                            "description": OBSERVATION_TOOLS[action],
                            "inputSchema": {
                                "type": "object", "properties": {}, "additionalProperties": False,
                            },
                            "deferLoading": False,
                        }
                        for action in task.actions
                    ],
                },
            )
            if (
                thread.get("approvalPolicy") != "never"
                or thread.get("sandbox") != {"type": "readOnly", "networkAccess": False}
                or thread.get("instructionSources")
            ):
                raise JsonRpcError("Codex app-server 未使用要求的只读配置")
            session.metadata["thread_id"] = _result_id(thread, "thread")
            turn = await client.request(
                "turn/start",
                {
                    "threadId": session.metadata["thread_id"],
                    "environments": [],
                    "input": [{"type": "text", "text": task.prompt, "text_elements": []}],
                },
            )
            session.metadata["turn_id"] = _result_id(turn, "turn")
            session.state = "running"
            return session
        except BaseException:
            await self.close(session)
            raise

    async def events(self, session: RuntimeSession) -> AsyncIterator[RuntimeEvent]:
        client = self._client(session)
        async for message in client.events():
            method = message.get("method")
            params = message.get("params")
            request_id = message.get("id")
            if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
                if isinstance(method, str) and method in _DENIED_APPROVALS:
                    await client.respond(request_id, _DENIED_APPROVALS[method])
                    yield RuntimeEvent("action_blocked", {"reason": "拒绝 Codex 原生权限审批"})
                    raise JsonRpcError("Codex 请求了未授权的原生能力")
                if method != "item/tool/call":
                    await client.reject(request_id)
                    yield RuntimeEvent("action_blocked", {"reason": "拒绝非健康检查请求"})
                    raise JsonRpcError("Codex 发出了不受支持的服务端请求")
                if not self._is_current_turn(session, params) or not isinstance(params.get("callId"), str):
                    raise JsonRpcError("Codex 工具请求未绑定当前 turn")
                action_request_id = f"codex_call_{uuid.uuid4().hex}"
                action = params.get("tool")
                arguments = params.get("arguments")
                if (
                    not isinstance(action, str)
                    or action not in OBSERVATION_TOOLS
                    or arguments != {}
                    or params.get("namespace") is not None
                ):
                    await client.respond(
                        request_id, _tool_response(False, {"error": "Only no-argument health tools are allowed"}),
                    )
                    yield RuntimeEvent("action_blocked", {"reason": "拒绝非白名单工具或额外参数"})
                    continue
                self._requests[(session.session_id, action_request_id)] = (request_id, action)
                yield RuntimeEvent("action_request", {"action": action}, action_request_id)
            elif method == "item/started" and self._is_current_turn(session, params):
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") in (
                    "commandExecution", "fileChange", "mcpToolCall", "webSearch", "collabAgentToolCall",
                ):
                    yield RuntimeEvent("action_blocked", {"reason": "检测到不受支持的 Codex 执行项"})
                    raise JsonRpcError("Codex 发出了不受支持的执行项")
            elif method == "turn/completed" and isinstance(params, dict):
                turn = params.get("turn")
                if (
                    params.get("threadId") != session.metadata["thread_id"]
                    or not isinstance(turn, dict)
                    or turn.get("id") != session.metadata["turn_id"]
                ):
                    continue
                if turn.get("status") != "completed":
                    raise JsonRpcError("Codex turn 未成功完成；请检查设备端登录、模型和网络")
                session.state = "completed"
                yield RuntimeEvent("turn_completed", {})
                return
        raise JsonRpcError("Codex app-server 在 turn 完成前断开")

    async def decide_action(self, session: RuntimeSession, decision: ActionDecision) -> None:
        client = self._client(session)
        context = self._requests.pop((session.session_id, decision.action_request_id), None)
        if context is None:
            raise JsonRpcError("未知的 Codex 工具请求")
        request_id, action = context
        allowed = (
            decision.decision == "allow"
            and decision.policy_context.get("risk_level") == "L0"
            and decision.policy_context.get("allowed_scope") == {
                "profile": HEALTH_PROFILE, "action": action,
            }
        )
        payload = decision.result if allowed else {"error": "Health check policy denied this call"}
        await client.respond(request_id, _tool_response(allowed, payload))

    async def cancel(self, session: RuntimeSession) -> None:
        try:
            await self._client(session).request(
                "turn/interrupt",
                {"threadId": session.metadata["thread_id"], "turnId": session.metadata["turn_id"]},
                timeout=2,
            )
        except (JsonRpcError, TimeoutError):
            pass
        session.state = "cancelled"

    async def close(self, session: RuntimeSession) -> None:
        client = self._clients.pop(session.session_id, None)
        directory = self._directories.pop(session.session_id, None)
        for key in tuple(self._requests):
            if key[0] == session.session_id:
                self._requests.pop(key)
        try:
            if client is not None:
                await client.close()
        finally:
            if directory is not None:
                directory.cleanup()
            session.state = "closed"

    def _client(self, session: RuntimeSession) -> JsonRpcClient:
        client = self._clients.get(session.session_id)
        if client is None:
            raise JsonRpcError("Codex Runtime 会话不存在")
        return client

    @staticmethod
    def _is_current_turn(session: RuntimeSession, params: object) -> bool:
        return (
            isinstance(params, dict)
            and params.get("threadId") == session.metadata["thread_id"]
            and params.get("turnId") == session.metadata["turn_id"]
        )


def _result_id(result: dict[str, Any], name: str) -> str:
    value = result.get(name)
    if isinstance(value, dict) and isinstance(value.get("id"), str) and value["id"]:
        return value["id"]
    raise JsonRpcError(f"Codex app-server 未返回 {name} ID")


def _tool_response(success: bool, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": success,
        "contentItems": [{"type": "inputText", "text": json.dumps(payload, ensure_ascii=False)}],
    }
