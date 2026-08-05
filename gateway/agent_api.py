from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .agent_hub import AgentConnection, AgentHub
from .database import Database
from .profiles import validate_profiles
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


async def handle_agent_websocket(websocket: WebSocket, db: Database, hub: AgentHub) -> None:
    await websocket.accept()
    agent_id: str | None = None
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        if not isinstance(hello, dict) or hello.get("v") != 1 or hello.get("type") != "hello":
            raise ValueError("无效的 Agent 握手消息")
        profiles = validate_profiles(hello.get("profiles"))

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
                profiles,
            )
            if registered is None:
                raise ValueError("注册令牌无效、已过期或已被使用")
            user_id = registered["user_id"]
            await websocket.send_json(
                {
                    "v": 1,
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
                raise ValueError("Agent Profile 与注册记录不一致")
            user_id = agent["user_id"]
            await websocket.send_json({"v": 1, "type": "ready", "agent_id": agent_id})

        connection = AgentConnection(agent_id, user_id, websocket)
        await hub.register(connection)
        db.touch_agent(agent_id)

        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict) or message.get("v") != 1:
                continue
            message_type = message.get("type")
            if message_type == "pong":
                hub.record_pong(agent_id)
                db.touch_agent(agent_id)
            elif message_type == "result":
                await hub.resolve(agent_id, message)
    except (ValueError, asyncio.TimeoutError) as exc:
        await websocket.send_json({"v": 1, "type": "error", "error": str(exc)})
        await websocket.close(code=4000, reason="认证失败")
    except WebSocketDisconnect:
        pass
    finally:
        if agent_id is not None:
            await hub.unregister(agent_id, websocket)
            db.touch_agent(agent_id)

