from __future__ import annotations

import asyncio
import json
import os
import platform
import ssl
import socket
import sys
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from remote_agent.profiles.base import ProfileHandler
from remote_agent.profiles.common.planned_action import MANAGED_PROFILES
from remote_agent.runtimes.base import LocalAgentRuntime
from remote_agent.runtimes.health import HEALTH_PROFILE

from .config import AgentConfigStore
from .errors import FatalAgentError
from .limits import MAX_CONCURRENT_REQUESTS_PER_AGENT
from .task_runtime import TASK_PROTOCOL_VERSION, TaskRuntime
from .version import AGENT_VERSION, CORE_VERSION, PROTOCOL_VERSION


RECONNECT_MIN_SECONDS = 2
RECONNECT_MAX_SECONDS = 30


class RemoteAgent:
    def __init__(
        self,
        gateway_url: str,
        config_store: AgentConfigStore,
        enrollment_token: str | None,
        name: str,
        description: str,
        build_id: str,
        handlers: dict[str, ProfileHandler],
        runtimes: dict[str, LocalAgentRuntime] | None = None,
    ):
        if not handlers:
            raise FatalAgentError("Agent 至少需要加载一个 Profile Handler")
        self.gateway_url = gateway_url
        self.config_store = config_store
        self.enrollment_token = enrollment_token
        self.name = name
        self.description = description
        self.build_id = build_id
        self.handlers = handlers
        self.running: dict[str, asyncio.Task[None]] = {}
        self.request_profiles: dict[str, str] = {}
        self.send_lock = asyncio.Lock()
        self.runtimes = dict(runtimes or {})
        self.task_runtime = TaskRuntime(self, handlers, self.runtimes)

    @property
    def profile_ids(self) -> list[str]:
        return sorted(self.handlers)

    @property
    def profile_declarations(self) -> list[dict[str, Any]]:
        return [self.handlers[profile_id].declaration() for profile_id in self.profile_ids]

    def hello_message(self) -> dict[str, Any]:
        runtimes = self.runtime_declarations
        supports_plan = bool(self.handlers.keys() & MANAGED_PROFILES)
        supports_tasks = supports_plan or HEALTH_PROFILE in self.handlers
        system = platform.system()
        family = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(
            system, "posix" if os.name == "posix" else "unknown",
        )
        release = (platform.mac_ver()[0] if system == "Darwin" else "") or platform.release()
        base: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": "hello",
            "profiles": self.profile_declarations,
            "version": {
                "agent_version": AGENT_VERSION,
                "core_version": CORE_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "build_id": self.build_id,
            },
            "device": {
                "device_type": "computer",
                "platform": family,
                "platform_version": release[:128] if release and release.isprintable() else None,
            },
            "metadata": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "pid": os.getpid(),
            },
            "features": {
                "task_protocol": TASK_PROTOCOL_VERSION if supports_tasks else 0,
                "event_stream": supports_tasks,
                "runtime_adapter": bool(runtimes),
                "planned_actions": supports_plan,
                "planned_l1_policy": supports_plan,
                "health_step_cancel": HEALTH_PROFILE in self.handlers,
                "action_interception": "limited" if runtimes else "full" if supports_tasks else "none",
                "evidence": False,
            },
            "runtimes": runtimes,
        }
        if self.enrollment_token:
            base.update(
                {
                    "enrollment_token": self.enrollment_token,
                    "name": self.name,
                    "description": self.description,
                }
            )
            return base
        config = self.config_store.load()
        if not config:
            raise FatalAgentError("首次运行需要提供 --token 完成 Agent 注册")
        base.update({"agent_id": config.get("agent_id"), "credential": config.get("credential")})
        return base

    @property
    def runtime_declarations(self) -> list[dict[str, Any]]:
        return [runtime.declaration() for runtime in self.runtimes.values()]

    async def send(self, websocket: ClientConnection, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await websocket.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def run_request(self, websocket: ClientConnection, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        profile_id = message.get("profile")
        try:
            if not isinstance(request_id, str):
                raise ValueError("request_id 无效")
            if not isinstance(profile_id, str) or profile_id not in self.handlers:
                raise ValueError(f"Agent 未加载 Profile: {profile_id}")
            handler = self.handlers[profile_id]
            if handler.plan_only:
                raise PermissionError("此 Profile 仅接受受控计划")
            action = message.get("action")
            if not isinstance(action, str) or not action:
                raise ValueError("action 无效")
            params = message.get("params")
            if not isinstance(params, dict):
                raise ValueError("params 必须是对象")
            result = await handler.execute(action, request_id, params)
            response = {
                "v": PROTOCOL_VERSION,
                "type": "result",
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        except asyncio.CancelledError:
            response = {
                "v": PROTOCOL_VERSION,
                "type": "result",
                "request_id": request_id,
                "ok": False,
                "error": "任务已取消",
            }
        except Exception as exc:
            response = {
                "v": PROTOCOL_VERSION,
                "type": "result",
                "request_id": request_id,
                "ok": False,
                "error": str(exc),
            }
        try:
            await self.send(websocket, response)
        finally:
            self.running.pop(request_id, None)
            self.request_profiles.pop(request_id, None)

    async def dispatch_request(
        self, websocket: ClientConnection, message: dict[str, Any]
    ) -> None:
        request_id = message.get("request_id")
        profile_id = message.get("profile")
        if not isinstance(request_id, str) or request_id in self.running:
            return
        if (
            len(self.running) + self.task_runtime.active_count
            >= MAX_CONCURRENT_REQUESTS_PER_AGENT
        ):
            await self.send(
                websocket,
                {
                    "v": PROTOCOL_VERSION,
                    "type": "result",
                    "request_id": request_id,
                    "ok": False,
                    "error": (
                        "Agent 当前忙碌；每台 Agent 最多同时执行 "
                        f"{MAX_CONCURRENT_REQUESTS_PER_AGENT} 个请求"
                    ),
                },
            )
            return
        if isinstance(profile_id, str):
            self.request_profiles[request_id] = profile_id
        task = asyncio.create_task(self.run_request(websocket, message))
        self.running[request_id] = task

    async def session(self) -> None:
        async with websockets.connect(
            self.gateway_url,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ) as websocket:
            await self.send(websocket, self.hello_message())
            raw = await asyncio.wait_for(websocket.recv(), timeout=15)
            response = json.loads(raw)
            if not isinstance(response, dict) or response.get("v") != PROTOCOL_VERSION:
                raise FatalAgentError(
                    f"网关协议版本不兼容，需要 v{PROTOCOL_VERSION}；请更新网关或 Agent"
                )
            if response.get("type") == "error":
                raise FatalAgentError(str(response.get("error") or "Agent 认证失败"))
            if response.get("type") == "registered":
                self.config_store.save(
                    {
                        "gateway_url": self.gateway_url,
                        "agent_id": response["agent_id"],
                        "credential": response["credential"],
                    }
                )
                self.enrollment_token = None
                print(f"Agent 注册成功: {response['agent_id']}", flush=True)
            elif response.get("type") != "ready":
                raise FatalAgentError("网关返回了无效的握手响应")

            profile_summary = ", ".join(
                f"{declaration['id']} [{_runtime_label(declaration['runtime'])}]"
                for declaration in self.profile_declarations
            )
            print(
                f"Agent {AGENT_VERSION} ({self.build_id}) 已连接: "
                f"{self.gateway_url} ({profile_summary})",
                flush=True,
            )
            async for raw_message in websocket:
                message = json.loads(raw_message)
                if not isinstance(message, dict) or message.get("v") != PROTOCOL_VERSION:
                    continue
                message_type = message.get("type")
                if message_type == "ping":
                    await self.send(
                        websocket,
                        {"v": PROTOCOL_VERSION, "type": "pong", "ts": message.get("ts")},
                    )
                elif message_type == "request":
                    await self.dispatch_request(websocket, message)
                elif message_type == "task_start":
                    await self.task_runtime.dispatch(
                        websocket,
                        message,
                        reserved_count=len(self.running),
                    )
                elif message_type == "task_action_decision":
                    await self.task_runtime.decide(message)
                elif message_type == "task_step_cancel":
                    task_id, step_id = message.get("task_id"), message.get("step_id")
                    if isinstance(task_id, str) and isinstance(step_id, str):
                        self.task_runtime.cancel_health_step(task_id, step_id)
                elif message_type == "task_cancel":
                    task_id = message.get("task_id")
                    if isinstance(task_id, str):
                        await self.task_runtime.cancel(task_id, websocket)
                elif message_type == "cancel":
                    request_id = message.get("request_id")
                    profile_id = self.request_profiles.get(request_id)
                    handler = self.handlers.get(profile_id) if profile_id else None
                    if handler is not None:
                        await handler.cancel(request_id)
                    task = self.running.get(request_id)
                    if task is not None:
                        task.cancel()

    async def run_forever(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        while True:
            try:
                await self.session()
                delay = RECONNECT_MIN_SECONDS
            except FatalAgentError:
                raise
            except ssl.SSLCertVerificationError as exc:
                raise FatalAgentError(f"Gateway TLS 证书验证失败: {exc}") from exc
            except websockets.InvalidURI as exc:
                raise FatalAgentError(f"Gateway WebSocket 地址无效: {exc}") from exc
            except (
                OSError,
                TimeoutError,
                websockets.WebSocketException,
                json.JSONDecodeError,
            ) as exc:
                print(f"连接中断: {exc}; {delay} 秒后重试", file=sys.stderr, flush=True)
            finally:
                tasks = list(self.running.values())
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await self.task_runtime.shutdown()
                self.running.clear()
                self.request_profiles.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)


def _runtime_label(runtime: dict[str, Any]) -> str:
    return str(
        runtime.get("dialect")
        or runtime.get("scope")
        or runtime.get("engine")
        or runtime.get("language")
        or "unknown"
    )
