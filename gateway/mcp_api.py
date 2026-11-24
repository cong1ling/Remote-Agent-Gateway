from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ContentBlock, Tool
from remote_agent.core.limits import MAX_CONCURRENT_REQUESTS_PER_AGENT

from .agent_hub import AgentCommandError, AgentExecutionStopped, AgentHub, AgentOfflineError
from .agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    CURRENT_AGENT_VERSION,
    CURRENT_CORE_VERSION,
    agent_version_status,
)
from .config import Settings
from .database import ConflictError, Database
from .device_directory import DeviceFilters, directory_fields
from .execution_controls import STOP_EXPLANATION
from .operation_plan_service import OperationPlanDraft, OperationPlanService, PlanHash, PlanNotFound
from .profiles import (
    profile_action_mcp_tool,
    profile_mcp_tools,
    describe_profile_declarations,
    profile_names,
    validate_profile_result,
)
from .security import hash_token
from .shell_policy import describe_shell_policy, evaluate_shell_command


_COMMON_TOOLS = {"list_agents", "get_agent_capabilities"}
_LEGACY_TOOLS = _COMMON_TOOLS | profile_mcp_tools()
_PLAN_TOOLS = _COMMON_TOOLS | {"create_operation_plan", "get_operation_plan_status", "start_operation_plan"} | profile_mcp_tools(plan_only=True)


class GatewayMCP(FastMCP):
    @staticmethod
    def _allowed_tools() -> set[str]:
        token = get_access_token()
        if token is None:
            return set()
        if "gateway:plan" in token.scopes:
            return _PLAN_TOOLS
        if "gateway:legacy" in token.scopes:
            return _LEGACY_TOOLS
        return set()

    async def list_tools(self) -> list[Tool]:
        allowed = self._allowed_tools()
        return [tool for tool in await super().list_tools() if tool.name in allowed]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        if name not in self._allowed_tools():
            raise ToolError("此 MCP Key 的权限范围不允许调用该工具")
        return await super().call_tool(name, arguments)


class DatabaseTokenVerifier:
    def __init__(self, db: Database):
        self.db = db

    async def verify_token(self, token: str) -> AccessToken | None:
        key = self.db.verify_mcp_key(hash_token(token))
        if key is None:
            return None
        return AccessToken(
            token=token,
            client_id=key["key_id"],
            scopes=["gateway:use", f"gateway:{key['scope']}"],
            subject=key["user_id"],
            claims={"user_id": key["user_id"], "email": key["email"]},
        )


def _current_user_id() -> str:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        raise ToolError("MCP Access Key 无效")
    return access_token.subject


def _plan_response(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return action()
    except PlanNotFound as exc:
        raise ToolError("该计划不存在或不是由此 MCP Key 提交") from exc
    except (ValueError, ConflictError) as exc:
        raise ToolError(str(exc)) from exc


def _agent_summary(agent: dict[str, Any], hub: AgentHub) -> dict[str, Any]:
    result = {
        "id": agent["id"],
        "name": agent["name"],
        "description": agent["description"],
        "status": "online" if hub.is_online(agent["id"]) else "offline",
        "capabilities": profile_names(agent["profiles"]),
        "profile_declarations": agent["profile_declarations"],
        "features": agent["features"],
        "runtimes": agent["runtimes"],
        "requires_reenrollment": not agent["profile_declarations"],
        "version": agent["version"],
        "version_status": agent_version_status(agent["version"]),
        "last_seen_at": agent["last_seen_at"],
        **directory_fields(agent),
    }
    if "shell.v1" in agent["profiles"]:
        result["shell_policy"] = describe_shell_policy(agent["shell_policy_mode"])
    return result


def _shell_runtime(agent: dict[str, Any]) -> dict[str, Any] | None:
    for declaration in agent["profile_declarations"]:
        if declaration.get("id") == "shell.v1":
            runtime = declaration.get("runtime")
            return runtime if isinstance(runtime, dict) else None
    return None


def _owned_profile_action(
    db: Database,
    user_id: str,
    agent_id: str,
    profile_id: str,
    action: str,
    mcp_tool: str,
) -> dict[str, Any]:
    agent = db.get_owned_agent(user_id, agent_id)
    if agent is None:
        raise ToolError("Agent 不存在或不属于当前用户")
    if profile_id not in agent["profiles"]:
        raise ToolError(f"该 Agent 不支持 {profile_id}")
    try:
        declared_tool = profile_action_mcp_tool(profile_id, action)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if declared_tool != mcp_tool:
        raise ToolError(f"{profile_id}/{action} 未映射到 {mcp_tool}")
    return agent


async def _execute_profile_action(
    *,
    db: Database,
    hub: AgentHub,
    user_id: str,
    agent_id: str,
    profile_id: str,
    action: str,
    mcp_tool: str,
    params: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    agent = _owned_profile_action(db, user_id, agent_id, profile_id, action, mcp_tool)
    if profile_id == "shell.v1" and action == "exec":
        violation = evaluate_shell_command(
            mode=agent["shell_policy_mode"],
            runtime=_shell_runtime(agent),
            command=str(params.get("command", "")),
        )
        if violation is not None:
            db.record_device_alert(
                user_id, agent_id, "shell_policy", "旧版 Shell 请求被标准策略拒绝",
                "检查设备 Shell 策略；不要用直连请求绕过已设置的限制",
                basis={"rule": violation.rule_id}, action="shell.v1/exec", blocked=True,
            )
            raise ToolError(violation.tool_error())
    try:
        result = await hub.execute(agent_id, profile_id, action, params, timeout)
    except AgentExecutionStopped as exc:
        raise ToolError(STOP_EXPLANATION) from exc
    except (AgentOfflineError, AgentCommandError) as exc:
        raise ToolError(str(exc)) from exc
    try:
        return validate_profile_result(profile_id, action, result)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def create_mcp_server(db: Database, hub: AgentHub, settings: Settings, plans: OperationPlanService) -> FastMCP:
    parsed = urlparse(settings.public_url)
    allowed_hosts = [parsed.netloc, "127.0.0.1:*", "localhost:*"]
    allowed_origins = [settings.public_url, "http://127.0.0.1:*", "http://localhost:*"]
    server = GatewayMCP(
        "Remote Agent Gateway",
        instructions=(
            "管理并操作当前用户拥有的远程 Agent。先调用 list_agents 选择目标设备；"
            "调用任何 Agent 专属工具前，必须先对目标 agent_id 调用 get_agent_capabilities。"
            "计划型 Key 仅能创建、查询和启动自己提交的免审批计划，需要审批的计划交由登录的设备所有者处理。"
            "直连 Key 只能调用能力详情中列出 mcp_tool 的 Profile Action，网关会在调用时再次校验。"
            "workspace.v1 覆盖已有文件或 edit 前必须先读取文件版本。"
            "若目标 Agent 启用了标准 Shell 拦截策略，部分系统级命令会在转发前被拒绝。"
        ),
        token_verifier=DatabaseTokenVerifier(db),
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=f"{settings.public_url}/mcp/",
            required_scopes=["gateway:use"],
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
    )

    @server.tool(
        description=(
            "列出当前用户拥有的远程 Agent。可用 filters 按 platform、environment、tags（全部匹配）"
            "和 capability（capability_ids 中的 ID）筛选；筛选不改变任何操作权限。"
        )
    )
    async def list_agents(ctx: Context, filters: DeviceFilters | None = None) -> dict[str, Any]:
        del ctx
        user_id = _current_user_id()
        agents = db.list_agents(user_id)
        return {"agents": [_agent_summary(agent, hub) for agent in agents if filters is None or filters.matches(agent)]}

    @server.tool(
        description=(
            "查询指定 Agent 的标准能力、可用操作、限制及每个操作对应的固定 MCP 工具。"
            "调用任何 Agent 专属工具前必须先调用它确认能力和调用入口；"
            "不要仅根据 tools/list 推断某个 Agent 支持哪些能力。"
        )
    )
    async def get_agent_capabilities(agent_id: str, ctx: Context) -> dict[str, Any]:
        del ctx
        user_id = _current_user_id()
        agent = db.get_owned_agent(user_id, agent_id)
        if agent is None:
            raise ToolError("Agent 不存在或不属于当前用户")
        profiles = describe_profile_declarations(agent["profile_declarations"])
        allowed_tools = GatewayMCP._allowed_tools()
        for profile in profiles:
            for action in profile["actions"]:
                if action.get("mcp_tool") not in allowed_tools:
                    action.pop("mcp_tool", None)
        return {
            "agent": _agent_summary(agent, hub),
            "profiles": profiles,
            "gateway_limits": {
                "max_command_timeout_seconds": settings.max_command_timeout,
                "max_concurrent_requests_per_agent": MAX_CONCURRENT_REQUESTS_PER_AGENT,
            },
            "current_agent_release": {
                "agent_version": CURRENT_AGENT_VERSION,
                "core_version": CURRENT_CORE_VERSION,
                "protocol_version": AGENT_PROTOCOL_VERSION,
            },
        }

    @server.tool(
        description=(
            "在指定在线 Agent 上执行 Shell 命令，并返回 stdout、stderr 和 exit_code。"
            "调用前必须先对该 agent_id 调用 get_agent_capabilities；"
            "只有 Agent 声明了 shell.v1，且 shell.v1/exec 的 mcp_tool 明确为 remote_exec 时才能调用，"
            "网关会再次校验。"
            "command 必须符合 get_agent_capabilities 返回的 Shell 方言。"
            "该能力拥有 Agent 所在系统账号的完整 Shell 权限。"
            "目标 Agent 启用标准命令策略时，网关会在执行前拦截部分系统级高风险操作。"
        )
    )
    async def remote_exec(
        agent_id: str,
        command: str,
        ctx: Context,
        timeout: int = 300,
    ) -> dict[str, Any]:
        del ctx
        user_id = _current_user_id()
        if not command.strip():
            raise ToolError("command 不能为空")
        if timeout < 1 or timeout > settings.max_command_timeout:
            raise ToolError(f"timeout 必须在 1 到 {settings.max_command_timeout} 秒之间")
        return await _execute_profile_action(
            db=db,
            hub=hub,
            user_id=user_id,
            agent_id=agent_id,
            profile_id="shell.v1",
            action="exec",
            mcp_tool="remote_exec",
            params={"command": command, "timeout": timeout},
            timeout=timeout,
        )

    @server.tool(
        description=(
            "读取指定在线 Agent 工作目录内的 UTF-8 文本文件。"
            "调用前必须先对该 agent_id 调用 get_agent_capabilities；"
            "只有 Agent 声明 workspace.v1 且 workspace.v1/read 的 mcp_tool 为 remote_read_file 时才能调用。"
            "path 必须是工作目录下的相对路径，返回内容可能因行数或字节上限被截断，并包含 sha256。"
        )
    )
    async def remote_read_file(
        agent_id: str,
        path: str,
        ctx: Context,
        offset: int = 1,
        limit: int = 2000,
    ) -> dict[str, Any]:
        del ctx
        user_id = _current_user_id()
        return await _execute_profile_action(
            db=db,
            hub=hub,
            user_id=user_id,
            agent_id=agent_id,
            profile_id="workspace.v1",
            action="read",
            mcp_tool="remote_read_file",
            params={"path": path, "offset": offset, "limit": limit},
            timeout=min(settings.max_command_timeout, 60),
        )

    @server.tool(
        description=(
            "在指定在线 Agent 工作目录内整体写入 UTF-8 文本文件。"
            "调用前必须先对该 agent_id 调用 get_agent_capabilities；"
            "只有 Agent 声明 workspace.v1 且 workspace.v1/write 的 mcp_tool 为 remote_write_file 时才能调用。"
            "path 必须是工作目录下的相对路径。expected_sha256 必须显式提供："
            "传 null 时只允许新建，传最近一次读取的 SHA-256 时只允许覆盖该版本。"
        )
    )
    async def remote_write_file(
        agent_id: str,
        path: str,
        content: str,
        expected_sha256: str | None,
        ctx: Context,
    ) -> dict[str, Any]:
        del ctx
        user_id = _current_user_id()
        return await _execute_profile_action(
            db=db,
            hub=hub,
            user_id=user_id,
            agent_id=agent_id,
            profile_id="workspace.v1",
            action="write",
            mcp_tool="remote_write_file",
            params={
                "path": path,
                "content": content,
                "expected_sha256": expected_sha256,
            },
            timeout=min(settings.max_command_timeout, 60),
        )

    @server.tool(
        description=(
            "在指定在线 Agent 工作目录内精确编辑 UTF-8 文本文件。"
            "调用前必须先对该 agent_id 调用 get_agent_capabilities 和 remote_read_file；"
            "只有 Agent 声明 workspace.v1 且 workspace.v1/edit 的 mcp_tool 为 remote_edit_file 时才能调用。"
            "old_text 必须恰好匹配一次，expected_sha256 必须来自最近一次读取；冲突时不会覆盖文件。"
        )
    )
    async def remote_edit_file(
        agent_id: str,
        path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
        ctx: Context,
    ) -> dict[str, Any]:
        del ctx
        user_id = _current_user_id()
        return await _execute_profile_action(
            db=db,
            hub=hub,
            user_id=user_id,
            agent_id=agent_id,
            profile_id="workspace.v1",
            action="edit",
            mcp_tool="remote_edit_file",
            params={
                "path": path,
                "old_text": old_text,
                "new_text": new_text,
                "expected_sha256": expected_sha256,
            },
            timeout=min(settings.max_command_timeout, 60),
        )

    @server.tool(
        description=(
            "读取指定在线 Agent 的主机系统、架构、主机名和 Agent 运行时信息。"
            "这是 system_observe.v1 的固定只读 Action，不接受命令或探测参数。"
        )
    )
    async def remote_get_host_info(agent_id: str, ctx: Context) -> dict[str, Any]:
        del ctx
        return await _execute_profile_action(
            db=db,
            hub=hub,
            user_id=_current_user_id(),
            agent_id=agent_id,
            profile_id="system_observe.v1",
            action="get_host_info",
            mcp_tool="remote_get_host_info",
            params={},
            timeout=min(settings.max_command_timeout, 60),
        )

    @server.tool(
        description=(
            "读取指定在线 Agent 的 CPU、内存和工作目录所在磁盘状态。"
            "这是 system_observe.v1 的固定只读 Action，不接受命令或探测参数。"
        )
    )
    async def remote_get_resources(agent_id: str, ctx: Context) -> dict[str, Any]:
        del ctx
        return await _execute_profile_action(
            db=db,
            hub=hub,
            user_id=_current_user_id(),
            agent_id=agent_id,
            profile_id="system_observe.v1",
            action="get_resources",
            mcp_tool="remote_get_resources",
            params={},
            timeout=min(settings.max_command_timeout, 60),
        )

    @server.tool(
        description=(
            "读取指定在线 Agent 的有界进程摘要，不返回命令参数或环境变量。"
            "这是 system_observe.v1 的固定只读 Action，不接受命令或探测参数。"
        )
    )
    async def remote_get_processes(agent_id: str, ctx: Context) -> dict[str, Any]:
        del ctx
        return await _execute_profile_action(
            db=db,
            hub=hub,
            user_id=_current_user_id(),
            agent_id=agent_id,
            profile_id="system_observe.v1",
            action="get_processes",
            mcp_tool="remote_get_processes",
            params={},
            timeout=min(settings.max_command_timeout, 60),
        )

    @server.tool(
        description=(
            "读取指定在线 Agent 的网络接口和本地地址元数据，不执行远程探测。"
            "这是 system_observe.v1 的固定只读 Action，不接受命令或探测参数。"
        )
    )
    async def remote_get_network(agent_id: str, ctx: Context) -> dict[str, Any]:
        del ctx
        return await _execute_profile_action(
            db=db,
            hub=hub,
            user_id=_current_user_id(),
            agent_id=agent_id,
            profile_id="system_observe.v1",
            action="get_network",
            mcp_tool="remote_get_network",
            params={},
            timeout=min(settings.max_command_timeout, 60),
        )

    @server.tool(
        description=(
            "提交有明确目标和依赖关系的受控计划；仅接受已定义的动作与自有在线设备。"
            "此调用只保存计划，不执行动作。需要审批时请交给设备所有者。"
        )
    )
    async def create_operation_plan(plan: OperationPlanDraft) -> dict[str, Any]:
        user_id = _current_user_id()
        key_id = get_access_token().client_id
        created = _plan_response(lambda: plans.save(user_id, plan, key_id=key_id))
        return _plan_response(lambda: plans.delegated_status(user_id, created["id"], key_id))

    @server.tool(description="查看此 MCP Key 所提交计划的步骤、状态和最小化结果。")
    async def get_operation_plan_status(task_id: str) -> dict[str, Any]:
        user_id = _current_user_id()
        key_id = get_access_token().client_id
        return _plan_response(lambda: plans.delegated_status(user_id, task_id, key_id))

    @server.tool(description="按计划哈希启动此 MCP Key 所提交的免人工审批计划，每个版本只能启动一次。")
    async def start_operation_plan(task_id: str, plan_hash: PlanHash) -> dict[str, Any]:
        user_id = _current_user_id()
        key_id = get_access_token().client_id
        _plan_response(lambda: plans.start(user_id, task_id, plan_hash, key_id=key_id))
        return _plan_response(lambda: plans.delegated_status(user_id, task_id, key_id))

    return server
