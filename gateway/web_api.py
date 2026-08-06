import time
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent_builder import AgentBuildError, PythonAgentBuilder, new_bundle_id
from .agent_hub import AgentHub
from .agent_protocol import agent_version_status
from .config import Settings
from .database import ConflictError, Database
from .profiles import (
    describe_profile_declarations,
    profile_names,
    public_profile_catalog,
    validate_profiles,
)
from .security import IssuedToken, hash_password, hash_token, issue_token, verify_password


SESSION_COOKIE = "remote_gateway_session"


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class NamedItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class AgentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class AgentBundleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    runtime: str = Field(default="python", max_length=32)
    profiles: list[str] = Field(min_length=1, max_length=16)


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if normalized.count("@") != 1 or normalized.startswith("@") or normalized.endswith("@"):
        raise HTTPException(status_code=422, detail="请输入有效的邮箱地址")
    return normalized


def _agent_view(agent: dict[str, Any], hub: AgentHub) -> dict[str, Any]:
    result = dict(agent)
    result["online"] = hub.is_online(agent["id"])
    result["capabilities"] = profile_names(agent["profiles"])
    result["requires_reenrollment"] = not agent["profile_declarations"]
    result["version_status"] = agent_version_status(agent["version"])
    return result


def create_web_router(db: Database, settings: Settings, hub: AgentHub) -> APIRouter:
    router = APIRouter(prefix="/api")
    python_builder = PythonAgentBuilder(settings.bundles_dir)

    def cleanup_expired_bundles() -> None:
        for filename in db.cleanup_expired_agent_bundles():
            (settings.bundles_dir / filename).unlink(missing_ok=True)

    def issue_agent_enrollment(
        user_id: str, label: str
    ) -> tuple[IssuedToken, dict[str, Any], int]:
        token = issue_token("enroll")
        expires_at = int(time.time()) + settings.enrollment_minutes * 60
        record = db.create_enrollment_token(
            user_id,
            label,
            token.prefix,
            token.digest,
            expires_at,
        )
        return token, record, expires_at

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

    @router.get("/session")
    def session_status(
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> dict[str, Any]:
        user = db.get_session_user(hash_token(session_token)) if session_token else None
        if user is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "user": user,
            "mcp_url": f"{settings.public_url}/mcp/",
            "agent_websocket_url": settings.websocket_url,
        }

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
        token, record, _ = issue_agent_enrollment(user["id"], item.name.strip())
        command = (
            f'python agent.py --gateway "{settings.websocket_url}" '
            f'--token "{token.value}"'
        )
        return {"token": record, "secret": token.value, "command": command}

    @router.delete("/enrollment-tokens/{token_id}", status_code=204)
    def revoke_enrollment_token(token_id: str, user: User) -> None:
        if not db.revoke_enrollment_token(user["id"], token_id):
            raise HTTPException(status_code=404, detail="注册令牌不存在或已被使用")

    @router.get("/agents")
    def list_agents(user: User) -> dict[str, Any]:
        return {"agents": [_agent_view(agent, hub) for agent in db.list_agents(user["id"])]}

    @router.get("/profile-catalog")
    def profile_catalog(user: User) -> dict[str, Any]:
        del user
        return {"profiles": public_profile_catalog(), "runtimes": ["python"]}

    @router.get("/agent-bundles")
    def list_agent_bundles(user: User) -> dict[str, Any]:
        cleanup_expired_bundles()
        bundles = db.list_agent_bundles(user["id"])
        for bundle in bundles:
            bundle["download_url"] = f"/api/agent-bundles/{bundle['id']}/download"
        return {"bundles": bundles}

    @router.post("/agent-bundles", status_code=201)
    def create_agent_bundle(request: AgentBundleRequest, user: User) -> dict[str, Any]:
        if request.runtime != "python":
            raise HTTPException(status_code=422, detail="当前只支持组装 Python Agent")
        name = request.name.strip()
        description = request.description.strip()
        try:
            selected_profiles = validate_profiles(request.profiles)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        cleanup_expired_bundles()
        bundle_id = new_bundle_id()
        try:
            path, filename = python_builder.build(
                bundle_id=bundle_id,
                name=name,
                description=description,
                profiles=selected_profiles,
            )
        except (ValueError, AgentBuildError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        token_record: dict[str, Any] | None = None
        try:
            enrollment, token_record, expires_at = issue_agent_enrollment(
                user["id"], f"Agent Bundle: {name}"
            )
            bundle = db.create_agent_bundle(
                bundle_id,
                user["id"],
                token_record["id"],
                name,
                description,
                request.runtime,
                selected_profiles,
                filename,
                expires_at,
            )
        except Exception:
            path.unlink(missing_ok=True)
            if token_record is not None:
                db.revoke_enrollment_token(user["id"], token_record["id"])
            raise
        bundle["download_url"] = f"/api/agent-bundles/{bundle_id}/download"
        command = (
            f'python run.py --gateway "{settings.websocket_url}" '
            f'--token "{enrollment.value}"'
        )
        return {"bundle": bundle, "secret": enrollment.value, "command": command}

    @router.get("/agent-bundles/{bundle_id}/download")
    def download_agent_bundle(bundle_id: str, user: User) -> FileResponse:
        bundle = db.get_owned_agent_bundle(user["id"], bundle_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="Agent 组装包不存在")
        if bundle["expires_at"] <= int(time.time()):
            raise HTTPException(status_code=410, detail="Agent 组装包已过期，请重新生成")
        path = (settings.bundles_dir / bundle["filename"]).resolve()
        bundles_root = settings.bundles_dir.resolve()
        if path.parent != bundles_root or not path.is_file():
            raise HTTPException(status_code=404, detail="Agent 组装包文件不存在")
        return FileResponse(path, media_type="application/zip", filename=bundle["filename"])

    @router.get("/agents/{agent_id}")
    def get_agent(agent_id: str, user: User) -> dict[str, Any]:
        agent = db.get_owned_agent(user["id"], agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        result = _agent_view(agent, hub)
        result["profile_details"] = describe_profile_declarations(
            agent["profile_declarations"]
        )
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
