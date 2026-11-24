from __future__ import annotations

import asyncio
import difflib
import hashlib
import re
import time
import uuid
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from remote_agent.profiles.common.managed_contracts import LOCAL_PLAN_PROFILES, SERVICE_PROFILE, project_managed_result
from remote_agent.profiles.common.planned_action import PlannedAction, PlannedOperation, digest_json, parse_planned_action

from .agent_hub import AgentCommandError, AgentHub, AgentOfflineError
from .database import ConflictError, Database
from .execution_controls import EXECUTION_STOPPED, STOP_EXPLANATION, operation_group
from .policy_service import PolicyService
from .task_models import RiskDecision


StepKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
PlanHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_ACTIVE_TASKS = {"queued", "running"}
_CANCELLABLE_TASKS = _ACTIVE_TASKS | {"approved", "waiting_approval"}
_DEFAULT_PLAN_POLICY = {"revision": 0, "l1_paths": []}
_MAX_MAINTENANCE_SECONDS = 24 * 60 * 60
_MAX_MAINTENANCE_LEAD_SECONDS = 7 * _MAX_MAINTENANCE_SECONDS
_STEP_ERRORS = {
    "workspace.conflict": "文件版本已变化或目标已存在，须重新读取并审批",
    "workspace.not_found": "文件或父目录不存在",
    "workspace.invalid_path": "设备未允许该工作区目标",
    "service.unavailable": "无法读取登记应用；确认本机服务管理接口或凭据",
    "service.invalid_response": "登记应用返回了不合法的结构化状态",
    "service.generation_changed": "服务代际已变；请重新观察并审批",
    "service.not_recoverable": "服务已就绪或正在启动；不会对该状态执行故障恢复",
    "service.busy": "同一登记服务已有恢复动作",
    "service.recovery_unconfirmed": "无法确认恢复；重新检查服务代际和就绪状态，不会重试",
    "service.not_ready": "登记应用尚未就绪；请查看最近的结构化诊断记录",
    "build.source_mismatch": "设备源码包摘要不匹配，未执行构建或测试",
    "build.invalid_archive": "设备源码包不满足路径或大小限制",
    "build.runner_failed": "设备无法运行固定 Python 验证",
    "build.verification_failed": "编译或测试未通过；请根据记录检查同一源码包后提交新计划",
    "device_changed": "所有权、连接、设备能力或设备策略已变化，须重新提交计划",
    "key_revoked": "启动计划的 MCP Key 已撤销",
    "policy_denied": "当前动作未获授权",
    EXECUTION_STOPPED: STOP_EXPLANATION,
    "expired": "计划的执行授权已过期",
    "outside_maintenance_window": "当前不在批准的维护窗口内，不再派发",
    "unconfirmed": "设备未返回已验证结果，不自动重试",
}


class OperationStepDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: StepKey
    agent_id: str = Field(min_length=1, max_length=80)
    depends_on: list[StepKey] = Field(default_factory=list, max_length=11)
    operation: PlannedOperation
    params: dict[str, Annotated[str, Field(max_length=8192)]] = Field(max_length=4)


class MaintenanceWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_at: int = Field(gt=0, strict=True)
    ends_at: int = Field(gt=0, strict=True)
    max_concurrency: int = Field(default=1, ge=1, le=5, strict=True)


class OperationPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=240)
    timeout_seconds: int = Field(default=120, ge=10, le=300, strict=True)
    steps: list[OperationStepDraft] = Field(min_length=1, max_length=12)
    maintenance: MaintenanceWindow | None = None


class OperationPlanRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_hash: PlanHash
    plan: OperationPlanDraft


class BoundPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_hash: PlanHash


class ApprovalRequest(BoundPlanRequest):
    decision: Literal["approve", "deny"]


class PlanNotFound(LookupError):
    pass


class PlanStepError(ValueError):
    pass


def _maintenance_batches(steps: list[dict[str, Any]], max_concurrency: int) -> list[list[str]]:
    pending = {step["key"]: step for step in steps}
    finished: set[str] = set()
    batches: list[list[str]] = []
    while pending:
        devices: set[str] = set()
        batch: list[str] = []
        for key, step in pending.items():
            if step["agent_id"] not in devices and finished.issuperset(step["depends_on"]):
                batch.append(key)
                devices.add(step["agent_id"])
                if len(batch) == max_concurrency:
                    break
        assert batch
        for key in batch:
            del pending[key]
        finished.update(batch)
        batches.append(batch)
    return batches


class OperationPlanService:
    def __init__(self, db: Database, hub: AgentHub):
        self.db = db
        self.hub = hub
        self.policy = PolicyService()
        self.running: dict[str, asyncio.Task[None]] = {}
        self.in_flight: dict[str, dict[str, asyncio.Task[None]]] = {}

    def _snapshot(self, user_id: str, agent_id: str) -> dict[str, Any] | None:
        connection_id = self.hub.planned_connection(user_id, agent_id)
        agent = self.db.get_owned_agent(user_id, agent_id) if connection_id else None
        if agent is None:
            return None
        plan_policy = agent["plan_policy"]
        if agent["features"].get("planned_l1_policy") is not True:
            plan_policy = {**plan_policy, "l1_paths": []}
        return {
            "name": agent["name"], "connection_id": connection_id, "version": agent["version"],
            "profiles": {declaration["id"]: declaration["runtime"] for declaration in agent["profile_declarations"]},
            "plan_policy": plan_policy,
        }

    def _load(
        self, user_id: str, task_id: str, expected_hash: str | None, *, key_id: str | None = None,
    ) -> dict[str, Any]:
        plan = self.db.get_task_plan(user_id, task_id)
        if plan is None or (key_id is not None and plan.get("submitted_by_key_id") != key_id):
            raise PlanNotFound
        digest = digest_json({key: value for key, value in plan.items() if key != "hash"})
        if (expected_hash is not None and expected_hash != plan["hash"]) or plan["hash"] != digest:
            raise ConflictError("计划版本已变化，请刷新")
        return plan

    def save(
        self, user_id: str, draft: OperationPlanDraft,
        *, task_id: str | None = None, previous_hash: str | None = None, key_id: str | None = None,
    ) -> dict[str, Any]:
        objective = draft.objective.strip()
        if not objective:
            raise ValueError("计划目标不能为空")
        now = int(time.time())
        execution_revision = self.hub.execution_revision()
        maintenance = draft.maintenance
        if maintenance is not None and not (
            now < maintenance.ends_at <= now + _MAX_MAINTENANCE_LEAD_SECONDS
            and 0 < maintenance.ends_at - maintenance.starts_at <= _MAX_MAINTENANCE_SECONDS
        ):
            raise ValueError("维护窗口必须结束于未来七天内，且时长大于零、不超过二十四小时")
        steps_by_key = {step.key: step for step in draft.steps}
        if len(steps_by_key) != len(draft.steps) or any(
            len(set(step.depends_on)) != len(step.depends_on)
            or not set(step.depends_on) <= steps_by_key.keys()
            for step in draft.steps
        ):
            raise ValueError("步骤标识或依赖无效")
        try:
            order = tuple(TopologicalSorter({step.key: step.depends_on for step in draft.steps}).static_order())
        except CycleError as exc:
            raise ValueError("计划的步骤依赖不能有环") from exc
        if task_id is None:
            task_id = f"task_{uuid.uuid4().hex}"
            revision = 1
        else:
            assert previous_hash is not None
            revision = self._load(user_id, task_id, previous_hash)["revision"] + 1
        targets: dict[str, dict[str, Any]] = {}
        steps = []
        for key in order:
            source = steps_by_key[key]
            action = parse_planned_action(source.operation, source.params)
            if source.agent_id not in targets:
                target = self._snapshot(user_id, source.agent_id)
                if target is None:
                    raise ValueError("请选择本人拥有、在线且支持受控计划的设备")
                targets[source.agent_id] = target
            if action.profile not in targets[source.agent_id]["profiles"]:
                raise ValueError("选定设备没有加载此动作所需的 Profile")
            if (
                action.profile == SERVICE_PROFILE and action.params["service_id"]
                not in targets[source.agent_id]["profiles"][SERVICE_PROFILE]["service_ids"]
            ):
                raise ValueError("选定设备未登记此服务 ID")
            decision = self.policy.evaluate_plan(action, l1_paths=targets[source.agent_id]["plan_policy"]["l1_paths"])
            if decision.decision == RiskDecision.DENY:
                raise ValueError(decision.reason)
            steps.append({
                **source.model_dump(),
                "id": f"{task_id}:r{revision}:{key}", "profile": action.profile, "action": action.action,
                "scope": action.scope, "risk_level": action.risk_level,
                "approval_required": decision.decision == RiskDecision.APPROVAL_REQUIRED,
            })
        max_level = max(step["risk_level"] for step in steps)
        uses_local_resources = any(step["profile"] in LOCAL_PLAN_PROFILES for step in steps)
        runs_source_code = any(step["operation"] == "build.verify" for step in steps)
        plan = {
            "task_id": task_id, "user_id": user_id, "revision": revision, "objective": objective,
            "execution_revision": execution_revision,
            "targets": targets, "steps": steps, "max_level": max_level,
            "requires_approval": maintenance is not None or any(step["approval_required"] for step in steps),
            "valid_until": maintenance.ends_at if maintenance else now + 1800, "timeout_seconds": draft.timeout_seconds,
            "runtime": "native", "workdir_scope": "local_registration" if uses_local_resources else "agent_workdir",
            "external_network": None if runs_source_code else False,
            "failure_policy": "stop_remaining_batches" if maintenance else "skip_dependents_no_retry", "rollback": "none",
        }
        if maintenance is not None:
            plan["maintenance"] = {
                **maintenance.model_dump(), "batches": _maintenance_batches(steps, maintenance.max_concurrency),
            }
        if key_id is not None:
            plan["submitted_by_key_id"] = key_id
        self.db.save_task_plan(user_id, task_id, digest_json(plan), plan, previous_hash=previous_hash)
        return self.detail(user_id, task_id)

    def detail(self, user_id: str, task_id: str) -> dict[str, Any]:
        task = self.db.get_task(user_id, task_id)
        if task is None:
            raise PlanNotFound
        plan = self.db.get_task_plan(user_id, task_id)
        task["steps"] = self.db.list_task_steps(user_id, task_id)
        if plan is not None:
            task["approval"] = self.db.get_task_approval(user_id, task_id, plan_hash=plan["hash"])
            task["plan"] = {
                **{key: value for key, value in plan.items() if key not in {"targets", "user_id", "submitted_by_key_id"}},
                "submitted_by_mcp": "submitted_by_key_id" in plan,
                "targets": {
                    agent_id: {key: value for key, value in target.items() if key != "connection_id"}
                    for agent_id, target in plan["targets"].items()
                },
            }
        return task

    def delegated_status(self, user_id: str, task_id: str, key_id: str) -> dict[str, Any]:
        self._load(user_id, task_id, None, key_id=key_id)
        task = self.detail(user_id, task_id)
        plan = task["plan"]
        by_id = {step["id"]: step for step in task["steps"]}
        return {
            "task_id": task_id, "status": task["status"], "plan_hash": plan["hash"],
            "requires_approval": plan["requires_approval"],
            **({"maintenance": plan["maintenance"]} if "maintenance" in plan else {}),
            "steps": [
                {
                    "key": step["key"], "agent_id": step["agent_id"], "operation": step["operation"],
                    "risk_level": step["risk_level"], "status": by_id[step["id"]]["status"],
                    "approval_required": step["approval_required"],
                    "error": by_id[step["id"]]["error"],
                    "result": {
                        key: value for key, value in (by_id[step["id"]]["result"] or {}).items() if key != "diff"
                    },
                }
                for step in plan["steps"]
            ],
        }

    def _check_target(self, user_id: str, agent_id: str, plan: dict[str, Any]) -> bool:
        snapshot = self._snapshot(user_id, agent_id)
        expected = plan["targets"][agent_id]
        return snapshot is not None and all(
            snapshot[field] == expected[field] for field in ("connection_id", "version", "profiles")
        ) and snapshot["plan_policy"] == expected.get("plan_policy", _DEFAULT_PLAN_POLICY)

    def _step_execution_blocked(self, user_id: str, plan: dict[str, Any], step: dict[str, Any]) -> bool:
        return self.hub.execution_blocked(
            user_id, step["agent_id"],
            operation=operation_group(step["profile"], step["action"], planned=True),
            revision=plan.get("execution_revision", 0),
        )

    def _require_execution_enabled(self, user_id: str, plan: dict[str, Any]) -> None:
        blocked = next((step for step in plan["steps"] if self._step_execution_blocked(user_id, plan, step)), None)
        if blocked is not None:
            self._blocked(plan["task_id"], EXECUTION_STOPPED, blocked)
            raise ConflictError(STOP_EXPLANATION)

    def decide(self, user_id: str, task_id: str, plan_hash: str, *, approve: bool) -> dict[str, Any]:
        plan = self._load(user_id, task_id, plan_hash)
        if approve:
            self._require_execution_enabled(user_id, plan)
        if approve and not all(self._check_target(user_id, agent_id, plan) for agent_id in plan["targets"]):
            self._blocked(task_id, "device_changed")
            raise ConflictError(_STEP_ERRORS["device_changed"])
        self.db.decide_task_plan(user_id, task_id, plan_hash, approve=approve)
        return self.detail(user_id, task_id)

    def start(self, user_id: str, task_id: str, plan_hash: str, *, key_id: str | None = None) -> dict[str, Any]:
        plan = self._load(user_id, task_id, plan_hash, key_id=key_id)
        if key_id is not None and plan["requires_approval"]:
            raise ConflictError("此计划需要人工审批和执行，请使用管理端")
        if key_id is not None and not self.db.is_active_plan_key(user_id, key_id):
            raise ConflictError(_STEP_ERRORS["key_revoked"])
        self._require_execution_enabled(user_id, plan)
        if not all(self._check_target(user_id, agent_id, plan) for agent_id in plan["targets"]):
            self._blocked(task_id, "device_changed")
            raise ConflictError(_STEP_ERRORS["device_changed"])
        approval_id, deadline = self.db.claim_task_plan(user_id, task_id, plan_hash)
        self.running[task_id] = asyncio.create_task(self._run(user_id, plan, approval_id, deadline, key_id))
        return self.detail(user_id, task_id)

    def _blocked(self, task_id: str, error: str, step: dict[str, Any] | None = None) -> None:
        agent_id = step["agent_id"] if step else None
        self.db.append_task_event(task_id, "action.blocked", {"error": error}, agent_id=agent_id,
                                  step_id=step["id"] if step else None)
        self._warn(task_id, error, step, blocked=True)

    def _warn(self, task_id: str, error: str, step: dict[str, Any] | None, *, blocked: bool = False) -> None:
        self.db.create_task_alert(
            alert_id=uuid.uuid4().hex, task_id=task_id, severity="warning",
            agent_id=step["agent_id"] if step else None, step_id=step["id"] if step else None,
            action=step["operation"] if step else None, basis={"reason": error}, blocked=blocked,
            category="operation_plan", trigger=_STEP_ERRORS.get(error, _STEP_ERRORS["unconfirmed"]),
            recommendation="核对最新状态与证据，按需提交新计划；不会自动重试",
        )

    async def cancel(self, user_id: str, task_id: str) -> bool:
        task = self.db.get_task(user_id, task_id)
        if task is None or task["status"] not in _CANCELLABLE_TASKS:
            return False
        self.db.update_task(user_id, task_id, status="cancelled")
        for step in self.db.list_task_steps(user_id, task_id):
            if step["status"] in {"created", "running"}:
                self.db.update_task_step(step["id"], status="cancelled", completed_at=int(time.time()))
        self.db.append_task_event(task_id, "task.cancelled", {"source": "user", "rollback": False})
        worker = self.running.get(task_id)
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self.running.pop(task_id, None)
        return True

    async def cancel_step(self, user_id: str, task_id: str, step_id: str) -> bool:
        task = self.db.get_task(user_id, task_id)
        if task is None or task["status"] not in _ACTIVE_TASKS:
            return False
        steps = self.db.list_task_steps(user_id, task_id)
        if not any(step["id"] == step_id and step["status"] in {"created", "running"} for step in steps):
            return False
        self.db.update_task_step(step_id, status="cancelled", completed_at=int(time.time()))
        self.db.append_task_event(task_id, "step.cancelled", {"source": "user", "rollback": False}, step_id=step_id)
        active = self.in_flight.get(task_id, {}).get(step_id)
        if active:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        return True

    async def shutdown(self) -> None:
        workers = list(self.running.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.running.clear()

    async def _run(
        self, user_id: str, plan: dict[str, Any], approval_id: str | None, deadline: int, key_id: str | None,
    ) -> None:
        task_id = plan["task_id"]
        outcomes: dict[str, str] = {}
        steps = {step["key"]: step for step in plan["steps"]}
        maintenance = plan.get("maintenance")
        batches = maintenance["batches"] if maintenance else [[key] for key in steps]
        stop_remaining = False
        try:
            self.db.update_task(user_id, task_id, status="running")
            self.db.append_task_event(task_id, "task.started", {
                "plan_hash": plan["hash"], "via": "plan_key" if key_id is not None else "web",
            })
            for index, batch in enumerate(batches, 1):
                if maintenance and not stop_remaining:
                    self.db.append_task_event(task_id, "maintenance.batch_started", {"batch": index, "steps": batch})
                ready: dict[str, dict[str, Any]] = {}
                for key in batch:
                    step = steps[key]
                    step_id = step["id"]
                    row = self.db.get_task_step(step_id)
                    if row and row["status"] == "cancelled":
                        outcomes[key] = "cancelled"
                        continue
                    error = (
                        "previous_batch_failed" if stop_remaining else
                        "dependency_failed" if any(outcomes[dependency] != "completed" for dependency in step["depends_on"])
                        else None
                    )
                    if error:
                        self.db.update_task_step(step_id, status="skipped", error=error, completed_at=int(time.time()))
                        self.db.append_task_event(task_id, "step.skipped", {"error": error}, step_id=step_id)
                        outcomes[key] = "skipped"
                    else:
                        ready[key] = step
                if ready:
                    workers = {
                        step["id"]: asyncio.create_task(self._execute(user_id, plan, step, approval_id, deadline, key_id))
                        for step in ready.values()
                    }
                    self.in_flight[task_id] = workers
                    try:
                        results = await asyncio.gather(*workers.values(), return_exceptions=True)
                        for result in results:
                            if isinstance(result, Exception):
                                raise result
                    finally:
                        self.in_flight.pop(task_id, None)
                    for key, step in ready.items():
                        outcomes[key] = self.db.get_task_step(step["id"])["status"]
                if maintenance and not stop_remaining:
                    verified = all(outcomes[key] == "completed" for key in batch)
                    self.db.append_task_event(task_id, "maintenance.batch_finished", {"batch": index, "verified": verified})
                    if not verified:
                        stop_remaining = True
                        self.db.append_task_event(task_id, "maintenance.stopped", {"batch": index, "automatic_retry": False})
            successes = sum(status == "completed" for status in outcomes.values())
            status = ("completed" if successes == len(outcomes) else "partially_completed" if successes
                      else "verification_failed" if "verification_failed" in outcomes.values() else "failed")
            self.db.update_task(user_id, task_id, status=status, result={"steps": outcomes, "plan_hash": plan["hash"]})
            self.db.append_task_event(task_id, "task.finished", {"status": status})
        except Exception:
            for step in self.db.list_task_steps(user_id, task_id):
                if step["status"] in {"created", "running"}:
                    self.db.update_task_step(
                        step["id"], status="failed", error="unconfirmed", completed_at=int(time.time()),
                    )
            self.db.update_task(user_id, task_id, status="failed", error="unconfirmed")
            self._blocked(task_id, "unconfirmed")
            self.db.append_task_event(task_id, "task.finished", {"status": "failed"})
        finally:
            self.running.pop(task_id, None)

    async def _execute(
        self, user_id: str, plan: dict[str, Any], step: dict[str, Any], approval_id: str | None,
        deadline: int, key_id: str | None,
    ) -> None:
        task_id, step_id, agent_id = plan["task_id"], step["id"], step["agent_id"]
        action = parse_planned_action(step["operation"], step["params"])
        action_id: str | None = None
        denial_error: str | None = None
        fields = {"step_id": step_id, "agent_id": agent_id}
        target_policy = plan["targets"][agent_id].get("plan_policy", _DEFAULT_PLAN_POLICY)
        decision = self.policy.evaluate_plan(action, l1_paths=target_policy["l1_paths"])
        needs_approval = decision.decision == RiskDecision.APPROVAL_REQUIRED
        policy = {
            **decision.as_dict(), "decision": "allow",
            "plan_hash": plan["hash"], "approval_id": approval_id, "expires_at": deadline,
            "device_policy_revision": target_policy["revision"],
        }

        def authorization_error() -> str | None:
            now = time.time()
            if self._step_execution_blocked(user_id, plan, step):
                return EXECUTION_STOPPED
            if now >= deadline:
                return "expired"
            maintenance = plan.get("maintenance")
            if maintenance and now < maintenance["starts_at"]:
                return "outside_maintenance_window"
            if key_id is not None and not self.db.is_active_plan_key(user_id, key_id):
                return "key_revoked"
            if not self._check_target(user_id, agent_id, plan):
                return "device_changed"
            if decision.decision == RiskDecision.DENY or (needs_approval and not approval_id):
                return "policy_denied"
            return None

        def authorize_dispatch() -> None:
            error = authorization_error()
            if error is not None:
                raise PlanStepError(error)

        async def on_message(message: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal action_id, denial_error
            if message["type"] != "task_action_request":
                if message["type"] == "task_event" and message.get("event_type") in {"action_started", "action_completed"}:
                    if action_id and message["payload"].get("action_request_id") == action_id:
                        self.db.append_task_event(task_id, f"agent.{message['event_type']}", {}, **fields)
                return None
            candidate = message.get("action_request_id")
            expected = {
                "step_id": step_id, "plan_hash": plan["hash"], "operation": action.operation,
                "profile": action.profile, "action": action.action, "params": action.params,
                "risk_level": action.risk_level,
            }
            if (
                action_id is not None or not isinstance(candidate, str)
                or not re.fullmatch(r"action_[a-f0-9]{32}", candidate)
                or any(message.get(key) != value for key, value in expected.items())
            ):
                self._blocked(task_id, "unconfirmed", step)
                return {"decision": "deny", "reason": "动作与短期授权不匹配"}
            error = authorization_error()
            if error is not None:
                denial_error = error
                return {"decision": "deny", "reason": error}
            try:
                self.db.create_task_action(
                    action_id=candidate, task_id=task_id, step_id=step_id, agent_id=agent_id,
                    profile=action.profile, action=action.action, params=action.scope,
                    idempotency_key=step_id, scope=action.scope, risk_level=action.risk_level,
                    policy_context=policy, approval_requirement="approval_required" if needs_approval else "allow",
                    expires_at=deadline,
                )
            except ConflictError:
                self._blocked(task_id, "unconfirmed", step)
                return {"decision": "deny", "reason": "动作已提交，不会重复执行"}
            action_id = candidate
            self.db.update_task_action(action_id, status="running", started_at=int(time.time()))
            self.db.append_task_event(task_id, "action.authorized", {"policy": policy, "action_id": action_id}, **fields)
            return {"decision": "allow", "policy_context": policy}

        try:
            authorize_dispatch()
            self.db.update_task_step(step_id, status="running", started_at=int(time.time()))
            result = await self.hub.execute_task(
                agent_id, step_id, "预先审核的受控动作",
                [{"step_id": step_id, "operation": action.operation, "params": action.params}],
                timeout=min(30, max(0, deadline - time.time())), deadline=deadline, on_message=on_message,
                plan_hash=plan["hash"], expected_connection_id=plan["targets"][agent_id]["connection_id"],
                authorize_dispatch=authorize_dispatch,
                execution_revision=plan.get("execution_revision", 0),
                execution_operation=operation_group(action.profile, action.action, planned=True),
            )
            if (
                action_id is None or result.get("action_request_id") != action_id or result.get("step_id") != step_id
                or result.get("profile") != action.profile or result.get("action") != action.action
            ):
                raise PlanStepError("unconfirmed")
            digested = _verified_result(action, result.get("result"))
            summary = {"operation": action.operation, "plan_hash": plan["hash"], **digested}
            verified = digested.get("verified") is not False
            self.db.create_task_evidence(
                evidence_id=uuid.uuid4().hex, task_id=task_id,
                kind="operation_plan" if verified else "operation_plan_attempt",
                summary=summary, content_hash=digest_json(summary), **fields,
            )
            if not verified:
                error = "service.recovery_unconfirmed" if action.profile == SERVICE_PROFILE else "build.verification_failed"
                self.db.update_task_step(
                    step_id, status="verification_failed", result=digested, error=error, completed_at=int(time.time()),
                )
                self.db.update_task_action(
                    action_id, status="verification_failed", result=digested, error=error, completed_at=int(time.time()),
                )
                self.db.append_task_event(task_id, "action.verification_failed", {"action_id": action_id, "error": error}, **fields)
                self._warn(task_id, error, step)
                return
            self.db.update_task_step(step_id, status="completed", result=digested, completed_at=int(time.time()))
            self.db.update_task_action(action_id, status="completed", result=digested, completed_at=int(time.time()))
            self.db.append_task_event(task_id, "action.verified", {"action_id": action_id}, **fields)
            if action.operation == "service.inspect" and digested["state"] != "ready":
                self._warn(task_id, "service.not_ready", step)
        except asyncio.CancelledError:
            if action_id:
                self.db.update_task_action(action_id, status="cancelled", error="unconfirmed", completed_at=int(time.time()))
                self._blocked(task_id, "unconfirmed", step)
            raise
        except (AgentOfflineError, AgentCommandError, PlanStepError) as exc:
            error = denial_error or str(exc)
            status = "verification_failed" if error.startswith("verification_failed:") else "failed"
            code = error if error in _STEP_ERRORS else "unconfirmed"
            self.db.update_task_step(step_id, status=status, error=code, completed_at=int(time.time()))
            if action_id:
                self.db.update_task_action(action_id, status=status, error=code, completed_at=int(time.time()))
            self._blocked(task_id, code, step)


def _verified_result(action: PlannedAction, result: object) -> dict[str, Any]:
    failure = PlanStepError("verification_failed: 设备未返回可验证的证据")
    if not isinstance(result, dict):
        raise failure
    if action.profile in LOCAL_PLAN_PROFILES:
        try:
            return project_managed_result(action.operation, action.params, result)
        except ValueError as exc:
            raise PlanStepError(str(exc)) from None
    if action.operation == "shell.version":
        version = result.get("version")
        if (
            result.get("tool") != action.params["tool"] or not isinstance(version, str)
            or not re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,39}", version)
        ):
            raise failure
        return {"tool": result["tool"], "version": version}
    path = action.params["path"]
    digest = result.get("sha256")
    if result.get("path") != path or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise failure
    checked = {"path": path, "sha256": digest}
    if action.operation == "workspace.hash":
        return checked
    before = action.params.get("old_text", "")
    after = action.params.get("new_text", action.params.get("content", ""))
    previous = action.params.get("expected_sha256")
    if (
        result.get("changed") is not True or result.get("verified") is not True or digest == previous
        or (action.operation == "workspace.create" and hashlib.sha256(after.encode("utf-8")).hexdigest() != digest)
    ):
        raise failure
    return {
        **checked, "before_sha256": previous, "verified": True,
        "diff": "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{path}", f"b/{path}", lineterm="")),
    }
