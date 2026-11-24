from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .agent_hub import AgentConnection, AgentHub
from .agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    validate_agent_features,
    validate_agent_version,
    validate_device_declaration,
    validate_runtime_declarations,
)
from .database import Database
from .profiles import (
    profile_ids,
    validate_profile_agent_compatibility,
    validate_profile_declarations,
)
from .security import hash_token, issue_token


def _text(value: object, field: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field} 不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field} 不能超过 {maximum} 个字符")
    return normalized


def _changed_device_components(
    before: dict[str, Any], device: dict[str, Any], version: dict[str, Any], runtimes: list[dict[str, Any]],
) -> list[str]:
    changes = []
    if before["device"] and device and any(
        before["device"].get(key) != device[key] for key in ("platform", "platform_version")
    ):
        changes.append("platform")
    if all(key in before["version"] for key in ("agent_version", "core_version")) and any(
        before["version"][key] != version[key] for key in ("agent_version", "core_version")
    ):
        changes.append("agent_version")
    runtime_keys = ("provider_id", "runtime_version", "adapter_version", "mode")
    previous_ids = {tuple(item.get(key) for key in runtime_keys) for item in before["runtimes"]}
    current_ids = {tuple(item.get(key) for key in runtime_keys) for item in runtimes}
    if previous_ids != current_ids:
        changes.append("runtimes")
    return changes


async def handle_agent_websocket(websocket: WebSocket, db: Database, hub: AgentHub) -> None:
    await websocket.accept()
    agent_id: str | None = None
    changed: list[str] = []
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise ValueError("无效的 Agent 握手消息")
        if hello.get("v") != AGENT_PROTOCOL_VERSION:
            raise ValueError(
                f"Agent 协议版本不兼容，需要 v{AGENT_PROTOCOL_VERSION}；请重新下载新版 Agent"
            )
        profile_declarations = validate_profile_declarations(hello.get("profiles"))
        profiles = profile_ids(profile_declarations)
        version = validate_agent_version(hello.get("version"), AGENT_PROTOCOL_VERSION)
        features = validate_agent_features(hello.get("features"))
        runtimes = validate_runtime_declarations(hello.get("runtimes"))
        device = validate_device_declaration(hello.get("device"))
        validate_profile_agent_compatibility(profile_declarations, version)

        enrollment_token = hello.get("enrollment_token")
        if isinstance(enrollment_token, str) and enrollment_token:
            name = _text(hello.get("name"), "name", 80)
            description = _text(hello.get("description", ""), "description", 500, required=False)
            credential = issue_token("agent")
            agent_id = str(uuid.uuid4())
            registered = db.register_agent(
                hash_token(enrollment_token),
                agent_id,
                credential.prefix,
                credential.digest,
                name,
                description,
                profile_declarations,
                version,
                features,
                runtimes,
                device=device,
            )
            if registered is None:
                raise ValueError("注册令牌无效、已过期或已被使用")
            user_id = registered["user_id"]
            await websocket.send_json(
                {
                    "v": AGENT_PROTOCOL_VERSION,
                    "type": "registered",
                    "agent_id": agent_id,
                    "credential": credential.value,
                }
            )
        else:
            agent_id = _text(hello.get("agent_id"), "agent_id", 64)
            credential_value = _text(hello.get("credential"), "credential", 256)
            agent = db.authenticate_agent(agent_id, hash_token(credential_value))
            if agent is None:
                raise ValueError("Agent 设备凭据无效或已被撤销")
            if agent["profiles"] != profiles:
                raise ValueError("Agent Profile 与注册记录不一致，请重新注册 Agent")
            changed = _changed_device_components(agent, device, version, runtimes)
            db.update_agent_runtime(agent_id, profile_declarations, version, features, runtimes, device=device)
            user_id = agent["user_id"]
            await websocket.send_json(
                {"v": AGENT_PROTOCOL_VERSION, "type": "ready", "agent_id": agent_id}
            )

        connection = AgentConnection(
            agent_id,
            user_id,
            websocket,
            features=features,
            runtimes=runtimes,
        )
        await hub.register(connection)
        if changed:
            db.record_device_alert(
                user_id, agent_id, "device_version_changed", "设备或本地 Runtime 的版本声明发生变化",
                "核对这是否为计划内变更，并检查声明与兼容目录",
                basis={"components": changed}, severity="info",
            )
        db.touch_agent(agent_id)

        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict) or message.get("v") != AGENT_PROTOCOL_VERSION:
                continue
            message_type = message.get("type")
            if message_type == "pong":
                hub.record_pong(connection)
                db.touch_agent(agent_id)
            elif message_type == "result":
                await hub.resolve(connection, message)
            elif message_type in {
                "task_ack",
                "task_event",
                "task_action_request",
                "task_result",
                "task_error",
            }:
                await hub.resolve_task(connection, message)
    except (ValueError, asyncio.TimeoutError) as exc:
        await websocket.send_json(
            {"v": AGENT_PROTOCOL_VERSION, "type": "error", "error": str(exc)}
        )
        await websocket.close(code=4000, reason="认证失败")
    except WebSocketDisconnect:
        pass
    finally:
        if agent_id is not None:
            await hub.unregister(agent_id, websocket)
            db.touch_agent(agent_id)
