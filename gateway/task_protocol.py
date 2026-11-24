from __future__ import annotations

from typing import Any

from .agent_protocol import AGENT_PROTOCOL_VERSION


TASK_PROTOCOL_VERSION = 1
TASK_MESSAGE_TYPES = frozenset(
    {
        "task_start",
        "task_ack",
        "task_event",
        "task_action_request",
        "task_action_decision",
        "task_cancel",
        "task_step_cancel",
        "task_result",
        "task_error",
    }
)


class TaskProtocolError(ValueError):
    pass


def _required_text(message: dict[str, Any], field_name: str) -> str:
    value = message.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise TaskProtocolError(f"{field_name} 无效")
    return value.strip()


def validate_task_message(
    value: object,
    *,
    expected_type: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskProtocolError("Task 消息必须是对象")
    if value.get("v") != AGENT_PROTOCOL_VERSION:
        raise TaskProtocolError("Task 消息的 WebSocket 协议版本无效")
    if value.get("task_protocol", TASK_PROTOCOL_VERSION) != TASK_PROTOCOL_VERSION:
        raise TaskProtocolError("Task 协议版本无效")
    message_type = value.get("type")
    if message_type not in TASK_MESSAGE_TYPES:
        raise TaskProtocolError("未知的 Task 消息类型")
    if expected_type is not None and message_type != expected_type:
        raise TaskProtocolError(f"期待 {expected_type}，实际为 {message_type}")
    task_id = _required_text(value, "task_id")

    if message_type == "task_ack":
        status = value.get("status", "accepted")
        if status not in {"accepted", "running", "rejected"}:
            raise TaskProtocolError("task_ack.status 无效")
    elif message_type == "task_event":
        _required_text(value, "event_id")
        event_type = _required_text(value, "event_type")
        sequence = value.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise TaskProtocolError("task_event.sequence 无效")
        payload = value.get("payload", {})
        if not isinstance(payload, dict):
            raise TaskProtocolError("task_event.payload 必须是对象")
        value = {**value, "event_type": event_type, "payload": payload}
    elif message_type == "task_action_request":
        _required_text(value, "step_id")
        _required_text(value, "action_request_id")
        _required_text(value, "profile")
        _required_text(value, "action")
        params = value.get("params", {})
        if not isinstance(params, dict):
            raise TaskProtocolError("task_action_request.params 必须是对象")
        value = {**value, "params": params}
    elif message_type == "task_action_decision":
        _required_text(value, "action_request_id")
        decision = value.get("decision")
        if decision not in {"allow", "deny"}:
            raise TaskProtocolError("task_action_decision.decision 无效")
    elif message_type == "task_step_cancel":
        _required_text(value, "step_id")
    elif message_type == "task_result":
        ok = value.get("ok")
        if not isinstance(ok, bool):
            raise TaskProtocolError("task_result.ok 无效")
        result = value.get("result", {})
        if not isinstance(result, dict):
            raise TaskProtocolError("task_result.result 必须是对象")
    elif message_type == "task_error":
        _required_text(value, "error")
    return {**value, "task_id": task_id, "type": message_type}


def make_task_start(
    task_id: str,
    objective: str,
    steps: list[dict[str, Any]],
    *,
    risk_policy: dict[str, Any] | None = None,
    deadline: int | None = None,
    requested_runtime: str | None = None,
    plan_hash: str | None = None,
) -> dict[str, Any]:
    message = {
        "v": AGENT_PROTOCOL_VERSION,
        "type": "task_start",
        "task_protocol": TASK_PROTOCOL_VERSION,
        "task_id": task_id,
        "objective": objective,
        "steps": steps,
        "risk_policy": dict(risk_policy or {}),
        "deadline": deadline,
    }
    if requested_runtime is not None:
        message["requested_runtime"] = requested_runtime
    if plan_hash is not None:
        message["plan_hash"] = plan_hash
    return message


def make_task_action_decision(
    task_id: str,
    action_request_id: str,
    decision: str,
    *,
    reason: str = "",
    policy_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = {
        "v": AGENT_PROTOCOL_VERSION,
        "type": "task_action_decision",
        "task_protocol": TASK_PROTOCOL_VERSION,
        "task_id": task_id,
        "action_request_id": action_request_id,
        "decision": decision,
        "reason": reason,
        "policy_context": dict(policy_context or {}),
    }
    return validate_task_message(message)


def make_task_cancel(task_id: str, reason: str = "任务已取消") -> dict[str, Any]:
    return {
        "v": AGENT_PROTOCOL_VERSION,
        "type": "task_cancel",
        "task_protocol": TASK_PROTOCOL_VERSION,
        "task_id": task_id,
        "reason": reason,
    }


def make_task_step_cancel(task_id: str, step_id: str) -> dict[str, Any]:
    return validate_task_message({
        "v": AGENT_PROTOCOL_VERSION,
        "type": "task_step_cancel",
        "task_protocol": TASK_PROTOCOL_VERSION,
        "task_id": task_id,
        "step_id": step_id,
    })
