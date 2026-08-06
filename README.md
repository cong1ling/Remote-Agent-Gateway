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

### `shell.v1` 契约

`shell.v1` 接收符合远程设备所声明 Shell 方言的命令字符串。官方 Python Agent 在启动时检测一次可用 Shell，随后在本次运行期间固定使用该实现：

| 系统 | 检测顺序 | 声明方言 |
|---|---|---|
| Windows | `pwsh` → `powershell.exe` → `cmd.exe` | `powershell` 或 `cmd` |
| Linux / macOS | `/bin/sh` | `posix-sh` |

Agent 会在注册与重连握手中上报操作系统、方言和可执行文件名；`list_agents` 与 `get_agent_capabilities` 会返回这些信息。命令返回 `stdout`、`stderr` 和 `exit_code`，stdout 与 stderr 各最多保留 2 MiB，超时或取消会终止本次命令的进程树。

同一台 Agent 当前最多同时处理 5 个请求。网关与官方 Agent 都会拒绝第 6 个并立即返回“Agent 当前忙碌”，不会把请求放入隐藏队列；该固定上限将在后续改为可配置的每 Agent 策略。

Agent WebSocket 协议当前为 v2，不兼容早期未携带结构化 Profile 运行时信息的脚本；请重新从工作台组装 Agent。

### Agent 版本信息

每个 Agent 在握手时上报以下版本信息，网关会持久化并显示版本状态：

- `agent_version`：Agent 发布版本，例如 `0.1.0`
- `core_version`：Core 数据与行为版本
- `protocol_version`：Agent WebSocket 协议版本
- `build_id`：工作台每次组装生成的唯一构建 ID；源码运行使用 `source-dev`

网页和 MCP 会将 Agent 标记为“当前版本”“需要升级”“协议不兼容”“高于网关版本”或“未知版本”。第一版只提供版本可见性，不实现 Agent 自动更新。

## 本地启动

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:GATEWAY_PUBLIC_URL = "http://127.0.0.1:8000"
.\.venv\Scripts\python.exe -m gateway
```

打开 <http://127.0.0.1:8000>，注册账号后按以下顺序操作：

### 两种 Agent 接入方式

- **Agent 工作台（推荐）**：面向普通用户，一次完成 Profile 选择、Python Agent ZIP 组装和 Enrollment Token 签发。
- **手动接入（高级）**：只签发 Enrollment Token，不生成 Agent 文件。仅适用于已经持有兼容 Agent 程序，或使用自动化部署的用户。

两条路径不需要同时使用。首次接入通常只使用 Agent 工作台。

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

Agent 在发送首次注册 Token 前会确认设备凭据位置可以创建并原子写入；检查失败时 Token 不会被消费。只读目录、系统服务或容器应通过 `--config` 指向可写且持久化的位置：

```powershell
python run.py --gateway "wss://gateway.example.com/ws/agent" --token "enroll_xxx" --config "D:\RemoteAgentData\local-windows\config.json"
```

同一个 config 代表同一个 Agent 实例。已有 config 时再次传入 Enrollment Token 会被拒绝；启动现有 Agent 应移除 `--token`，注册另一台 Agent 应使用新的 `--config` 路径。容器中的 config 路径必须挂载持久卷。

### CI/CD 与环境变量凭据

GitHub Actions、临时容器等无持久磁盘环境可以从 CI Secret 注入已经注册好的设备凭据。先为 CI 创建一台专用 Agent，正常注册一次并停止进程，再把 `.agent/config.json` 编码为单行 Base64：

```powershell
python -c "import base64,pathlib; print(base64.b64encode(pathlib.Path('.agent/config.json').read_bytes()).decode())"
```

将输出保存为 CI Secret `REMOTE_AGENT_CONFIG_B64`，启动时指定环境变量名称：

```powershell
python run.py --config-env REMOTE_AGENT_CONFIG_B64 --workdir .
```

Agent 读取后会立即从自身进程环境中移除该变量，避免 `shell.v1` 启动的子进程读取设备凭据。环境变量模式是只读的，不能与 Enrollment Token 同时使用。工作台生成的 ZIP 包含 `github-actions.yml.example` 和保护 `.agent/` 的 `.gitignore`；GitHub Actions 模板仅允许手动启动、使用只读仓库权限、禁止持久化 Checkout 凭据、限制单实例并设置运行超时。

CI Runner 在线期间拥有该任务本身的系统权限。不要在同一个 Agent Step 中注入生产部署秘钥，也不要通过 `push` 或 `pull_request` 自动触发远程 Shell 会话。

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

`get_agent_capabilities` 返回的每个 Profile Action 都会携带对应的固定 `mcp_tool` 名称。例如 `shell.v1/exec` 明确映射到 `remote_exec`，AI 无需根据描述猜测调用入口。Profile 只描述标准能力，MCP 工具仍由网关固定发布，不会按 Agent 动态注册。

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
python -m compileall -q gateway remote_agent agent.py tests
```

当前范围刻意不包含上传下载、Agent 分享、团队 RBAC、路径级 Shell 权限、硬件 Profile 和多进程扩展。
