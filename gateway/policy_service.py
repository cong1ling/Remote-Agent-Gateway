from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from remote_agent.profiles.common.planned_action import PlannedAction

from .task_models import RiskDecision


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: RiskDecision
    risk_level: str
    matched_rules: tuple[str, ...]
    reason: str
    allowed_scope: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "risk_level": self.risk_level,
            "matched_rules": list(self.matched_rules),
            "reason": self.reason,
            "allowed_scope": self.allowed_scope,
        }


class PolicyService:
    def evaluate_plan(self, action: PlannedAction, *, l1_paths: list[str]) -> PolicyDecision:
        level = action.risk_level
        if level == "L0":
            return PolicyDecision(
                RiskDecision.ALLOW, level, ("operation_plan.read_only",), "受控只读动作", action.scope,
            )
        if (
            level == "L1" and action.operation in {"workspace.create", "workspace.edit"}
            and action.params["path"] in l1_paths
        ):
            return PolicyDecision(
                RiskDecision.ALLOW, level, ("operation_plan.device_l1_path",), "设备所有者授权了此 L1 路径", action.scope,
            )
        if level in {"L1", "L2"}:
            return PolicyDecision(
                RiskDecision.APPROVAL_REQUIRED, level, ("operation_plan.human_approval",), "必须批准当前计划版本", action.scope,
            )
        return PolicyDecision(RiskDecision.DENY, level, ("operation_plan.default_deny",), "不允许此风险等级", {})

    def evaluate(
        self,
        *,
        profile: str,
        action: str,
        params: dict[str, Any],
        risk_level: str = "L0",
        action_interception: str = "full",
    ) -> PolicyDecision:
        if profile == "system_observe.v1" and action in {
            "get_host_info",
            "get_resources",
            "get_processes",
            "get_network",
        }:
            if params:
                return PolicyDecision(
                    RiskDecision.DENY,
                    "L0",
                    ("system_observe.no_parameters",),
                    "只读观察 Action 不接受额外参数",
                    {},
                )
            if risk_level != "L0":
                return PolicyDecision(
                    RiskDecision.DENY,
                    risk_level,
                    ("system_observe.read_only",),
                    "系统观察 Action 只能按 L0 风险执行",
                    {},
                )
            return PolicyDecision(
                RiskDecision.ALLOW,
                "L0",
                ("health_check.read_only",),
                "健康检查只读取固定系统观察数据",
                {"profile": profile, "action": action},
            )

        if action_interception == "none":
            return PolicyDecision(
                RiskDecision.DENY,
                risk_level,
                ("runtime.action_interception_missing",),
                "当前 Runtime 无法提供 Action 级拦截",
                {},
            )
        return PolicyDecision(
            RiskDecision.APPROVAL_REQUIRED,
            risk_level,
            ("default.mutation_requires_approval",),
            "该 Action 不属于一期免审批的只读健康检查",
            {},
        )
