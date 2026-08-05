import time
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from .agent_hub import AgentHub
from .config import Settings
from .database import ConflictError, Database
from .profiles import describe_profiles, profile_names
from .security import hash_password, hash_token, issue_token, verify_password


SESSION_COOKIE = "remote_gateway_session"


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class NamedItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class AgentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if normalized.count("@") != 1 or normalized.startswith("@") or normalized.endswith("@"):
        raise HTTPException(status_code=422, detail="请输入有效的邮箱地址")
    return normalized


def _agent_view(agent: dict[str, Any], hub: AgentHub) -> dict[str, Any]:
    result = dict(agent)
    result["online"] = hub.is_online(agent["id"])
    result["capabilities"] = profile_names(agent["profiles"])
    return result


def create_web_router(db: Database, settings: Settings, hub: AgentHub) -> APIRouter:
    router = APIRouter(prefix="/api")

    def current_user(
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> dict[str, Any]:
        if not session_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
        user = db.get_session_user(hash_token(session_token))
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
        return user

    User = Annotated[dict[str, Any], Depends(current_user)]

    def start_session(response: Response, user_id: str) -> None:
        token = issue_token("session")
        max_age = settings.session_days * 24 * 60 * 60
        db.create_session(token.digest, user_id, int(time.time()) + max_age)
        response.set_cookie(
            SESSION_COOKIE,
            token.value,
            max_age=max_age,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path="/",
        )

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/auth/register", status_code=201)
    def register(credentials: Credentials, response: Response) -> dict[str, Any]:
        email = _normalize_email(credentials.email)
        try:
            user = db.create_user(email, hash_password(credentials.password))
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        start_session(response, user["id"])
        return {"user": user}

    @router.post("/auth/login")
    def login(credentials: Credentials, response: Response) -> dict[str, Any]:
        email = _normalize_email(credentials.email)
        user = db.get_user_by_email(email)
        if user is None or not verify_password(credentials.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="邮箱或密码错误")
        start_session(response, user["id"])
        return {"user": {key: user[key] for key in ("id", "email", "created_at")}}

    @router.post("/auth/logout", status_code=204)
    def logout(
        response: Response,
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> None:
        if session_token:
            db.delete_session(hash_token(session_token))
        response.delete_cookie(SESSION_COOKIE, path="/")

    @router.get("/me")
    def me(user: User) -> dict[str, Any]:
        return {
            "user": user,
            "mcp_url": f"{settings.public_url}/mcp/",
            "agent_websocket_url": settings.websocket_url,
        }

    @router.get("/mcp-keys")
    def list_mcp_keys(user: User) -> dict[str, Any]:
        return {"keys": db.list_mcp_keys(user["id"])}

    @router.post("/mcp-keys", status_code=201)
    def create_mcp_key(item: NamedItem, user: User) -> dict[str, Any]:
        token = issue_token("mcp")
        key = db.create_mcp_key(user["id"], item.name.strip(), token.prefix, token.digest)
        return {"key": key, "secret": token.value}

    @router.delete("/mcp-keys/{key_id}", status_code=204)
    def revoke_mcp_key(key_id: str, user: User) -> None:
        if not db.revoke_mcp_key(user["id"], key_id):
            raise HTTPException(status_code=404, detail="MCP Key 不存在")

    @router.get("/enrollment-tokens")
    def list_enrollment_tokens(user: User) -> dict[str, Any]:
        return {"tokens": db.list_enrollment_tokens(user["id"])}

    @router.post("/enrollment-tokens", status_code=201)
    def create_enrollment_token(item: NamedItem, user: User) -> dict[str, Any]:
        token = issue_token("enroll")
        expires_at = int(time.time()) + settings.enrollment_minutes * 60
        record = db.create_enrollment_token(
            user["id"], item.name.strip(), token.prefix, token.digest, expires_at
        )
        command = (
            f'python agent.py --gateway "{settings.websocket_url}" '
            f'--token "{token.value}" --name "{item.name.strip()}"'
        )
        return {"token": record, "secret": token.value, "command": command}

    @router.delete("/enrollment-tokens/{token_id}", status_code=204)
    def revoke_enrollment_token(token_id: str, user: User) -> None:
        if not db.revoke_enrollment_token(user["id"], token_id):
            raise HTTPException(status_code=404, detail="注册令牌不存在或已被使用")

    @router.get("/agents")
    def list_agents(user: User) -> dict[str, Any]:
        return {"agents": [_agent_view(agent, hub) for agent in db.list_agents(user["id"])]}

    @router.get("/agents/{agent_id}")
    def get_agent(agent_id: str, user: User) -> dict[str, Any]:
        agent = db.get_owned_agent(user["id"], agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        result = _agent_view(agent, hub)
        result["profile_details"] = describe_profiles(agent["profiles"])
        return {"agent": result}

    @router.patch("/agents/{agent_id}")
    def update_agent(agent_id: str, update: AgentUpdate, user: User) -> dict[str, Any]:
        if not db.update_agent(user["id"], agent_id, update.name.strip(), update.description.strip()):
            raise HTTPException(status_code=404, detail="Agent 不存在")
        agent = db.get_owned_agent(user["id"], agent_id)
        return {"agent": _agent_view(agent, hub)}

    @router.delete("/agents/{agent_id}", status_code=204)
    async def revoke_agent(agent_id: str, user: User) -> None:
        if not db.revoke_agent(user["id"], agent_id):
            raise HTTPException(status_code=404, detail="Agent 不存在")
        await hub.disconnect(agent_id)

    return router
