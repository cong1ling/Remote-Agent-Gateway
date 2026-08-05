from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket


class AgentError(Exception):
    pass


class AgentOfflineError(AgentError):
    pass


class AgentCommandError(AgentError):
    pass


@dataclass(slots=True)
class AgentConnection:
    agent_id: str
    user_id: str
    websocket: WebSocket
    connected_at: float = field(default_factory=time.time)
    last_pong: float = field(default_factory=time.monotonic)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_json(message)


@dataclass(slots=True)
class PendingRequest:
    agent_id: str
    future: asyncio.Future[dict[str, Any]]


class AgentHub:
    def __init__(self) -> None:
        self._connections: dict[str, AgentConnection] = {}
        self._pending: dict[str, PendingRequest] = {}
        self._lock = asyncio.Lock()

    async def register(self, connection: AgentConnection) -> None:
        async with self._lock:
            previous = self._connections.get(connection.agent_id)
            self._connections[connection.agent_id] = connection
        if previous is not None and previous.websocket is not connection.websocket:
            try:
                await previous.websocket.close(code=4001, reason="Agent 已在其他连接上线")
            except RuntimeError:
                pass

    async def unregister(self, agent_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            current = self._connections.get(agent_id)
            if current is None or current.websocket is not websocket:
                return
            self._connections.pop(agent_id, None)
            failed = [
                (request_id, pending)
                for request_id, pending in self._pending.items()
                if pending.agent_id == agent_id
            ]
            for request_id, _ in failed:
                self._pending.pop(request_id, None)
        for _, pending in failed:
            if not pending.future.done():
                pending.future.set_exception(AgentOfflineError("Agent 连接已断开"))

    def is_online(self, agent_id: str) -> bool:
        return agent_id in self._connections

    def online_agent_ids(self) -> set[str]:
        return set(self._connections)

    async def execute(
        self,
        agent_id: str,
        profile: str,
        action: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        connection = self._connections.get(agent_id)
        if connection is None:
            raise AgentOfflineError("Agent 当前离线")

        request_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        async with self._lock:
            if self._connections.get(agent_id) is not connection:
                raise AgentOfflineError("Agent 当前离线")
            self._pending[request_id] = PendingRequest(agent_id=agent_id, future=future)

        try:
            await connection.send(
                {
                    "v": 1,
                    "type": "request",
                    "request_id": request_id,
                    "profile": profile,
                    "action": action,
                    "params": params,
                }
            )
            message = await asyncio.wait_for(future, timeout=timeout + 5)
        except asyncio.TimeoutError as exc:
            try:
                await connection.send({"v": 1, "type": "cancel", "request_id": request_id})
            except Exception:
                pass
            raise AgentCommandError("远程命令等待超时") from exc
        except (RuntimeError, OSError) as exc:
            raise AgentOfflineError("无法向 Agent 发送请求") from exc
        finally:
            async with self._lock:
                self._pending.pop(request_id, None)

        if not message.get("ok"):
            raise AgentCommandError(str(message.get("error") or "远程执行失败"))
        result = message.get("result")
        if not isinstance(result, dict):
            raise AgentCommandError("Agent 返回了无效结果")
        return result

    async def resolve(self, agent_id: str, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str):
            return
        pending = self._pending.get(request_id)
        if pending is None or pending.agent_id != agent_id or pending.future.done():
            return
        pending.future.set_result(message)

    def record_pong(self, agent_id: str) -> None:
        connection = self._connections.get(agent_id)
        if connection is not None:
            connection.last_pong = time.monotonic()

    async def heartbeat(self, timeout: float = 60) -> list[str]:
        now = time.monotonic()
        stale: list[str] = []
        for agent_id, connection in list(self._connections.items()):
            if now - connection.last_pong > timeout:
                stale.append(agent_id)
                try:
                    await connection.websocket.close(code=4002, reason="心跳超时")
                except RuntimeError:
                    pass
                continue
            try:
                await connection.send({"v": 1, "type": "ping", "ts": int(time.time())})
            except Exception:
                stale.append(agent_id)
                try:
                    await connection.websocket.close()
                except RuntimeError:
                    pass
        return stale

    async def close_all(self) -> None:
        for connection in list(self._connections.values()):
            try:
                await connection.websocket.close(code=1001, reason="网关正在关闭")
            except RuntimeError:
                pass

    async def disconnect(self, agent_id: str, reason: str = "Agent 已被撤销") -> None:
        connection = self._connections.get(agent_id)
        if connection is None:
            return
        try:
            await connection.websocket.close(code=4003, reason=reason)
        except RuntimeError:
            pass

