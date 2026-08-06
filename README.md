# Remote Agent Gateway

Remote Agent Gateway 是一个可自行部署的 MCP 网关。远程 Agent 只需主动连接公网网关，不需要开放入站端口或配置内网穿透；Claude、WorkBuddy 等 MCP 客户端通过统一入口列出设备、查询标准能力并执行远程命令。

```text
Claude / WorkBuddy
        | MCP Streamable HTTP
        v
Python Gateway ---- Web UI / SQLite
        ^
        | WebSocket (生产环境使用 WSS)
Remote Agent
```

远程 Agent 不是 MCP Server。它只实现网关定义的标准 Profile；第一版仅包含 `shell.v1`。

## 当前能力

- 用户注册、登录与所有权隔离
- 可分别创建、撤销的 MCP Access Key
- 短期有效且只能使用一次的 Agent Enrollment Token
- Agent 首次注册后自动保存独立设备凭据
- Agent 心跳、离线检测、断线重连和稳定 ID
- Agent 名称、用途描述、在线状态和 Profile 管理
- Agent 工作台：从可信 Profile Catalog 组装并下载独立 Python Agent ZIP
- MCP 工具：`list_agents`、`get_agent_capabilities`、`remote_exec`
- Web 管理端
- SQLite 持久化，数据库仅保存令牌哈希

`remote_exec` 拥有 Agent 所在系统账号的完整 Shell 权限。只应在自己拥有或明确获准管理的设备上运行 Agent。

## 本地启动

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:GATEWAY_PUBLIC_URL = "http://127.0.0.1:8000"
.\.venv\Scripts\python.exe -m gateway
```

打开 <http://127.0.0.1:8000>，注册账号后按以下顺序操作：

1. 在“Agent 工作台”中填写名称和用途，选择 Profile 并组装。
2. 解压自动下载的 ZIP，在远程环境执行 `python -m pip install -r requirements.txt`。
3. 运行页面单独展示的一次性 Agent 启动命令。
4. 在“MCP Keys”中为 Claude 或 WorkBuddy 创建独立 Key。
5. 使用页面展示的 MCP Endpoint 和 Key 配置客户端。

组装包的远程 Agent 首次注册示例：

```powershell
python run.py --gateway "ws://127.0.0.1:8000/ws/agent" --token "enroll_xxx"
```

组装包中的名称、描述和 Profile 来自 `remote_agent/manifest.json`，注册 Token 不会写入 ZIP。注册成功后，设备凭据保存在当前组装包的 `.agent/config.json`，后续只需执行：

```powershell
python run.py
```

源码仓库仍保留兼容入口 `python agent.py`。真正的远程实现位于：

```text
remote_agent/
├── core/       # 连接、凭据、Manifest、协议路由
└── profiles/   # 标准能力 Handler
```

## MCP 配置

不同客户端的配置文件位置不同，核心配置如下：

```json
{
  "mcpServers": {
    "remote-gateway": {
      "url": "http://127.0.0.1:8000/mcp/",
      "headers": {
        "Authorization": "Bearer mcp_xxx"
      }
    }
  }
}
```

连接后可以按以下顺序调用：

```text
list_agents()
get_agent_capabilities(agent_id)
remote_exec(agent_id, command, timeout)
```

## 自行部署

Docker Compose：

```powershell
$env:GATEWAY_PUBLIC_URL = "https://gateway.example.com"
$env:GATEWAY_SECURE_COOKIES = "true"
docker compose up -d --build
```

生产环境应使用 Caddy、Nginx 或云负载均衡器终止 TLS，再反向代理到网关的 `8000` 端口。WebSocket 和 MCP Streamable HTTP 都通过同一域名工作。`deploy/Caddyfile.example` 提供了最小 Caddy 配置。

第一版必须使用单个 Uvicorn worker，因为在线 Agent 连接和待处理请求保存在当前进程内。SQLite 数据位于 `GATEWAY_DATA_DIR`，部署时必须挂载持久卷。

可用环境变量见 [.env.example](.env.example)。最重要的是：

| 变量 | 说明 |
|---|---|
| `GATEWAY_PUBLIC_URL` | 用户和 Agent 实际访问的公网地址 |
| `GATEWAY_DATA_DIR` | SQLite 和运行数据目录 |
| `GATEWAY_SECURE_COOKIES` | HTTPS 生产环境设为 `true` |
| `GATEWAY_MAX_COMMAND_TIMEOUT` | MCP 允许的最大命令超时秒数 |

## 开发验证

```powershell
python -m unittest discover -v
python -m compileall -q gateway agent.py tests
```

当前范围刻意不包含上传下载、Agent 分享、团队 RBAC、路径级 Shell 权限、硬件 Profile 和多进程扩展。
