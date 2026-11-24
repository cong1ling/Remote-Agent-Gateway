import asyncio
import json
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from remote_agent.profiles.common.planned_action import planned_workspace_risk

from .agent_builder import AgentBuildError, PythonAgentBuilder, new_bundle_id
from .agent_hub import AgentHub
from .agent_protocol import agent_version_status
from .config import Settings
from .database import ConflictError, Database
from .device_directory import DeviceFilters, DeviceLabel, directory_fields
from .execution_controls import OPERATION_GROUPS
from .health_check_service import HealthCheckError, HealthCheckService
from .health_monitor import HealthMonitor
from .operation_plan_service import (
    ApprovalRequest,
    BoundPlanRequest,
    OperationPlanDraft,
    OperationPlanRevision,
    OperationPlanService,
    PlanNotFound,
)
from .profiles import (
    describe_profile_declarations,
    profile_names,
    public_profile_catalog,
    validate_profiles,
)
from .runtime_catalog import HEALTH_RUNTIME_CATALOG
from .security import IssuedToken, hash_password, hash_token, issue_token, verify_password
from .shell_policy import describe_shell_policy


SESSION_COOKIE = "remote_gateway_session"


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class NamedItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class McpKeyRequest(NamedItem):
    scope: Literal["legacy", "plan"] = "legacy"


class AgentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    environment: DeviceLabel | None = None
    tags: list[DeviceLabel] = Field(default_factory=list, max_length=12)


class ShellPolicyUpdate(BaseModel):
    mode: Literal["off", "standard"]


class PlanPolicyUpdate(BaseModel):
    l1_paths: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(max_length=16)


class ExecutionStopUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["account", "device", "runtime", "operation"]
    agent_id: str = Field(default="", max_length=80)
    subject: str = Field(default="", max_length=80)
    stopped: bool = Field(strict=True)


AlertStatus = Literal["open", "acknowledged", "approved", "blocked", "resolved", "ignored"]


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    previous_status: AlertStatus
    status: Literal["acknowledged", "resolved", "ignored"]


class HealthMonitorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_minutes: Literal[15, 30, 60] | None


class AgentBundleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    runtime: str = Field(default="python", max_length=32)
    profiles: list[str] = Field(min_length=1, max_length=16)


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["health_check"] = "health_check"
    objective: str = Field(default="跨设备只读健康检查", min_length=1, max_length=240)
    agent_ids: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=64)
    target_selector: DeviceFilters | None = None
    requested_runtime: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,79}$")


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if normalized.count("@") != 1 or normalized.startswith("@") or normalized.endswith("@"):
        raise HTTPException(status_code=422, detail="请输入有效的邮箱地址")
    return normalized


def _agent_view(agent: dict[str, Any], hub: AgentHub) -> dict[str, Any]:
    result = {**agent, **directory_fields(agent)}
    result["online"] = hub.is_online(agent["id"])
    result["capabilities"] = profile_names(agent["profiles"])
    result["features"] = agent["features"]
    result["runtimes"] = agent["runtimes"]
    result["health_runtimes"] = [
        provider_id for provider_id in HEALTH_RUNTIME_CATALOG
        if "system_observe.v1" in agent["profiles"] and hub.supports_task_runtime(agent["id"], provider_id)
    ]
    result["requires_reenrollment"] = not agent["profile_declarations"]
    result["version_status"] = agent_version_status(agent["version"])
    result["shell_policy"] = (
        describe_shell_policy(agent["shell_policy_mode"])
        if "shell.v1" in agent["profiles"]
        else None
    )
    return result


def create_web_router(
    db: Database,
    settings: Settings,
    hub: AgentHub,
    health_checks: HealthCheckService,
    operation_plans: OperationPlanService,
    health_monitor: HealthMonitor,
) -> APIRouter:
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
    def create_mcp_key(item: McpKeyRequest, user: User) -> dict[str, Any]:
        token = issue_token("mcp")
        key = db.create_mcp_key(user["id"], item.name.strip(), token.prefix, token.digest, scope=item.scope)
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
    def list_agents(user: User, filters: Annotated[DeviceFilters, Query()]) -> dict[str, Any]:
        return {"agents": [
            _agent_view(agent, hub) for agent in db.list_agents(user["id"]) if filters.matches(agent)
        ]}

    @router.get("/execution-controls")
    def execution_controls(user: User) -> dict[str, Any]:
        return {
            "stops": db.execution_stops(user["id"]), "operation_groups": OPERATION_GROUPS,
            "runtime_ids": sorted(HEALTH_RUNTIME_CATALOG),
        }

    @router.patch("/execution-controls")
    async def update_execution_stop(update: ExecutionStopUpdate, user: User) -> dict[str, Any]:
        device_scope = update.scope in {"device", "runtime"}
        subjects = HEALTH_RUNTIME_CATALOG if update.scope == "runtime" else OPERATION_GROUPS if update.scope == "operation" else {""}
        if bool(update.agent_id) != device_scope or update.subject not in subjects:
            raise HTTPException(status_code=422, detail="停机范围或目标无效")
        if device_scope and db.get_owned_agent(user["id"], update.agent_id) is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        db.set_execution_stop(
            update.scope, stopped=update.stopped, user_id=user["id"], agent_id=update.agent_id, subject=update.subject,
        )
        interrupted = await hub.enforce_execution_stops(user["id"]) if update.stopped else 0
        return {"stops": db.execution_stops(user["id"]), "interrupted": interrupted}

    @router.post("/tasks", status_code=202)
    async def create_task(request: TaskCreateRequest, user: User) -> dict[str, Any]:
        try:
            if "target_selector" in request.model_fields_set and request.target_selector is None:
                raise HealthCheckError("target_selector 不能为 null")
            task = health_checks.create_task(
                user["id"],
                agent_ids=request.agent_ids if "agent_ids" in request.model_fields_set else None,
                target_selector=request.target_selector,
                objective=request.objective.strip(),
                requested_runtime=request.requested_runtime,
            )
        except HealthCheckError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        health_checks.enqueue(user["id"], task["id"])
        return {"task": task}

    @router.get("/health-monitors")
    def list_health_monitors(user: User) -> dict[str, Any]:
        return {"monitors": health_monitor.list(user["id"])}

    @router.put("/health-monitors/{agent_id}")
    async def configure_health_monitor(agent_id: str, update: HealthMonitorUpdate, user: User) -> dict[str, Any]:
        try:
            if not await health_monitor.configure(user["id"], agent_id, update.interval_minutes):
                raise HTTPException(status_code=404, detail="Agent 不存在")
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"monitors": health_monitor.list(user["id"])}

    @router.get("/tasks")
    def list_tasks(user: User) -> dict[str, Any]:
        return {"tasks": db.list_tasks(user["id"])}

    def plan_response(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return {"task": action()}
        except PlanNotFound as exc:
            raise HTTPException(status_code=404, detail="计划不存在") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/task-plans", status_code=201)
    async def create_operation_plan(request: OperationPlanDraft, user: User) -> dict[str, Any]:
        return plan_response(lambda: operation_plans.save(user["id"], request))

    @router.put("/tasks/{task_id}/plan")
    async def revise_operation_plan(task_id: str, request: OperationPlanRevision, user: User) -> dict[str, Any]:
        return plan_response(lambda: operation_plans.save(
            user["id"], request.plan, task_id=task_id, previous_hash=request.plan_hash,
        ))

    @router.post("/tasks/{task_id}/approval")
    async def decide_operation_plan(task_id: str, request: ApprovalRequest, user: User) -> dict[str, Any]:
        return plan_response(lambda: operation_plans.decide(
            user["id"], task_id, request.plan_hash, approve=request.decision == "approve",
        ))

    @router.post("/tasks/{task_id}/execute", status_code=202)
    async def execute_operation_plan(task_id: str, request: BoundPlanRequest, user: User) -> dict[str, Any]:
        return plan_response(lambda: operation_plans.start(user["id"], task_id, request.plan_hash))

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, user: User) -> dict[str, Any]:
        return plan_response(lambda: operation_plans.detail(user["id"], task_id))

    @router.get("/tasks/{task_id}/events")
    def list_task_events(
        task_id: str, user: User, after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    ) -> dict[str, Any]:
        if db.get_task(user["id"], task_id) is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        return {"events": db.list_task_events(user["id"], task_id, after_sequence=after_sequence)}

    @router.get("/tasks/{task_id}/events/live")
    async def live_task_events(
        task_id: str, request: Request, user: User, after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    ) -> StreamingResponse:
        user_id = user["id"]
        if db.get_task(user_id, task_id) is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        token_hash = hash_token(request.cookies[SESSION_COOKIE])
        client_cursor = request.headers.get("Last-Event-ID", "")
        if client_cursor.isascii() and client_cursor.isdecimal() and len(client_cursor) <= 18:
            after_sequence = max(after_sequence, int(client_cursor))
        terminal = {"completed", "partially_completed", "failed", "verification_failed", "cancelled", "rolled_back"}

        async def stream():
            cursor = after_sequence
            yield "retry: 2000\n\n"
            for iteration in range(120):
                session_user = db.get_session_user(token_hash)
                task = db.get_task(user_id, task_id)
                if not session_user or session_user["id"] != user_id or task is None:
                    return
                events = db.list_task_events(user_id, task_id, after_sequence=cursor, limit=200)
                for event in events:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: task_event\ndata: {json.dumps(event)}\n\n"
                if len(events) == 200:
                    continue
                if task["status"] in terminal:
                    yield f"event: task_finished\ndata: {json.dumps({'status': task['status']})}\n\n"
                    return
                if iteration % 30 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
        })

    @router.get("/tasks/{task_id}/evidence")
    def list_task_evidence(task_id: str, user: User) -> dict[str, Any]:
        if db.get_task(user["id"], task_id) is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        return {"evidence": db.list_task_evidence(user["id"], task_id)}

    @router.get("/tasks/{task_id}/alerts")
    def list_task_alerts(task_id: str, user: User) -> dict[str, Any]:
        if db.get_task(user["id"], task_id) is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        return {"alerts": db.list_task_alerts(user["id"], task_id)}

    @router.get("/alerts")
    def alerts(
        user: User, status: AlertStatus | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> dict[str, Any]:
        return {"alerts": db.list_alerts(user["id"], status=status, limit=limit)}

    def owned_alert(user_id: str, alert_id: str) -> dict[str, Any]:
        alert = db.alert_detail(user_id, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        return {"alert": alert}

    @router.get("/alerts/{alert_id}")
    def alert_detail(alert_id: str, user: User) -> dict[str, Any]:
        return owned_alert(user["id"], alert_id)

    @router.patch("/alerts/{alert_id}")
    def update_alert(alert_id: str, update: AlertUpdate, user: User) -> dict[str, Any]:
        try:
            if not db.update_alert(user["id"], alert_id, previous=update.previous_status, status=update.status):
                raise HTTPException(status_code=404, detail="告警不存在")
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return owned_alert(user["id"], alert_id)

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, user: User) -> dict[str, Any]:
        if db.get_task(user["id"], task_id) is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        service = operation_plans if db.get_task_plan(user["id"], task_id) else health_checks
        cancelled = await service.cancel(user["id"], task_id)
        return {"cancelled": cancelled}

    @router.post("/tasks/{task_id}/steps/{step_id}/cancel")
    async def cancel_step(task_id: str, step_id: str, user: User) -> dict[str, Any]:
        if db.get_task(user["id"], task_id) is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        service = operation_plans if db.get_task_plan(user["id"], task_id) else health_checks
        cancelled = await service.cancel_step(user["id"], task_id, step_id)
        return {"cancelled": cancelled}

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
        fields = update.model_fields_set
        updated = db.update_agent(
            user["id"], agent_id, update.name.strip(), update.description.strip(),
            environment=(update.environment or "") if "environment" in fields else None,
            tags=update.tags if "tags" in fields else None,
        )
        agent = db.get_owned_agent(user["id"], agent_id) if updated else None
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        return {"agent": _agent_view(agent, hub)}

    @router.patch("/agents/{agent_id}/shell-policy")
    def update_agent_shell_policy(
        agent_id: str, update: ShellPolicyUpdate, user: User
    ) -> dict[str, Any]:
        agent = db.get_owned_agent(user["id"], agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        if "shell.v1" not in agent["profiles"]:
            raise HTTPException(status_code=422, detail="该 Agent 未声明 shell.v1")
        db.update_agent_shell_policy(user["id"], agent_id, update.mode)
        updated = db.get_owned_agent(user["id"], agent_id)
        return {"agent": _agent_view(updated, hub)}

    @router.patch("/agents/{agent_id}/plan-policy")
    async def update_agent_plan_policy(agent_id: str, update: PlanPolicyUpdate, user: User) -> dict[str, Any]:
        agent = db.get_owned_agent(user["id"], agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        if update.l1_paths and ("workspace.v1" not in agent["profiles"] or not agent["features"].get("planned_l1_policy")):
            raise HTTPException(status_code=422, detail="该 Agent 不支持工作区 L1 设备授权，请重新组装")
        try:
            if any(planned_workspace_risk(path) != "L1" for path in update.l1_paths):
                raise ValueError("免审批只能授权普通 .txt 或 .md 文件，不包括配置文件")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.update_agent_plan_policy(user["id"], agent_id, update.l1_paths)
        return {"agent": _agent_view(db.get_owned_agent(user["id"], agent_id), hub)}

    @router.delete("/agents/{agent_id}", status_code=204)
    async def revoke_agent(agent_id: str, user: User) -> None:
        if not db.revoke_agent(user["id"], agent_id):
            raise HTTPException(status_code=404, detail="Agent 不存在")
        await hub.disconnect(agent_id)

    return router
