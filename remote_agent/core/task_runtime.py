from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from remote_agent.profiles.base import ProfileHandler
from remote_agent.profiles.common.filesystem import WorkspaceError
from remote_agent.profiles.common.planned_action import (
    check_workspace_target,
    parse_planned_action,
    public_planned_result,
)
from remote_agent.runtimes.base import ActionDecision, LocalAgentRuntime, RuntimeTask
from remote_agent.runtimes.health import HEALTH_PROFILE, OBSERVATION_TOOLS
from remote_agent.runtimes.jsonrpc import JsonRpcError

from .limits import MAX_CONCURRENT_REQUESTS_PER_AGENT
from .version import PROTOCOL_VERSION


TASK_PROTOCOL_VERSION = 1
_HEALTH_PROMPT = (
    "Run the read-only device health check by calling each of the four supplied health tools "
    "exactly once with empty arguments. Do not use any other capability. Finish after the "
    "four calls have returned. The gateway computes the health report from its own evidence."
)


class TaskSender(Protocol):
    async def send(self, websocket: Any, message: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class HealthStep:
    step_id: str
    action: str


HealthResult = tuple[dict[str, Any], dict[str, Any]] | None


class TaskRuntime:
    def __init__(
        self,
        sender: TaskSender,
        handlers: dict[str, ProfileHandler],
        runtimes: dict[str, LocalAgentRuntime] | None = None,
    ):
        self.sender = sender
        self.handlers = handlers
        self.runtimes = runtimes or {}
        self.running: dict[str, asyncio.Task[None]] = {}
        self.decisions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.action_tasks: dict[str, str] = {}
        self.task_sequences: dict[str, int] = {}
        self.health_steps: dict[str, set[str]] = {}
        self.cancelled_health_steps: dict[str, set[str]] = {}
        self.active_health_steps: dict[str, tuple[str, asyncio.Task[HealthResult]]] = {}

    @property
    def active_count(self) -> int:
        return len(self.running)

    async def dispatch(
        self,
        websocket: Any,
        message: dict[str, Any],
        *,
        reserved_count: int = 0,
    ) -> None:
        task_id = message.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return
        if task_id in self.running:
            await self._send_error(websocket, task_id, "Task 已在执行")
            return
        if len(self.running) + reserved_count >= MAX_CONCURRENT_REQUESTS_PER_AGENT:
            await self._send_error(
                websocket,
                task_id,
                (
                    "Agent 当前忙碌；每台 Agent 最多同时执行 "
                    f"{MAX_CONCURRENT_REQUESTS_PER_AGENT} 个请求"
                ),
            )
            return
        task = asyncio.create_task(self._run(websocket, message))
        self.running[task_id] = task

    async def decide(self, message: dict[str, Any]) -> None:
        action_request_id = message.get("action_request_id")
        task_id = message.get("task_id")
        if not isinstance(action_request_id, str) or not isinstance(task_id, str):
            return
        future = self.decisions.get(action_request_id)
        if future is None or future.done():
            return
        if self.action_tasks.get(action_request_id) != task_id:
            return
        future.set_result(
            {
                "decision": message.get("decision"),
                "reason": str(message.get("reason") or ""),
                "policy_context": message.get("policy_context", {}),
            }
        )

    async def cancel(self, task_id: str, websocket: Any = None) -> None:
        task = self.running.get(task_id)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.running.pop(task_id, None)
        if websocket is not None:
            await self._send_error(websocket, task_id, "Task 已取消")

    def cancel_health_step(self, task_id: str, step_id: str) -> None:
        if step_id not in self.health_steps.get(task_id, ()):
            return
        self.cancelled_health_steps[task_id].add(step_id)
        active = self.active_health_steps.get(task_id)
        if active and active[0] == step_id:
            active[1].cancel()

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.cancel(task_id) for task_id in tuple(self.running)))
        self.running.clear()
        self.decisions.clear()
        self.action_tasks.clear()

    async def _run(self, websocket: Any, message: dict[str, Any]) -> None:
        task_id = str(message["task_id"])
        try:
            if message.get("task_protocol", TASK_PROTOCOL_VERSION) != TASK_PROTOCOL_VERSION:
                raise ValueError("不支持的 Task 协议版本")
            if "plan_hash" in message:
                await self._run_planned(websocket, task_id, message)
                return
            steps = self._parse_steps(message.get("steps"))
            self.health_steps[task_id] = {step.step_id for step in steps.values()}
            self.cancelled_health_steps[task_id] = set()
            provider = message.get("requested_runtime")
            if provider is not None and (
                not isinstance(provider, str)
                or provider not in self.runtimes
                or steps.keys() != OBSERVATION_TOOLS.keys()
            ):
                raise ValueError("目标设备未启用指定 Runtime 或健康检查计划不完整")
            deadline = message.get("deadline")
            budget = min(120, deadline - time.time()) if isinstance(deadline, int) else 120
            if budget <= 0:
                raise TimeoutError
            async with asyncio.timeout(budget):
                await self._send(
                    websocket,
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "task_ack",
                        "task_protocol": TASK_PROTOCOL_VERSION,
                        "task_id": task_id,
                        "status": "accepted",
                    },
                )
                if provider is None:
                    results = []
                    for step in steps.values():
                        observed = await self._observe_step(websocket, task_id, step)
                        if observed is not None:
                            results.append(observed[0])
                    result = {"steps": results}
                else:
                    result = await self._run_runtime(websocket, task_id, self.runtimes[provider], steps)
                await self._send(
                    websocket,
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "task_result",
                        "task_protocol": TASK_PROTOCOL_VERSION,
                        "task_id": task_id,
                        "ok": True,
                        "result": result,
                    },
                )
        except TimeoutError:
            await self._send_error(websocket, task_id, "Task 或运行时等待超时")
        except WorkspaceError as exc:
            await self._send_error(websocket, task_id, f"workspace.{exc.code}")
        except (PermissionError, ValueError, JsonRpcError) as exc:
            await self._send_error(websocket, task_id, str(exc))
        except Exception:
            await self._send_error(websocket, task_id, "设备观察或运行时执行失败")
        finally:
            self.health_steps.pop(task_id, None)
            self.cancelled_health_steps.pop(task_id, None)
            self.active_health_steps.pop(task_id, None)
            self.task_sequences.pop(task_id, None)
            self.running.pop(task_id, None)

    async def _run_planned(self, websocket: Any, task_id: str, message: dict[str, Any]) -> None:
        plan_hash = message["plan_hash"]
        deadline = message.get("deadline")
        steps = message.get("steps")
        if (
            not isinstance(plan_hash, str) or len(plan_hash) != 64
            or any(character not in "0123456789abcdef" for character in plan_hash)
            or not isinstance(deadline, int) or isinstance(deadline, bool)
            or message.get("requested_runtime") is not None
            or not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict)
        ):
            raise ValueError("单步受控计划的协议或授权边界无效")
        step = steps[0]
        step_id = _text(step.get("step_id"), "step_id")
        operation = parse_planned_action(step.get("operation"), step.get("params"))
        handler = self.handlers.get(operation.profile)
        if handler is None or operation.action not in handler.actions:
            raise ValueError("设备未声明计划所需的 Profile 或 Action")
        action_request_id = f"action_{uuid.uuid4().hex}"
        fields = {
            "step_id": step_id,
            "profile": operation.profile,
            "action": operation.action,
            "action_request_id": action_request_id,
        }
        budget = min(30, deadline - time.time())
        if budget <= 0:
            raise TimeoutError
        future = asyncio.get_running_loop().create_future()
        self.decisions[action_request_id] = future
        self.action_tasks[action_request_id] = task_id
        try:
            async with asyncio.timeout(budget):
                await self._send(websocket, {
                    "v": PROTOCOL_VERSION, "type": "task_action_request", "task_id": task_id,
                    "task_protocol": TASK_PROTOCOL_VERSION, "plan_hash": plan_hash,
                    "operation": operation.operation, "risk_level": operation.risk_level,
                    "params": operation.params, **fields,
                })
                decision = await future
                context = decision.get("policy_context")
                revision = context.get("device_policy_revision") if isinstance(context, dict) else None
                device_grant = (
                    operation.operation in {"workspace.create", "workspace.edit"}
                    and operation.risk_level == "L1" and type(revision) is int and revision > 0
                    and context.get("matched_rules") == ["operation_plan.device_l1_path"]
                )
                if (
                    decision.get("decision") != "allow"
                    or not isinstance(context, dict)
                    or context.get("plan_hash") != plan_hash
                    or context.get("allowed_scope") != operation.scope
                    or context.get("risk_level") != operation.risk_level
                    or context.get("expires_at") != deadline
                    or (operation.risk_level != "L0" and not context.get("approval_id") and not device_grant)
                    or time.time() >= deadline
                ):
                    raise PermissionError("Gateway 未授权这一版本的计划动作")
                await self._send_event(websocket, task_id, "action_started", fields, step_id=step_id)
                if time.time() >= deadline:
                    raise TimeoutError
                path = operation.params.get("path")
                if path is not None:
                    check_workspace_target(handler.context.workdir, path, create=operation.action == "write")
                result = public_planned_result(
                    operation, await handler.execute(operation.action, action_request_id, operation.handler_params),
                )
                if operation.profile == "workspace.v1" and operation.risk_level != "L0":
                    assert path is not None
                    check_workspace_target(handler.context.workdir, path)
                    checked = await handler.execute("read", action_request_id, {"path": path, "limit": 1})
                    if checked.get("path") != path or checked.get("sha256") != result["sha256"]:
                        raise ValueError("verification_failed: 文件写入后的摘要不匹配")
                    result["verified"] = True
                await self._send_event(websocket, task_id, "action_completed", fields, step_id=step_id)
                await self._send(websocket, {
                    "v": PROTOCOL_VERSION, "type": "task_result", "task_id": task_id,
                    "task_protocol": TASK_PROTOCOL_VERSION, "ok": True, "result": {**fields, "result": result},
                })
        except (asyncio.CancelledError, TimeoutError):
            await handler.cancel(action_request_id)
            raise
        finally:
            self.decisions.pop(action_request_id, None)
            self.action_tasks.pop(action_request_id, None)

    def _parse_steps(self, value: object) -> dict[str, HealthStep]:
        if not isinstance(value, list) or not 0 < len(value) <= len(OBSERVATION_TOOLS):
            raise ValueError("健康检查步骤数量无效")
        if HEALTH_PROFILE not in self.handlers:
            raise ValueError("设备未加载 system_observe.v1")
        steps: dict[str, HealthStep] = {}
        ids: set[str] = set()
        for step in value:
            if not isinstance(step, dict):
                raise ValueError("健康检查步骤无效")
            step_id = _text(step.get("step_id"), "step_id")
            action = _text(step.get("action"), "action")
            if (
                step.get("profile") != HEALTH_PROFILE
                or action not in OBSERVATION_TOOLS
                or action not in self.handlers[HEALTH_PROFILE].actions
                or step.get("params", {}) != {}
                or step.get("risk_level", "L0") != "L0"
                or action in steps
                or step_id in ids
            ):
                raise ValueError("一期仅允许无参数、不可重复的固定 L0 健康检查步骤")
            steps[action] = HealthStep(step_id, action)
            ids.add(step_id)
        return steps

    async def _observe_step(self, websocket: Any, task_id: str, step: HealthStep) -> HealthResult:
        cancelled = self.cancelled_health_steps[task_id]
        if step.step_id in cancelled:
            return None
        child = asyncio.create_task(self._execute_step(websocket, task_id, step))
        self.active_health_steps[task_id] = (step.step_id, child)
        try:
            return await child
        except asyncio.CancelledError:
            if step.step_id not in cancelled or asyncio.current_task().cancelling():
                raise
            return None
        finally:
            self.active_health_steps.pop(task_id, None)

    async def _execute_step(
        self, websocket: Any, task_id: str, step: HealthStep,
    ) -> HealthResult:
        action_request_id = f"action_{uuid.uuid4().hex}"
        decision_future = asyncio.get_running_loop().create_future()
        self.decisions[action_request_id] = decision_future
        self.action_tasks[action_request_id] = task_id
        action_fields = {
            "step_id": step.step_id,
            "action_request_id": action_request_id,
            "profile": HEALTH_PROFILE,
            "action": step.action,
        }
        handler = self.handlers[HEALTH_PROFILE]
        try:
            await self._send_event(websocket, task_id, "step_started", action_fields, step_id=step.step_id)
            await self._send(websocket, {
                "v": PROTOCOL_VERSION,
                "type": "task_action_request",
                "task_protocol": TASK_PROTOCOL_VERSION,
                "task_id": task_id,
                "params": {},
                "risk_level": "L0",
                **action_fields,
            })
            decision = await asyncio.wait_for(decision_future, timeout=30)
            context = decision.get("policy_context")
            if decision.get("decision") == "deny" and isinstance(context, dict) and context.get("step_cancelled") is True:
                self.cancelled_health_steps[task_id].add(step.step_id)
                return None
            if (
                decision.get("decision") != "allow"
                or not isinstance(context, dict)
                or context.get("risk_level") != "L0"
                or context.get("allowed_scope") != {"profile": HEALTH_PROFILE, "action": step.action}
            ):
                await self._send_event(websocket, task_id, "action_blocked", action_fields, step_id=step.step_id)
                raise PermissionError("Gateway 未授权当前 L0 观察动作")
            await self._send_event(websocket, task_id, "action_started", action_fields, step_id=step.step_id)
            result = await handler.execute(step.action, action_request_id, {})
            await self._send_event(websocket, task_id, "action_completed", action_fields, step_id=step.step_id)
            return {**action_fields, "result": result}, context
        except asyncio.CancelledError:
            await handler.cancel(action_request_id)
            raise
        finally:
            self.decisions.pop(action_request_id, None)
            self.action_tasks.pop(action_request_id, None)

    async def _run_runtime(
        self,
        websocket: Any,
        task_id: str,
        runtime: LocalAgentRuntime,
        steps: dict[str, HealthStep],
    ) -> dict[str, Any]:
        session = await runtime.start(
            RuntimeTask(task_id, "Read-only health check", _HEALTH_PROMPT, tuple(steps)),
        )
        runtime_info = {
            "session_id": session.session_id,
            "provider_id": session.provider_id,
            "runtime_version": session.provider_version,
            "adapter_version": session.adapter_version,
            "mode": session.mode,
        }
        status = "failed"
        results: dict[str, dict[str, Any]] = {}
        cancelled = self.cancelled_health_steps[task_id]
        calls = 0
        try:
            await self._send_event(websocket, task_id, "runtime_started", runtime_info)
            async for event in runtime.events(session):
                if event.event_type == "turn_completed":
                    if any(action not in results and step.step_id not in cancelled for action, step in steps.items()):
                        raise ValueError("Runtime 未完成全部已授权观察，无健康检查结论")
                    status = "completed"
                    return {"steps": list(results.values()), "runtime": runtime_info}
                calls += 1
                if calls > 2 * len(steps):
                    raise ValueError("Runtime 健康检查超过工具请求预算")
                if event.event_type == "action_blocked":
                    await self._send_event(websocket, task_id, "action_blocked", event.payload)
                    continue
                if event.event_type != "action_request" or not event.action_request_id:
                    continue
                action = event.payload.get("action")
                if not isinstance(action, str) or action not in steps or action in results:
                    await runtime.decide_action(session, ActionDecision(event.action_request_id, "deny"))
                    await self._send_event(
                        websocket, task_id, "action_blocked", {"reason": "动作不在计划中或已执行"},
                    )
                    continue
                try:
                    observed = await self._observe_step(websocket, task_id, steps[action])
                except PermissionError:
                    await runtime.decide_action(session, ActionDecision(event.action_request_id, "deny"))
                    raise
                if observed is None or steps[action].step_id in cancelled:
                    await runtime.decide_action(session, ActionDecision(event.action_request_id, "deny"))
                    continue
                result, context = observed
                await runtime.decide_action(session, ActionDecision(
                    event.action_request_id, "allow",
                    policy_context=context, result=_runtime_observation(action, result["result"]),
                ))
                results[action] = result
            raise ValueError("运行时未返回成功的 turn")
        except asyncio.CancelledError:
            status = "cancelled"
            await runtime.cancel(session)
            raise
        finally:
            await runtime.close(session)
            await self._send_event(
                websocket, task_id, "runtime_finished",
                {"session_id": session.session_id, "status": status},
            )

    async def _send_event(
        self,
        websocket: Any,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> None:
        sequence = self.task_sequences.get(task_id, 0) + 1
        self.task_sequences[task_id] = sequence
        message: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": "task_event",
            "task_protocol": TASK_PROTOCOL_VERSION,
            "task_id": task_id,
            "event_id": f"event_{uuid.uuid4().hex}",
            "event_type": event_type,
            "sequence": sequence,
            "payload": payload,
        }
        if step_id is not None:
            message["step_id"] = step_id
        await self._send(websocket, message)

    async def _send_error(self, websocket: Any, task_id: str, error: str) -> None:
        await self._send(
            websocket,
            {
                "v": PROTOCOL_VERSION,
                "type": "task_error",
                "task_protocol": TASK_PROTOCOL_VERSION,
                "task_id": task_id,
                "error": error,
            },
        )

    async def _send(self, websocket: Any, message: dict[str, Any]) -> None:
        try:
            await self.sender.send(websocket, message)
        except Exception:
            pass


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 无效")
    return value.strip()


def _runtime_observation(action: str, result: dict[str, Any]) -> dict[str, Any]:
    if action == "get_host_info":
        os_name = result.get("os")
        return {"os": os_name if os_name in {"windows", "macos", "linux"} else "other"}
    if action == "get_resources":
        memory = result.get("memory") or {}
        return {
            "memory_used_percent": memory.get("utilization_percent"),
            "disk_free_percent": [
                round(100 * disk["free_bytes"] / disk["total_bytes"], 1)
                for disk in result.get("disks", []) if disk.get("total_bytes")
            ],
        }
    if action == "get_processes":
        return {
            "available": result.get("available", False),
            "process_count_sampled": len(result.get("processes", [])),
        }
    return {"interface_count": len(result.get("interfaces", []))}
