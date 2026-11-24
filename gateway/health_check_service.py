from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from typing import Any

from .agent_hub import AgentCommandError, AgentExecutionStopped, AgentHub, AgentOfflineError
from .database import ConflictError, Database
from .device_directory import DeviceFilters
from .execution_controls import EXECUTION_STOPPED, STOP_EXPLANATION
from .health_signals import monitor_observation
from .policy_service import PolicyService
from .profiles import validate_profile_result
from .runtime_catalog import HEALTH_RUNTIME_CATALOG
from .task_models import StepRecord, TaskRecord, TaskStatus


HEALTH_ACTIONS = (
    ("get_host_info", "remote_get_host_info"),
    ("get_resources", "remote_get_resources"),
    ("get_processes", "remote_get_processes"),
    ("get_network", "remote_get_network"),
)
HEALTH_TIMEOUT_SECONDS = 120
_RUNTIME_FIELDS = ("session_id", "provider_id", "runtime_version", "adapter_version", "mode")


class HealthCheckError(ValueError):
    pass


class HealthCheckService:
    def __init__(self, db: Database, hub: AgentHub):
        self.db = db
        self.hub = hub
        self.policy = PolicyService()
        self._running: dict[str, asyncio.Task[None]] = {}
        self._active_agents: dict[str, set[str]] = {}

    def create_task(
        self,
        user_id: str,
        *,
        agent_ids: list[str] | None = None,
        target_selector: DeviceFilters | None = None,
        objective: str = "跨设备只读健康检查",
        requested_runtime: str | None = None,
        monitor_expires_at: int | None = None,
        execution_revision: int | None = None,
    ) -> dict[str, Any]:
        if not objective.strip() or (requested_runtime is not None and requested_runtime not in HEALTH_RUNTIME_CATALOG):
            raise HealthCheckError("健康检查名称或 Runtime 无效")
        if monitor_expires_at is not None and (
            not agent_ids or len(agent_ids) != 1 or target_selector is not None or requested_runtime is not None
            or execution_revision is None or monitor_expires_at <= time.time()
        ):
            raise HealthCheckError("巡检只能观察一台明确授权的设备，不能调用 Runtime")
        if execution_revision is None:
            execution_revision = self.hub.execution_revision()
        if target_selector is not None:
            if agent_ids is not None:
                raise HealthCheckError("只能提供 agent_ids 或 target_selector 之一")
            if not target_selector.model_dump(exclude_defaults=True):
                raise HealthCheckError("target_selector 至少需要一个筛选条件")
        agents = self._select_agents(user_id, agent_ids, requested_runtime, target_selector)
        if not agents:
            raise HealthCheckError("没有支持所选 Runtime 的在线健康检查 Agent")
        selected_ids = [agent["id"] for agent in agents]
        task = TaskRecord.new(
            user_id,
            objective,
            target_selector={"agent_ids": selected_ids},
            requested_runtime=requested_runtime,
            risk_policy={"max_level": "L0", "mode": "read_only"},
            approval_policy={"required": False, "reason": "一期健康检查仅允许固定只读 Action"},
            resource_budget={
                "max_devices": len(selected_ids),
                "max_actions_per_device": len(HEALTH_ACTIONS),
                "timeout_seconds": HEALTH_TIMEOUT_SECONDS,
                "execution_revision": execution_revision,
                **({"source": "health_monitor"} if monitor_expires_at is not None else {}),
            },
            deadline=monitor_expires_at,
        )
        self.db.create_task(
            user_id,
            task.objective,
            task_id=task.id,
            status=TaskStatus.CREATED.value,
            target_selector=task.target_selector,
            requested_runtime=task.requested_runtime,
            risk_policy=task.risk_policy,
            approval_policy=task.approval_policy,
            resource_budget=task.resource_budget,
            deadline=task.deadline,
        )
        self.db.append_task_event(task.id, "task.created", {
            "kind": "health_monitor" if monitor_expires_at is not None else "health_check",
        })
        self.db.update_task(user_id, task.id, status=TaskStatus.PLANNED.value)
        self.db.append_task_event(
            task.id,
            "task.planned",
            {"agent_ids": selected_ids, "risk_level": "L0", "requested_runtime": requested_runtime},
        )
        self.db.update_task(user_id, task.id, status=TaskStatus.APPROVED.value)
        self.db.append_task_event(
            task.id,
            "task.approved",
            {"decision": "allow", "reason": "固定只读健康检查无需人工审批"},
        )
        for agent in agents:
            for action, mcp_tool in HEALTH_ACTIONS:
                step = StepRecord.new(
                    task.id,
                    f"system_observe.v1/{action}",
                    target_agent_id=agent["id"],
                    required_capabilities=["system_observe.v1"],
                    inputs={
                        "profile": "system_observe.v1",
                        "action": action,
                        "mcp_tool": mcp_tool,
                        "params": {},
                    },
                    expected_result={"read_only": True},
                )
                self.db.create_task_step(
                    step_id=step.id,
                    task_id=step.task_id,
                    action_type=step.action_type,
                    idempotency_key=step.idempotency_key,
                    target_agent_id=step.target_agent_id,
                    required_capabilities=step.required_capabilities,
                    inputs=step.inputs,
                    expected_result=step.expected_result,
                    retry_policy=step.retry_policy,
                )
        self.db.update_task(user_id, task.id, status=TaskStatus.QUEUED.value)
        self.db.append_task_event(task.id, "task.queued", {"device_count": len(selected_ids)})
        return self.get_task(user_id, task.id)

    def enqueue(self, user_id: str, task_id: str) -> asyncio.Task[None]:
        if task_id not in self._running:
            self._running[task_id] = asyncio.create_task(self._run(user_id, task_id))
        return self._running[task_id]

    async def cancel(self, user_id: str, task_id: str) -> bool:
        task = self.db.get_task(user_id, task_id)
        if task is None:
            return False
        if task["status"] in {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.PARTIALLY_COMPLETED.value,
            TaskStatus.ROLLED_BACK.value,
        }:
            return False
        self.db.update_task(user_id, task_id, status=TaskStatus.CANCELLED.value)
        for step in self.db.list_task_steps(user_id, task_id):
            if step["status"] in {"created", "running"}:
                self.db.update_task_step(step["id"], status="cancelled", completed_at=int(time.time()))
        self.db.append_task_event(task_id, "task.cancelled", {"source": "user"})
        for agent_id in self._active_agents.get(task_id, set()):
            await self.hub.cancel_task(agent_id, task_id, "用户取消 Task")
        return True

    async def cancel_step(self, user_id: str, task_id: str, step_id: str) -> bool:
        task = self.db.get_task(user_id, task_id)
        if not task or task["status"] != "running":
            return False
        step = self.db.get_task_step(step_id)
        if not step or step["task_id"] != task_id or step["status"] not in {"created", "running"}:
            return False
        agent_id = step["target_agent_id"]
        if not agent_id or not self.hub.can_cancel_health_step(agent_id, task_id, step_id):
            return False
        completed_at = int(time.time())
        self.db.update_task_step(step_id, status="cancelled", completed_at=completed_at)
        inputs = step["inputs"]
        action = self.db.get_task_action_by_idempotency(f"{task_id}:{step_id}:{inputs['profile']}:{inputs['action']}")
        if action:
            self.db.update_task_action(action["id"], status="cancelled", completed_at=completed_at)
        self.db.append_task_event(task_id, "step.cancelled", {"source": "user"}, step_id=step_id, agent_id=agent_id)
        await self.hub.cancel_health_step(agent_id, task_id, step_id)
        return True

    def get_task(self, user_id: str, task_id: str) -> dict[str, Any]:
        task = self.db.get_task(user_id, task_id)
        if task is None:
            raise HealthCheckError("Task 不存在")
        task["steps"] = self.db.list_task_steps(user_id, task_id)
        task["events"] = self.db.list_task_events(user_id, task_id, limit=1)
        return task

    async def shutdown(self) -> None:
        for task_id in list(self._running):
            task = self._running[task_id]
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()
        self._active_agents.clear()

    def _select_agents(
        self,
        user_id: str,
        agent_ids: list[str] | None,
        requested_runtime: str | None,
        target_selector: DeviceFilters | None,
    ) -> list[dict[str, Any]]:
        available = {
            agent["id"]: agent
            for agent in self.db.list_agents(user_id)
            if "system_observe.v1" in agent["profiles"]
            and self.hub.supports_task_runtime(agent["id"], requested_runtime)
            and (target_selector is None or target_selector.matches(agent))
        }
        selected = list(dict.fromkeys(agent_ids or available))
        if len(selected) > 64:
            raise HealthCheckError("单次健康检查最多选择 64 台设备")
        if any(agent_id not in available for agent_id in selected):
            raise HealthCheckError("指定设备不属于当前用户、当前离线或未启用所选 Runtime")
        return [available[agent_id] for agent_id in selected]

    async def _run(self, user_id: str, task_id: str) -> None:
        task = self.db.get_task(user_id, task_id)
        if task is None:
            return
        if task["status"] == TaskStatus.CANCELLED.value:
            self._running.pop(task_id, None)
            return
        self.db.update_task(user_id, task_id, status=TaskStatus.RUNNING.value)
        self.db.append_task_event(task_id, "task.started", {})
        steps = self.db.list_task_steps(user_id, task_id)
        agents = sorted({step["target_agent_id"] for step in steps if step["target_agent_id"]})
        self._active_agents[task_id] = set(agents)
        try:
            results = await asyncio.gather(
                *(
                    self._run_device(user_id, task_id, task, agent_id, steps)
                    for agent_id in agents
                ),
                return_exceptions=True,
            )
            devices: list[dict[str, Any]] = []
            for agent_id, result in zip(agents, results, strict=True):
                if isinstance(result, Exception):
                    devices.append(
                        {
                            "agent_id": agent_id,
                            "status": "failed",
                            "observations": {},
                            "alerts": [],
                            "error": str(result),
                        }
                    )
            devices.extend(result for result in results if isinstance(result, dict))
            summary = {
                "healthy": sum(device["status"] == "healthy" for device in devices),
                "warning": sum(device["status"] == "warning" for device in devices),
                "critical": sum(device["status"] == "critical" for device in devices),
                "failed": sum(device["status"] == "failed" for device in devices),
            }
            final_status = TaskStatus.COMPLETED.value
            if summary["failed"] and any(
                device["status"] != "failed" or (device.get("incomplete") and device["observations"])
                for device in devices
            ):
                final_status = TaskStatus.PARTIALLY_COMPLETED.value
            elif summary["failed"]:
                final_status = TaskStatus.FAILED.value
            if self.db.get_task(user_id, task_id)["status"] == TaskStatus.CANCELLED.value:
                return
            result = {"task_id": task_id, "devices": devices, "summary": summary}
            self.db.update_task(user_id, task_id, status=final_status, result=result)
            self.db.append_task_event(
                task_id,
                "task.completed" if final_status == TaskStatus.COMPLETED.value else "task.finished",
                {"status": final_status, "summary": summary},
            )
        except asyncio.CancelledError:
            raise
        finally:
            self._active_agents.pop(task_id, None)
            self._running.pop(task_id, None)

    async def _run_device(
        self,
        user_id: str,
        task_id: str,
        task: dict[str, Any],
        agent_id: str,
        all_steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        device_steps = [step for step in all_steps if step["target_agent_id"] == agent_id]
        steps_by_id = {step["id"]: step for step in device_steps}
        requested_runtime = task["requested_runtime"]
        monitor = task["resource_budget"].get("source") == "health_monitor"
        action_context: dict[str, dict[str, Any]] = {}
        approved_steps: set[str] = set()
        runtime_info: dict[str, str] | None = None
        runtime_session_id: str | None = None
        runtime_status: str | None = None
        unmatched_action_reported = False
        self.db.append_task_event(
            task_id,
            "device.started",
            {"agent_id": agent_id},
            agent_id=agent_id,
        )

        def require_active_task() -> None:
            current = self.db.get_task(user_id, task_id)
            if current is None or current["status"] != TaskStatus.RUNNING.value:
                raise HealthCheckError("Task 已取消或不再运行")

        def cancelled_steps() -> set[str]:
            return {
                step["id"] for step in self.db.list_task_steps(user_id, task_id)
                if step["id"] in steps_by_id and step["status"] == "cancelled"
            }

        async def on_message(message: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal runtime_info, runtime_session_id, runtime_status, unmatched_action_reported
            message_type = message.get("type")
            if message_type == "task_action_request":
                step_id = message["step_id"]
                step = steps_by_id.get(step_id)
                if step and self.db.get_task_step(step_id)["status"] == "cancelled":
                    return {"decision": "deny", "reason": "用户取消观察步骤", "policy_context": {"step_cancelled": True}}
                inputs = step["inputs"] if step else {}
                if (
                    self.db.get_task(user_id, task_id)["status"] == "cancelled"
                    or (requested_runtime and (runtime_info is None or runtime_status is not None))
                    or step is None
                    or step_id in approved_steps
                    or message["profile"] != inputs["profile"]
                    or message["action"] != inputs["action"]
                    or message["params"] != inputs["params"]
                    or message.get("risk_level", "L0") != "L0"
                ):
                    reason = "Action 与当前 L0 计划不匹配、已授权或任务已结束"
                    self.db.append_task_event(task_id, "action.blocked", {"reason": reason}, agent_id=agent_id)
                    if not unmatched_action_reported and self.db.get_task(user_id, task_id)["status"] == "running":
                        self.db.create_task_alert(**_alert(
                            task_id, agent_id, "warning", "health_action_denied",
                            "设备请求了计划外或重复的健康观察", "检查 Runtime 与 Agent 的任务事件",
                            action=f"{inputs['profile']}/{inputs['action']}" if inputs else None,
                            basis={"reason": "action_not_allowed"}, blocked=True,
                        ), step_id=step_id if step else None)
                        unmatched_action_reported = True
                    return {"decision": "deny", "reason": reason}
                profile, action, params = message["profile"], message["action"], message["params"]
                policy = self.policy.evaluate(
                    profile=profile,
                    action=action,
                    params=params,
                    risk_level=message.get("risk_level", "L0"),
                    action_interception="limited" if requested_runtime else "full",
                )
                action_request_id = message["action_request_id"]
                try:
                    self.db.create_task_action(
                        action_id=action_request_id,
                        task_id=task_id,
                        step_id=step["id"],
                        agent_id=agent_id,
                        profile=profile,
                        action=action,
                        params=params,
                        idempotency_key=f"{task_id}:{step['id']}:{profile}:{action}",
                        risk_level=policy.risk_level,
                        policy_context=policy.as_dict(),
                        approval_requirement=policy.decision.value,
                    )
                except ConflictError:
                    return {"decision": "deny", "reason": "Action 请求标识已使用"}
                if policy.decision.value == "allow":
                    approved_steps.add(step_id)
                    action_context[action_request_id] = {
                        "step_id": step_id,
                        "profile": profile,
                        "action": action,
                    }
                self.db.update_task_step(
                    step["id"],
                    status=TaskStatus.RUNNING.value,
                    started_at=int(time.time()),
                )
                self.db.update_task_action(
                    action_request_id,
                    status=policy.decision.value,
                    started_at=int(time.time()),
                )
                self.db.append_task_event(
                    task_id,
                    "action.requested",
                    {
                        "action_request_id": action_request_id,
                        "profile": profile,
                        "action": action,
                        "policy": policy.as_dict(),
                    },
                    step_id=step["id"],
                    agent_id=agent_id,
                )
                return {
                    "decision": "allow" if policy.decision.value == "allow" else "deny",
                    "reason": policy.reason,
                    "policy_context": policy.as_dict(),
                }
            if message_type == "task_event":
                event_type = message["event_type"]
                if monitor and event_type not in {"step_started", "action_started", "action_completed", "action_blocked"}:
                    return None
                payload = {} if monitor else _redact(message["payload"])
                step_id = message.get("step_id")
                if event_type == "runtime_started":
                    if not requested_runtime or runtime_info is not None:
                        raise ValueError("收到未请求或重复的 Runtime 会话")
                    runtime_info = _runtime_info(payload, requested_runtime)
                    runtime_session_id = f"runtime_{uuid.uuid4().hex}"
                    self.db.create_runtime_session(
                        session_id=runtime_session_id,
                        agent_id=agent_id,
                        provider_id=requested_runtime,
                        runtime_version=runtime_info["runtime_version"],
                        adapter_version=runtime_info["adapter_version"],
                        mode=runtime_info["mode"],
                        status="running",
                        metadata={"task_id": task_id},
                    )
                    payload = {**runtime_info, "session_id": runtime_session_id}
                elif event_type == "runtime_finished":
                    status = payload.get("status")
                    if (
                        not runtime_info
                        or runtime_status is not None
                        or payload.get("session_id") != runtime_info["session_id"]
                        or status not in ("completed", "failed", "cancelled")
                    ):
                        raise ValueError("Runtime 会话与任务不匹配")
                    runtime_status = status
                    payload = {"session_id": runtime_session_id, "status": runtime_status}
                self.db.append_task_event(
                    task_id,
                    f"agent.{event_type}",
                    {
                        "remote_sequence": message["sequence"],
                        "payload": payload,
                        **({} if monitor else {"remote_event_id": message["event_id"]}),
                    },
                    event_id=None if monitor else message["event_id"],
                    step_id=step_id if isinstance(step_id, str) and step_id in steps_by_id else None,
                    agent_id=agent_id,
                )
            elif message_type == "task_ack":
                self.db.append_task_event(
                    task_id,
                    "agent.acknowledged",
                    {"status": message.get("status", "accepted")},
                    agent_id=agent_id,
                )
            elif message_type == "task_result":
                self.db.append_task_event(
                    task_id,
                    "agent.result",
                    {"ok": message["ok"]},
                    agent_id=agent_id,
                )
            elif message_type == "task_error":
                self.db.append_task_event(
                    task_id,
                    "agent.error",
                    {"error": "巡检设备返回失败" if monitor else message.get("error", "unknown")},
                    agent_id=agent_id,
                )
            return None

        session_status = "failed"
        try:
            result = await self.hub.execute_task(
                agent_id,
                task_id,
                task["objective"],
                [
                    {
                        "step_id": step["id"],
                        "profile": step["inputs"]["profile"],
                        "action": step["inputs"]["action"],
                        "params": step["inputs"].get("params", {}),
                        "risk_level": "L0",
                    }
                    for step in device_steps
                ],
                timeout=HEALTH_TIMEOUT_SECONDS,
                risk_policy=task["risk_policy"],
                deadline=task["deadline"],
                requested_runtime=requested_runtime,
                on_message=on_message,
                authorize_dispatch=require_active_task,
                execution_revision=task["resource_budget"].get("execution_revision", 0),
                execution_operation="health_monitor" if monitor else "",
            )
            if self.db.get_task(user_id, task_id)["status"] == "cancelled":
                session_status = "cancelled"
                raise ValueError("Task 已取消")
            if requested_runtime and (runtime_status != "completed" or result.get("runtime") != runtime_info):
                raise ValueError("未收到指定 Runtime 的完整执行证据")
            public_runtime = {**runtime_info, "session_id": runtime_session_id} if runtime_info else None
            observations: dict[str, Any] = {}
            items = result.get("steps")
            cancelled = cancelled_steps()
            if not isinstance(items, list) or len(items) > len(device_steps):
                raise ValueError("Agent Task 缺少完整的观察结果")
            verified: list[tuple[str, dict[str, Any]]] = []
            seen_steps: set[str] = set()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Agent Task steps 结果无效")
                request_id = item.get("action_request_id")
                context = action_context.get(request_id) if isinstance(request_id, str) else None
                if (
                    context is None
                    or any(item.get(field) != expected for field, expected in context.items())
                    or context["step_id"] in seen_steps
                ):
                    raise ValueError("Agent Task 返回未知 Action")
                seen_steps.add(context["step_id"])
                if context["step_id"] in cancelled:
                    continue
                normalized = _redact(validate_profile_result(
                    context["profile"],
                    context["action"],
                    item.get("result", {}),
                ))
                observations[context["action"]] = normalized
                verified.append((request_id, context))
            if set(steps_by_id) - cancelled - seen_steps:
                raise ValueError("Agent Task 缺少完整的观察结果")
            status, alerts = ("failed", []) if cancelled else self._assess(observations, agent_id, task_id)
            if monitor:
                observations = {action: monitor_observation(action, record) for action, record in observations.items()}
            for request_id, context in verified:
                public_result = observations[context["action"]]
                self.db.update_task_step(
                    context["step_id"],
                    status=TaskStatus.COMPLETED.value,
                    result=public_result,
                    completed_at=int(time.time()),
                )
                self.db.update_task_action(
                    request_id,
                    status=TaskStatus.COMPLETED.value,
                    result=public_result,
                    completed_at=int(time.time()),
                )
            step_by_action = {
                f"{step['inputs']['profile']}/{step['inputs']['action']}": step["id"] for step in device_steps
            }
            for alert in alerts:
                self.db.create_task_alert(**alert, step_id=step_by_action.get(alert["action"]))
            evidence_summary = {
                "agent_id": agent_id,
                "status": status,
                "observations": observations,
                "alerts": alerts,
                "runtime": public_runtime,
                **({"incomplete": True, "error": "观察步骤已取消，未生成完整健康结论"} if cancelled else {}),
            }
            content_hash = hashlib.sha256(
                json.dumps(evidence_summary, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if observations:
                self.db.create_task_evidence(
                    evidence_id=f"evidence_{uuid.uuid4().hex}",
                    task_id=task_id,
                    kind="health_check_partial" if cancelled else "health_monitor" if monitor else "health_check",
                    summary=evidence_summary,
                    content_hash=content_hash,
                    agent_id=agent_id,
                )
            self.db.append_task_event(
                task_id,
                "device.incomplete" if cancelled else "device.completed",
                {"agent_id": agent_id, "status": status, "alert_count": len(alerts)},
                agent_id=agent_id,
            )
            session_status = "completed"
            return evidence_summary
        except asyncio.CancelledError:
            session_status = "cancelled"
            raise
        except (AgentOfflineError, AgentCommandError, ValueError) as exc:
            if self.db.get_task(user_id, task_id)["status"] == "cancelled":
                session_status = "cancelled"
                raise
            error = "巡检未获得完整验证结果" if monitor and not isinstance(exc, AgentExecutionStopped) else str(exc)
            cancelled = cancelled_steps()
            for step in device_steps:
                if step["id"] in cancelled:
                    continue
                self.db.update_task_step(
                    step["id"],
                    status=TaskStatus.FAILED.value,
                    error=error,
                    completed_at=int(time.time()),
                )
            self.db.append_task_event(
                task_id,
                "device.failed",
                {"agent_id": agent_id, "error": error},
                agent_id=agent_id,
            )
            if isinstance(exc, AgentExecutionStopped):
                session_status = "cancelled"
                self.db.create_task_alert(**_alert(
                    task_id, agent_id, "warning", "execution_stop", STOP_EXPLANATION,
                    "检查设备状态和停机范围；不会自动重试", basis={"reason": EXECUTION_STOPPED}, blocked=True,
                ))
            if monitor:
                raise HealthCheckError(error) from None
            raise
        finally:
            if session_status != "completed":
                cancelled = cancelled_steps()
                for request_id, context in action_context.items():
                    if context["step_id"] in cancelled:
                        continue
                    self.db.update_task_action(
                        request_id, status=session_status, completed_at=int(time.time()),
                    )
            if runtime_session_id:
                self.db.update_runtime_session(
                    runtime_session_id, status=session_status, ended_at=int(time.time()),
                )

    def _assess(
        self,
        observations: dict[str, Any],
        agent_id: str,
        task_id: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        alerts: list[dict[str, Any]] = []
        resources = observations.get("get_resources", {})
        memory = resources.get("memory") if isinstance(resources, dict) else None
        utilization = memory.get("utilization_percent") if isinstance(memory, dict) else None
        resources_action = "system_observe.v1/get_resources"
        if not isinstance(utilization, (int, float)) or isinstance(utilization, bool) or not 0 <= utilization <= 100:
            alerts.append(_alert(
                task_id, agent_id, "warning", "memory_observation",
                "内存利用率不可用", "使用设备原生监控补充检查",
                action=resources_action, basis={"signal": "memory_unavailable"},
            ))
        elif utilization >= 90:
            alerts.append(
                _alert(
                    task_id,
                    agent_id,
                    "warning",
                    "memory",
                    "内存使用率达到或超过 90%",
                    "检查高占用进程，必要时安排人工诊断",
                    action=resources_action, basis={"observed_percent": round(utilization, 1), "threshold_percent": 90},
                )
            )
        if isinstance(resources, dict):
            cpu = resources.get("cpu", {})
            load = cpu.get("load_average")
            count = cpu.get("logical_count")
            if (
                isinstance(load, list) and load and isinstance(load[0], (int, float))
                and not isinstance(load[0], bool) and 0 <= load[0] <= 1_000_000
                and isinstance(count, int) and count > 0 and load[0] * 2 >= count * 3
            ):
                alerts.append(_alert(
                    task_id, agent_id, "warning", "cpu_load", "一分钟负载达到逻辑 CPU 数的 1.5 倍",
                    "复测负载；单次快照不代表持续 CPU 饱和",
                    action=resources_action, basis={"load_per_core": round(load[0] / count, 2), "threshold": 1.5},
                ))
            for disk in resources.get("disks", []):
                total = disk.get("total_bytes", 0)
                free = disk.get("free_bytes", 0)
                if total and free * 10 < total:
                    alerts.append(
                        _alert(
                            task_id,
                            agent_id,
                            "warning",
                            "disk",
                            "磁盘可用空间低于 10%",
                            "清理空间前先进行人工确认，不自动删除文件",
                            action=resources_action, basis={"available_percent": round(100 * (free / total), 1),
                                                            "threshold_percent": 10},
                        )
                    )
        processes = observations.get("get_processes", {})
        if isinstance(processes, dict) and not processes.get("available", False):
            alerts.append(
                _alert(
                    task_id,
                    agent_id,
                    "warning",
                    "process_observation",
                    "进程观察不可用",
                    "检查 Agent 运行权限和平台进程查询能力",
                    action="system_observe.v1/get_processes", basis={"signal": "processes_unavailable"},
                )
            )
        network = observations.get("get_network", {})
        if isinstance(network, dict) and not network.get("interfaces"):
            alerts.append(
                _alert(
                    task_id,
                    agent_id,
                    "warning",
                    "network",
                    "未发现本机网络接口",
                    "确认设备网络状态和 Agent 权限",
                    action="system_observe.v1/get_network", basis={"interface_count": 0},
                )
            )
        return ("warning" if alerts else "healthy"), alerts


def _alert(
    task_id: str,
    agent_id: str,
    severity: str,
    category: str,
    trigger: str,
    recommendation: str,
    *,
    action: str | None = None,
    basis: dict[str, Any],
    blocked: bool = False,
) -> dict[str, Any]:
    return {
        "alert_id": f"alert_{uuid.uuid4().hex}",
        "task_id": task_id,
        "agent_id": agent_id,
        "severity": severity,
        "category": category,
        "trigger": trigger,
        "recommendation": recommendation,
        "action": action,
        "basis": basis,
        "blocked": blocked,
    }


def _runtime_info(payload: dict[str, Any], requested_runtime: str) -> dict[str, str]:
    info = {field: payload.get(field) for field in _RUNTIME_FIELDS}
    contract = HEALTH_RUNTIME_CATALOG.get(requested_runtime)
    if (
        contract is None or not contract.matches(info)
        or not isinstance(info["session_id"], str)
        or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", info["session_id"])
    ):
        raise ValueError("Runtime 会话元数据无效")
    return info


def _redact(value: Any) -> Any:
    sensitive_keys = {
        "password",
        "passwd",
        "token",
        "secret",
        "authorization",
        "api_key",
        "private_key",
        "credential",
    }
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if key.lower() in sensitive_keys else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
