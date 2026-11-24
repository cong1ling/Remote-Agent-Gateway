from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class InvalidTaskTransition(ValueError):
    pass


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_DEVICE = "waiting_device"
    PARTIALLY_COMPLETED = "partially_completed"
    VERIFICATION_FAILED = "verification_failed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class RiskDecision(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PLANNED, TaskStatus.CANCELLED}),
    TaskStatus.PLANNED: frozenset(
        {TaskStatus.WAITING_APPROVAL, TaskStatus.APPROVED, TaskStatus.CANCELLED}
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.APPROVED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.APPROVED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.WAITING_DEVICE, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_DEVICE,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.VERIFICATION_FAILED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_DEVICE: frozenset(
        {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.PARTIALLY_COMPLETED: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.VERIFICATION_FAILED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.VERIFICATION_FAILED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.ROLLED_BACK}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.ROLLED_BACK: frozenset(),
}


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value.strip()


@dataclass(slots=True)
class TaskRecord:
    id: str
    user_id: str
    objective: str
    status: TaskStatus = TaskStatus.CREATED
    target_selector: dict[str, Any] = field(default_factory=dict)
    requested_runtime: str | None = None
    risk_policy: dict[str, Any] = field(default_factory=dict)
    approval_policy: dict[str, Any] = field(default_factory=dict)
    resource_budget: dict[str, Any] = field(default_factory=dict)
    deadline: int | None = None
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)

    @classmethod
    def new(
        cls,
        user_id: str,
        objective: str,
        *,
        target_selector: dict[str, Any] | None = None,
        requested_runtime: str | None = None,
        risk_policy: dict[str, Any] | None = None,
        approval_policy: dict[str, Any] | None = None,
        resource_budget: dict[str, Any] | None = None,
        deadline: int | None = None,
    ) -> TaskRecord:
        return cls(
            id=_new_id("task"),
            user_id=_require_text(user_id, "user_id"),
            objective=_require_text(objective, "objective"),
            target_selector=dict(target_selector or {}),
            requested_runtime=requested_runtime,
            risk_policy=dict(risk_policy or {}),
            approval_policy=dict(approval_policy or {}),
            resource_budget=dict(resource_budget or {}),
            deadline=deadline,
        )

    def transition(self, status: TaskStatus) -> None:
        if status == self.status:
            return
        allowed = _TASK_TRANSITIONS[self.status]
        if status not in allowed:
            raise InvalidTaskTransition(
                f"任务 {self.id} 不能从 {self.status.value} 转为 {status.value}"
            )
        self.status = status
        self.updated_at = _now()


@dataclass(slots=True)
class StepRecord:
    id: str
    task_id: str
    action_type: str
    status: TaskStatus = TaskStatus.CREATED
    target_agent_id: str | None = None
    depends_on: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    expected_result: dict[str, Any] = field(default_factory=dict)
    retry_policy: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    started_at: int | None = None
    completed_at: int | None = None

    @classmethod
    def new(
        cls,
        task_id: str,
        action_type: str,
        *,
        target_agent_id: str | None = None,
        depends_on: list[str] | None = None,
        required_capabilities: list[str] | None = None,
        inputs: dict[str, Any] | None = None,
        expected_result: dict[str, Any] | None = None,
        retry_policy: dict[str, Any] | None = None,
    ) -> StepRecord:
        task_id = _require_text(task_id, "task_id")
        action_type = _require_text(action_type, "action_type")
        step_id = _new_id("step")
        return cls(
            id=step_id,
            task_id=task_id,
            action_type=action_type,
            target_agent_id=target_agent_id,
            depends_on=list(depends_on or []),
            required_capabilities=list(required_capabilities or []),
            inputs=dict(inputs or {}),
            expected_result=dict(expected_result or {}),
            retry_policy=dict(retry_policy or {}),
            idempotency_key=f"{task_id}:{step_id}",
        )


@dataclass(slots=True)
class ActionRequest:
    id: str
    task_id: str
    step_id: str
    agent_id: str
    profile: str
    action: str
    params: dict[str, Any]
    scope: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "L0"
    policy_context: dict[str, Any] = field(default_factory=dict)
    approval_requirement: RiskDecision = RiskDecision.ALLOW
    expires_at: int | None = None
    idempotency_key: str = ""

    @classmethod
    def new(
        cls,
        task_id: str,
        step_id: str,
        agent_id: str,
        profile: str,
        action: str,
        params: dict[str, Any],
        *,
        scope: dict[str, Any] | None = None,
        risk_level: str = "L0",
        policy_context: dict[str, Any] | None = None,
        approval_requirement: RiskDecision = RiskDecision.ALLOW,
        expires_at: int | None = None,
    ) -> ActionRequest:
        task_id = _require_text(task_id, "task_id")
        step_id = _require_text(step_id, "step_id")
        agent_id = _require_text(agent_id, "agent_id")
        profile = _require_text(profile, "profile")
        action = _require_text(action, "action")
        if not isinstance(params, dict):
            raise ValueError("params 必须是对象")
        return cls(
            id=_new_id("action"),
            task_id=task_id,
            step_id=step_id,
            agent_id=agent_id,
            profile=profile,
            action=action,
            params=dict(params),
            scope=dict(scope or {}),
            risk_level=_require_text(risk_level, "risk_level"),
            policy_context=dict(policy_context or {}),
            approval_requirement=approval_requirement,
            expires_at=expires_at,
            idempotency_key=f"{task_id}:{step_id}:{profile}:{action}",
        )


@dataclass(slots=True)
class ApprovalRecord:
    id: str
    task_id: str
    plan_hash: str
    approved_scope: dict[str, Any]
    decision: str
    approver_user_id: str | None = None
    reason: str = ""
    created_at: int = field(default_factory=_now)
    expires_at: int | None = None


@dataclass(slots=True)
class TaskEvent:
    id: str
    task_id: str
    event_type: str
    payload: dict[str, Any]
    sequence: int
    step_id: str | None = None
    agent_id: str | None = None
    created_at: int = field(default_factory=_now)


@dataclass(slots=True)
class EvidenceRecord:
    id: str
    task_id: str
    kind: str
    summary: dict[str, Any]
    content_hash: str | None = None
    storage_ref: str | None = None
    step_id: str | None = None
    agent_id: str | None = None
    created_at: int = field(default_factory=_now)
    expires_at: int | None = None
