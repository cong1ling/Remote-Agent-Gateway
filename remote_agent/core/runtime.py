from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import sys
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from remote_agent.profiles.base import ProfileHandler

from .config import AgentConfig
from .errors import FatalAgentError


PROTOCOL_VERSION = 1
RECONNECT_MIN_SECONDS = 2
RECONNECT_MAX_SECONDS = 30


class RemoteAgent:
    def __init__(
        self,
        gateway_url: str,
        config_store: AgentConfig,
        enrollment_token: str | None,
        name: str,
        description: str,
        handlers: dict[str, ProfileHandler],
    ):
        if not handlers:
            raise FatalAgentError("Agent 至少需要加载一个 Profile Handler")
        self.gateway_url = gateway_url
        self.config_store = config_store
        self.enrollment_token = enrollment_token
        self.name = name
        self.description = description
        self.handlers = handlers
        self.running: dict[str, asyncio.Task[None]] = {}
        self.request_profiles: dict[str, str] = {}
        self.send_lock = asyncio.Lock()

    @property
    def profile_ids(self) -> list[str]:
        return sorted(self.handlers)

    def hello_message(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": "hello",
            "profiles": self.profile_ids,
            "metadata": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "pid": os.getpid(),
            },
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
            action = message.get("action")
            if not isinstance(action, str) or not action:
                raise ValueError("action 无效")
            params = message.get("params")
            if not isinstance(params, dict):
                raise ValueError("params 必须是对象")
            result = await self.handlers[profile_id].execute(action, request_id, params)
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

            print(
                f"Agent 已连接: {self.gateway_url} ({', '.join(self.profile_ids)})",
                flush=True,
            )
            async for raw_message in websocket:
                message = json.loads(raw_message)
                message_type = message.get("type")
                if message_type == "ping":
                    await self.send(
                        websocket,
                        {"v": PROTOCOL_VERSION, "type": "pong", "ts": message.get("ts")},
                    )
                elif message_type == "request":
                    request_id = message.get("request_id")
                    profile_id = message.get("profile")
                    if not isinstance(request_id, str) or request_id in self.running:
                        continue
                    if isinstance(profile_id, str):
                        self.request_profiles[request_id] = profile_id
                    task = asyncio.create_task(self.run_request(websocket, message))
                    self.running[request_id] = task
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
            except (OSError, TimeoutError, websockets.ConnectionClosed, json.JSONDecodeError) as exc:
                print(f"连接中断: {exc}; {delay} 秒后重试", file=sys.stderr, flush=True)
            finally:
                for task in list(self.running.values()):
                    task.cancel()
                self.running.clear()
                self.request_profiles.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

