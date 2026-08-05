from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .agent_hub import AgentCommandError, AgentHub, AgentOfflineError
from .config import Settings
from .database import Database
from .profiles import describe_profiles, profile_names
from .security import hash_token


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
            scopes=["gateway:use"],
            subject=key["user_id"],
            claims={"user_id": key["user_id"], "email": key["email"]},
        )


def _current_user_id() -> str:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        raise ToolError("MCP Access Key 无效")
    return access_token.subject


def _agent_summary(agent: dict[str, Any], hub: AgentHub) -> dict[str, Any]:
    return {
        "id": agent["id"],
        "name": agent["name"],
        "description": agent["description"],
        "status": "online" if hub.is_online(agent["id"]) else "offline",
        "capabilities": profile_names(agent["profiles"]),
        "last_seen_at": agent["last_seen_at"],
    }


def create_mcp_server(db: Database, hub: AgentHub, settings: Settings) -> FastMCP:
    parsed = urlparse(settings.public_url)
    allowed_hosts = [parsed.netloc, "127.0.0.1:*", "localhost:*"]
    allowed_origins = [settings.public_url, "http://127.0.0.1:*", "http://localhost:*"]
    server = FastMCP(
        "Remote Agent Gateway",
        instructions=(
            "管理并操作当前用户拥有的远程 Agent。先调用 list_agents 选择在线设备，"
            "需要了解设备能力时调用 get_agent_capabilities，再调用 remote_exec。"
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
        description="列出当前用户拥有的远程 Agent，包括用途、在线状态和可用能力。"
    )
    async def list_agents(ctx: Context) -> dict[str, Any]:
        del ctx
        user_id = _current_user_id()
        agents = db.list_agents(user_id)
        return {"agents": [_agent_summary(agent, hub) for agent in agents]}

    @server.tool(
        description="查询指定 Agent 的标准能力、可用操作及限制。调用远程操作前可用它确认能力。"
    )
    async def get_agent_capabilities(agent_id: str, ctx: Context) -> dict[str, Any]:
        del ctx
        user_id = _current_user_id()
        agent = db.get_owned_agent(user_id, agent_id)
        if agent is None:
            raise ToolError("Agent 不存在或不属于当前用户")
        return {
            "agent": _agent_summary(agent, hub),
            "profiles": describe_profiles(agent["profiles"]),
        }

    @server.tool(
        description=(
            "在指定在线 Agent 上执行 Shell 命令，并返回 stdout、stderr 和 exit_code。"
            "该能力拥有 Agent 所在系统账号的完整 Shell 权限。"
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
        agent = db.get_owned_agent(user_id, agent_id)
        if agent is None:
            raise ToolError("Agent 不存在或不属于当前用户")
        if "shell.v1" not in agent["profiles"]:
            raise ToolError("该 Agent 不支持远程 Shell")
        if not command.strip():
            raise ToolError("command 不能为空")
        if timeout < 1 or timeout > settings.max_command_timeout:
            raise ToolError(f"timeout 必须在 1 到 {settings.max_command_timeout} 秒之间")
        try:
            return await hub.execute(
                agent_id,
                "shell.v1",
                "exec",
                {"command": command, "timeout": timeout},
                timeout,
            )
        except (AgentOfflineError, AgentCommandError) as exc:
            raise ToolError(str(exc)) from exc

    return server

