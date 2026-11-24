from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from remote_agent.core.limits import MAX_CONCURRENT_REQUESTS_PER_AGENT

from .agent_protocol import AGENT_PROTOCOL_VERSION
from .database import Database
from .execution_controls import EXECUTION_STOPPED, operation_group
from .runtime_catalog import HEALTH_RUNTIME_CATALOG
from .task_protocol import (
    make_task_action_decision,
    make_task_cancel,
    make_task_start,
    make_task_step_cancel,
    validate_task_message,
)


class AgentError(Exception):
    pass


class AgentOfflineError(AgentError):
    pass


class AgentCommandError(AgentError):
    pass


class AgentBusyError(AgentCommandError):
    pass


class AgentExecutionStopped(AgentCommandError):
    def __init__(self) -> None:
        super().__init__(EXECUTION_STOPPED)


@dataclass(slots=True)
class AgentConnection:
    agent_id: str
    user_id: str
    websocket: WebSocket
    features: dict[str, Any] = field(default_factory=dict)
    runtimes: list[dict[str, Any]] = field(default_factory=list)
    connected_at: float = field(default_factory=time.time)
    last_pong: float = field(default_factory=time.monotonic)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connection_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    async def send(
        self, message: dict[str, Any], *, before_send: Callable[[], None] | None = None,
    ) -> None:
        async with self.send_lock:
            if before_send is not None:
                before_send()
            await self.websocket.send_json(message)

    def supports_task_runtime(self, provider_id: str | None) -> bool:
        if self.features.get("task_protocol", 0) < 1:
            return False
        if provider_id is None:
            return True
        contract = HEALTH_RUNTIME_CATALOG.get(provider_id)
        return (
            contract is not None
            and self.features.get("runtime_adapter") is True
            and self.features.get("action_interception") == contract.action_interception
            and any(contract.accepts(runtime) for runtime in self.runtimes)
        )


@dataclass(slots=True)
class PendingRequest:
    agent_id: str
    connection: AgentConnection
    future: asyncio.Future[dict[str, Any]]
    operation: str
    revision: int
    dispatched: bool = False
    interrupted: bool = False


@dataclass(slots=True)
class PendingTask:
    agent_id: str
    task_id: str
    connection: AgentConnection
    operation: str
    runtime: str
    revision: int
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    dispatched: bool = False
    interrupted: bool = False
    health_step_ids: frozenset[str] = frozenset()
    cancelled_steps: set[str] = field(default_factory=set)


class AgentHub:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._connections: dict[str, AgentConnection] = {}
        self._pending: dict[str, PendingRequest] = {}
        self._pending_tasks: dict[tuple[str, str], PendingTask] = {}
        self._lock = asyncio.Lock()

    async def register(self, connection: AgentConnection) -> None:
        async with self._lock:
            previous = self._connections.get(connection.agent_id)
            self._connections[connection.agent_id] = connection
            failed = []
            failed_tasks: list[PendingTask] = []
            if previous is not None and previous is not connection:
                failed = self._remove_pending_for_connection(previous)
                failed_tasks = self._remove_tasks_for_connection(previous)
        self._fail_pending(failed, "Agent 已在新的连接上线")
        self._fail_tasks(failed_tasks, "Agent 已在新的连接上线")
        if self._db is not None:
            self._db.resolve_device_offline_alerts(connection.user_id, connection.agent_id)
            if previous is not None and previous is not connection:
                self._db.record_device_alert(
                    connection.user_id, connection.agent_id, "device_connection_replaced",
                    "旧连接尚未关闭时发生了重新连接", "确认设备上未重复启动 Agent，检查连接和设备凭据",
                    basis={"signal": "connection_replaced"},
                )
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
            failed = self._remove_pending_for_connection(current)
            failed_tasks = self._remove_tasks_for_connection(current)
        self._fail_pending(failed, "Agent 连接已断开")
        self._fail_tasks(failed_tasks, "Agent 连接已断开")
        if self._db is not None:
            self._db.record_device_alert(
                current.user_id, agent_id, "device_offline", "设备连接已断开",
                "检查设备进程和网络；已有操作结果不明时须重新核对",
                basis={"signal": "websocket_disconnected"},
            )

    def _remove_pending_for_connection(
        self, connection: AgentConnection
    ) -> list[PendingRequest]:
        request_ids = [
            request_id
            for request_id, pending in self._pending.items()
            if pending.connection is connection
        ]
        return [self._pending.pop(request_id) for request_id in request_ids]

    def _remove_tasks_for_connection(self, connection: AgentConnection) -> list[PendingTask]:
        task_keys = [
            task_key
            for task_key, pending in self._pending_tasks.items()
            if pending.connection is connection
        ]
        return [self._pending_tasks.pop(task_key) for task_key in task_keys]

    @staticmethod
    def _fail_pending(pending_requests: list[PendingRequest], message: str) -> None:
        for pending in pending_requests:
            if not pending.future.done():
                pending.future.set_exception(AgentOfflineError(message))

    @staticmethod
    def _fail_tasks(pending_tasks: list[PendingTask], message: str) -> None:
        for pending in pending_tasks:
            pending.queue.put_nowait(
                {
                    "v": AGENT_PROTOCOL_VERSION,
                    "type": "task_error",
                    "task_id": pending.task_id,
                    "error": message,
                }
            )

    def is_online(self, agent_id: str) -> bool:
        return agent_id in self._connections

    def supports_task_runtime(self, agent_id: str, provider_id: str | None) -> bool:
        connection = self._connections.get(agent_id)
        return (
            connection is not None
            and not self.execution_blocked(connection.user_id, agent_id, runtime=provider_id or "")
            and connection.supports_task_runtime(provider_id)
        )

    def execution_revision(self) -> int:
        return self._db.execution_revision() if self._db is not None else 0

    def execution_blocked(
        self, user_id: str, agent_id: str, *, runtime: str = "", operation: str = "", revision: int | None = None,
    ) -> bool:
        return self._db is not None and self._db.execution_blocked(
            user_id, agent_id, runtime=runtime, operation=operation, revision=revision,
        )

    def planned_connection(self, user_id: str, agent_id: str) -> str | None:
        connection = self._connections.get(agent_id)
        if connection and connection.user_id == user_id and connection.features.get("planned_actions") is True:
            return connection.connection_id
        return None

    def online_agent_ids(self) -> set[str]:
        return set(self._connections)

    def _active_count(self, agent_id: str) -> int:
        return sum(1 for pending in self._pending.values() if pending.agent_id == agent_id) + sum(
            1 for pending in self._pending_tasks.values() if pending.agent_id == agent_id
        )

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
        pending = PendingRequest(
            agent_id, connection, future, operation_group(profile, action), self.execution_revision(),
        )
        async with self._lock:
            if self._connections.get(agent_id) is not connection:
                raise AgentOfflineError("Agent 当前离线")
            if self._active_count(agent_id) >= MAX_CONCURRENT_REQUESTS_PER_AGENT:
                raise AgentBusyError(
                    "Agent 当前忙碌；每台 Agent 最多同时执行 "
                    f"{MAX_CONCURRENT_REQUESTS_PER_AGENT} 个请求"
                )
            self._pending[request_id] = pending

        def check_dispatch() -> None:
            if pending.interrupted or self.execution_blocked(
                connection.user_id, agent_id, operation=pending.operation, revision=pending.revision,
            ):
                raise AgentExecutionStopped()

        def before_send() -> None:
            check_dispatch()
            pending.dispatched = True

        try:
            await connection.send(
                {
                    "v": AGENT_PROTOCOL_VERSION,
                    "type": "request",
                    "request_id": request_id,
                    "profile": profile,
                    "action": action,
                    "params": params,
                },
                before_send=before_send,
            )
            message = await asyncio.wait_for(future, timeout=timeout + 5)
            check_dispatch()
        except AgentExecutionStopped:
            if pending.dispatched and not pending.interrupted:
                await self._cancel_remote(connection, request_id)
            raise
        except asyncio.TimeoutError as exc:
            await self._cancel_remote(connection, request_id)
            raise AgentCommandError("远程命令等待超时") from exc
        except asyncio.CancelledError:
            await self._cancel_remote(connection, request_id)
            raise
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

    async def _cancel_remote(self, connection: AgentConnection, request_id: str) -> None:
        try:
            await asyncio.shield(
                asyncio.wait_for(
                    connection.send({
                        "v": AGENT_PROTOCOL_VERSION,
                        "type": "cancel",
                        "request_id": request_id,
                    }),
                    timeout=2,
                )
            )
        except Exception:
            pass

    async def execute_task(
        self,
        agent_id: str,
        task_id: str,
        objective: str,
        steps: list[dict[str, Any]],
        timeout: float,
        *,
        risk_policy: dict[str, Any] | None = None,
        deadline: int | None = None,
        requested_runtime: str | None = None,
        on_message: Any = None,
        plan_hash: str | None = None,
        expected_connection_id: str | None = None,
        authorize_dispatch: Callable[[], None] | None = None,
        execution_revision: int | None = None,
        execution_operation: str = "",
    ) -> dict[str, Any]:
        connection = self._connections.get(agent_id)
        if connection is None:
            raise AgentOfflineError("Agent 当前离线")
        if connection.features.get("task_protocol", 0) < 1:
            raise AgentCommandError("Agent 不支持 Task Protocol v1")
        if not connection.supports_task_runtime(requested_runtime):
            raise AgentCommandError("Agent 当前未启用指定的健康检查 Runtime")
        if plan_hash is not None and (
            connection.features.get("planned_actions") is not True
            or not expected_connection_id
            or requested_runtime is not None
            or deadline is None
        ):
            raise AgentCommandError("设备不支持受控计划或缺少计划授权边界")

        key = (agent_id, task_id)
        pending = PendingTask(
            agent_id, task_id, connection, execution_operation, requested_runtime or "",
            self.execution_revision() if execution_revision is None else execution_revision,
            health_step_ids=(
                frozenset(step["step_id"] for step in steps)
                if plan_hash is None and connection.features.get("health_step_cancel") is True else frozenset()
            ),
        )

        def check_controls() -> None:
            if pending.interrupted or self.execution_blocked(
                connection.user_id, agent_id,
                runtime=pending.runtime, operation=pending.operation, revision=pending.revision,
            ):
                raise AgentExecutionStopped()

        def check_dispatch() -> None:
            check_controls()
            if self._connections.get(agent_id) is not connection:
                raise AgentOfflineError("设备连接已变化，请重新提交计划")
            if plan_hash is not None and connection.connection_id != expected_connection_id:
                raise AgentOfflineError("设备连接已变化，请重新提交计划")
            if deadline is not None and time.time() >= deadline:
                raise AgentCommandError("计划授权已过期")
            if authorize_dispatch is not None:
                authorize_dispatch()

        def before_task_start() -> None:
            check_dispatch()
            pending.dispatched = True

        async with self._lock:
            if self._connections.get(agent_id) is not connection:
                raise AgentOfflineError("Agent 当前离线")
            if key in self._pending_tasks:
                raise AgentBusyError("该 Agent 已有同一 Task 在执行")
            if self._active_count(agent_id) >= MAX_CONCURRENT_REQUESTS_PER_AGENT:
                raise AgentBusyError(
                    "Agent 当前忙碌；每台 Agent 最多同时执行 "
                    f"{MAX_CONCURRENT_REQUESTS_PER_AGENT} 个任务"
                )
            self._pending_tasks[key] = pending

        try:
            await connection.send(
                make_task_start(
                    task_id,
                    objective,
                    steps,
                    risk_policy=risk_policy,
                    deadline=deadline,
                    requested_runtime=requested_runtime,
                    plan_hash=plan_hash,
                ),
                before_send=before_task_start,
            )
            started = time.monotonic()
            while True:
                remaining = timeout - (time.monotonic() - started)
                try:
                    if remaining <= 0:
                        raise TimeoutError
                    message = await asyncio.wait_for(pending.queue.get(), remaining)
                except asyncio.TimeoutError as exc:
                    await self._cancel_task_remote(connection, task_id, "Task 等待超时")
                    raise AgentCommandError("远程 Task 等待超时") from exc
                if pending.interrupted:
                    raise AgentExecutionStopped()
                message_type = message.get("type")
                if message_type in {"task_action_request", "task_result"}:
                    check_controls()
                if message_type == "task_action_request":
                    decision = None
                    if on_message is not None:
                        decision = await on_message(message)
                    if not isinstance(decision, dict):
                        decision = {
                            "decision": "deny",
                            "reason": "Gateway 未提供 Action 决策",
                            "policy_context": {},
                        }
                    response = make_task_action_decision(
                        task_id, message["action_request_id"], decision.get("decision", "deny"),
                        reason=str(decision.get("reason", "")), policy_context=decision.get("policy_context", {}),
                    )

                    def before_action_decision() -> None:
                        if message["step_id"] in pending.cancelled_steps:
                            response.update(decision="deny", reason="用户取消观察步骤", policy_context={"step_cancelled": True})
                        elif response["decision"] == "allow":
                            check_dispatch()

                    await connection.send(response, before_send=before_action_decision)
                    continue
                if on_message is not None:
                    await on_message(message)
                if message_type == "task_result":
                    if not message.get("ok"):
                        raise AgentCommandError(str(message.get("error") or "Task 执行失败"))
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise AgentCommandError("Agent 返回了无效 Task 结果")
                    return result
                if message_type == "task_error":
                    raise AgentCommandError(str(message.get("error") or "Task 执行失败"))
        except AgentExecutionStopped:
            if pending.dispatched and not pending.interrupted:
                await self._cancel_task_remote(connection, task_id, EXECUTION_STOPPED)
            raise
        except asyncio.CancelledError:
            await self._cancel_task_remote(connection, task_id, "Gateway 调用已取消")
            raise
        except ValueError:
            if pending.dispatched:
                await self._cancel_task_remote(connection, task_id, "Task 协议校验失败")
            raise
        except (RuntimeError, OSError) as exc:
            raise AgentOfflineError("无法向 Agent 发送 Task") from exc
        finally:
            async with self._lock:
                if self._pending_tasks.get(key) is pending:
                    self._pending_tasks.pop(key)

    async def _cancel_task_remote(
        self,
        connection: AgentConnection,
        task_id: str,
        reason: str,
    ) -> None:
        try:
            await asyncio.shield(asyncio.wait_for(connection.send(make_task_cancel(task_id, reason)), timeout=2))
        except Exception:
            pass

    async def cancel_task(
        self,
        agent_id: str,
        task_id: str,
        reason: str = "Task 已取消",
    ) -> bool:
        pending = self._pending_tasks.get((agent_id, task_id))
        if pending is None:
            return False
        await self._cancel_task_remote(pending.connection, task_id, reason)
        return True

    def can_cancel_health_step(self, agent_id: str, task_id: str, step_id: str) -> bool:
        pending = self._pending_tasks.get((agent_id, task_id))
        return (
            pending is not None and pending.dispatched and not pending.interrupted
            and step_id in pending.health_step_ids and step_id not in pending.cancelled_steps
        )

    async def cancel_health_step(self, agent_id: str, task_id: str, step_id: str) -> None:
        pending = self._pending_tasks.get((agent_id, task_id))
        if pending is None or step_id not in pending.health_step_ids:
            return
        pending.cancelled_steps.add(step_id)
        try:
            await asyncio.wait_for(pending.connection.send(make_task_step_cancel(task_id, step_id)), timeout=2)
        except Exception:
            pass

    async def enforce_execution_stops(self, user_id: str | None = None) -> int:
        cancellations = []
        interrupted = 0
        for request_id, pending in list(self._pending.items()):
            if user_id is not None and pending.connection.user_id != user_id:
                continue
            if pending.future.done() or not self.execution_blocked(
                pending.connection.user_id, pending.agent_id, operation=pending.operation, revision=pending.revision,
            ):
                continue
            pending.interrupted = True
            pending.future.set_result({"ok": False, "error": EXECUTION_STOPPED})
            interrupted += 1
            if pending.dispatched:
                cancellations.append(self._cancel_remote(pending.connection, request_id))
        for pending in list(self._pending_tasks.values()):
            if user_id is not None and pending.connection.user_id != user_id:
                continue
            if pending.interrupted or not self.execution_blocked(
                pending.connection.user_id, pending.agent_id,
                runtime=pending.runtime, operation=pending.operation, revision=pending.revision,
            ):
                continue
            pending.interrupted = True
            pending.queue.put_nowait({"type": "task_error", "task_id": pending.task_id, "error": EXECUTION_STOPPED})
            interrupted += 1
            if pending.dispatched:
                cancellations.append(self._cancel_task_remote(pending.connection, pending.task_id, EXECUTION_STOPPED))
        if cancellations:
            await asyncio.gather(*cancellations)
        return interrupted

    async def resolve(self, connection: AgentConnection, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str):
            return
        pending = self._pending.get(request_id)
        if (
            pending is None
            or pending.connection is not connection
            or pending.future.done()
        ):
            return
        pending.future.set_result(message)

    async def resolve_task(self, connection: AgentConnection, message: dict[str, Any]) -> None:
        try:
            normalized = validate_task_message(message)
        except ValueError:
            return
        pending = self._pending_tasks.get((connection.agent_id, normalized["task_id"]))
        if pending is None or pending.connection is not connection:
            return
        pending.queue.put_nowait(normalized)

    def record_pong(self, connection: AgentConnection) -> None:
        if self._connections.get(connection.agent_id) is connection:
            connection.last_pong = time.monotonic()

    async def heartbeat(self, timeout: float = 60) -> None:
        now = time.monotonic()
        stale: dict[str, AgentConnection] = {}
        for agent_id, connection in list(self._connections.items()):
            if now - connection.last_pong > timeout:
                stale[agent_id] = connection
                try:
                    await connection.websocket.close(code=4002, reason="心跳超时")
                except RuntimeError:
                    pass
                continue
            try:
                await connection.send(
                    {
                        "v": AGENT_PROTOCOL_VERSION,
                        "type": "ping",
                        "ts": int(time.time()),
                    }
                )
            except Exception:
                stale[agent_id] = connection
                try:
                    await connection.websocket.close()
                except RuntimeError:
                    pass
        for agent_id, connection in stale.items():
            await self.unregister(agent_id, connection.websocket)

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
