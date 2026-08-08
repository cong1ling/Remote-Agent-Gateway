# Remote Agent Gateway

> 一个 MCP 入口，连接你散落在任何地方的设备能力。

[![Version](https://img.shields.io/badge/version-0.2.1-2f6feb)](https://github.com/zshs000/Remote-Agent-Gateway/releases)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-0f766e)](LICENSE)

Remote Agent Gateway 是一个可自行部署的远程 Agent MCP 网关。Claude、WorkBuddy、OpenClaw 或其他 MCP Client 只连接一个网关；远程电脑、服务器和 CI 运行轻量 Agent，并主动通过 WebSocket 连接回来。

远端不需要公网 IP、入站端口或 SSH，也不需要每台设备分别运行 MCP Server。

> **Technical Preview**：当前版本已经跑通完整闭环，适合自行部署和体验。项目尚未承诺生产级稳定性，后续版本仍会快速演进，但已经发布的同名 Profile 不会静默改变契约语义。

```text
AI / MCP Client
      | MCP Streamable HTTP
      v
Remote Agent Gateway
  认证、用户隔离、能力目录、请求路由
      ^
      | WS / WSS（Agent 主动出站）
      |
Remote Agent Core
      |
Profile -> Action -> 本地系统能力
```

**一句话概括：MCP 只运行一次，能力可以运行在任何地方。**

## 为什么做这个项目

让每台电脑、手机或边缘设备直接暴露 MCP，意味着每台设备都要处理 MCP 协议、TLS、秘钥、工具描述、客户端连接和升级。设备越多，维护成本和攻击面越大；对资源有限的设备也不现实。

Remote Agent Gateway 把复杂部分集中在网关：

- MCP Client 始终连接少量、稳定的固定工具。
- 网关负责认证、所有权、能力发现、契约校验和请求路由。
- Agent 只实现它实际加载的标准 Profile，不运行模型，也不是 MCP Server。
- Agent 主动连接网关，因此可以位于 NAT、家庭网络、CI Runner 或其他无入站网络的环境。

这不是任意 MCP Server 聚合器，也不是另一个内置模型、记忆和聊天渠道的本地 AI Agent。它只解决一件事：让上游 AI Runtime 能够发现并调用远端真实设备的标准能力。

## 当前能做什么

- 注册登录、用户隔离以及独立的 MCP Access Key
- 一次性 Agent Enrollment Token 和长期设备凭据
- Agent 工作台：选择 Profile，组装并下载独立 Python Agent ZIP
- Agent 心跳、掉线检测、自动重连、稳定设备 ID 和版本状态
- 固定 Profile Catalog、参数校验、结果校验和 MCP 工具映射
- 每台 Agent 最多同时执行 5 个请求，第 6 个立即返回忙碌
- 每台 Shell Agent 可在网关配置命令拦截策略，默认关闭
- 原生 Web 管理端与 SQLite 持久化
- Docker Compose 自行部署

当前提供两个标准 Profile：

| Profile | Action | 固定 MCP 工具 | 用途 |
|---|---|---|---|
| `shell.v1` | `exec` | `remote_exec` | 使用 Agent 声明的系统 Shell 执行非交互命令 |
| `workspace.v1` | `read` | `remote_read_file` | 读取工作目录内的 UTF-8 文本 |
| `workspace.v1` | `write` | `remote_write_file` | 创建文件或按 SHA-256 安全覆盖已读取版本 |
| `workspace.v1` | `edit` | `remote_edit_file` | 按 SHA-256 精确替换唯一文本片段 |

## 五分钟跑通

需要 Python 3.11 或更高版本。

```powershell
git clone https://github.com/zshs000/Remote-Agent-Gateway.git
cd Remote-Agent-Gateway
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m gateway
```

macOS / Linux 将最后两条命令中的 `.\.venv\Scripts\python.exe` 替换为 `.venv/bin/python`。

打开 <http://127.0.0.1:8000>，然后：

1. 注册一个账号。
2. 在“Agent 工作台”填写设备名称和用途，选择 Profile 并组装 Agent。
3. 在远程设备解压 ZIP，安装 `requirements.txt`，运行页面给出的一次性启动命令。
4. 回到网关确认 Agent 在线，并创建 MCP Key。
5. 把 MCP Endpoint 和 Key 配置到 Claude、WorkBuddy 或其他 MCP Client。

首次注册示例：

```powershell
python run.py --gateway "ws://127.0.0.1:8000/ws/agent" --token "enroll_xxx"
```

注册成功后，Agent 会把独立设备凭据原子写入 `.agent/config.json`。Enrollment Token 不会写进 ZIP，且只能消费一次；后续启动只需要：

```powershell
python run.py
```

工作台是普通用户的推荐入口。“手动接入（高级）”只签发 Enrollment Token，适用于已经持有兼容 Agent 程序或使用自动化部署的用户，两条路径不需要同时使用。

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

AI 看到的 MCP 工具始终是固定集合，不会为每台 Agent 动态生成一套工具：

```text
list_agents()
get_agent_capabilities(agent_id)
remote_exec(agent_id, command, timeout)
remote_read_file(agent_id, path, offset, limit)
remote_write_file(agent_id, path, content, expected_sha256)
remote_edit_file(agent_id, path, old_text, new_text, expected_sha256)
```

标准调用顺序是：

```text
发现 Agent
    -> 查询 Agent 的 Profile 和详细契约
    -> 读取 Action 的 mcp_tool 映射
    -> 调用对应的固定 MCP 工具
```

即使用户连接 100 台 Agent，模型看到的仍然只是少量稳定工具；设备和能力详情按需发现。

## Profile 行为

### `shell.v1`

`shell.v1` 表示“按照 Agent 声明的 Shell 方言执行命令”，不是一套跨平台统一命令语言。

| 系统 | 检测顺序 | 声明方言 |
|---|---|---|
| Windows | `pwsh` -> `powershell.exe` -> `cmd.exe` | `powershell` 或 `cmd` |
| Linux / macOS | `/bin/sh`，缺失时从 PATH 查找 `sh` | `posix-sh` |

- 返回 `stdout`、`stderr` 和 `exit_code`，stdout/stderr 各最多 2 MiB。
- 子进程 stdin 固定为 `DEVNULL`，不支持编辑器、REPL、密码提示、SSH 交互、TUI 或 PTY。
- 超时、取消和断线会终止整个命令进程树。
- `--workdir` 只是 Shell 初始目录，不是 Shell 权限沙箱。
- 网关可按 Agent 配置命令拦截策略：默认 `off`；`standard` 会在转发前拦截磁盘管理、系统电源、权限提升、系统凭据、持久化入口和关闭安全防护等系统级操作。
- 命中标准策略时，命令不会发送到远端 Agent；MCP 返回 `command_blocked` 和命中的规则 ID。
- 标准策略不拦截普通文件删除、项目修改、依赖安装、构建或测试。它只检查收到的命令文本，不是操作系统沙箱，也不能替代独立 OS 用户、容器或虚拟机。

### `workspace.v1`

`workspace.v1` 是受 `--workdir` 路径约束的结构化 UTF-8 文本通道，不是上传下载系统或操作系统沙箱。

- 只接受 `/` 分隔的相对路径，拒绝绝对路径、`..` 和逃出工作目录的符号链接。
- 单文件最多 8 MiB；单次读取最多 2000 行或 50 KiB；单次写入最多 2 MiB。
- `read` 返回完整文件 SHA-256，并用 `truncated`、`lines` 和 `total_lines` 声明读取范围。
- `write.expected_sha256=null` 只允许创建不存在的文件；传 SHA-256 只允许覆盖完全匹配的已读取版本。
- `edit` 必须携带最近一次 `read` 的 SHA-256，且 `old_text` 必须精确匹配一次。
- 当前不提供无条件 `force` 覆盖，也不提供目录列举 Action。

远程编码推荐同时组装 `shell.v1 + workspace.v1`：Shell 负责目录发现、搜索、Git、构建和测试，Workspace 负责可靠的结构化文本操作。

完整契约和新增能力的约束见 [Profile 开发规范](PROFILE_DEVELOPMENT.md)。

## 安全边界

Remote Agent Gateway 提供认证、用户所有权、Token 哈希、Profile 契约、路径约束和乐观并发控制，但不会把完整 Shell 伪装成安全沙箱。

- `remote_exec` 拥有 Agent 所在系统账号的完整权限。
- Shell 命令策略由 Agent 所属用户在网页“能力”详情中的 `shell.v1` 项配置，默认关闭；策略在网关转发前执行，远端 Agent 无需重启或修改配置文件。
- 只应在自己拥有或明确获准管理的设备上运行 Agent。
- 真正的权限隔离应依赖独立 OS 用户、容器、虚拟机或平台沙箱。
- MCP Access Key、Enrollment Token 和长期设备凭据是三种不同凭据，不得混用。
- `.agent/`、`.mcp.json`、数据库和任何 Secret 都不应提交到 Git。
- 网络断线可能出现“远端已执行但响应丢失”，重要写操作后应重新读取验证。

## Agent 配置与 CI

同一个 config 代表同一个 Agent 实例。已有 config 时再次传入 Enrollment Token 会被拒绝；注册另一台 Agent 应使用新的 `--config` 路径：

```powershell
python run.py --gateway "wss://gateway.example.com/ws/agent" `
  --token "enroll_xxx" `
  --config "D:\RemoteAgentData\local-windows\config.json"
```

只读目录、临时容器和 CI Runner 可以通过 `--config-env NAME` 从环境变量读取 Base64 JSON 设备凭据。Agent 读取后会立即从自身进程环境中移除该变量，避免 Profile 子进程继承。

```powershell
python run.py --config-env REMOTE_AGENT_CONFIG_B64 --workdir .
```

工作台 ZIP 包含手动触发的 GitHub Actions 示例。CI Agent 拥有 Job 本身的系统权限，不要在同一个 Agent Step 中注入无关生产秘钥，也不要通过不受信任的 `push` 或 `pull_request` 自动开放远程 Shell。

## 自行部署

```powershell
$env:GATEWAY_PUBLIC_URL = "https://gateway.example.com"
$env:GATEWAY_SECURE_COOKIES = "true"
docker compose up -d --build
```

生产环境使用 Caddy、Nginx 或云负载均衡器终止 TLS，再反向代理到网关 `8000` 端口。WebSocket 和 MCP Streamable HTTP 使用同一个域名，[deploy/Caddyfile.example](deploy/Caddyfile.example) 提供最小配置。

当前必须使用单个 Uvicorn worker，因为在线 Agent 连接和待处理请求保存在当前进程内。SQLite 位于 `GATEWAY_DATA_DIR`，生产部署必须挂载持久卷。可用环境变量见 [.env.example](.env.example)。

## 版本与兼容性

当前发布组成：

| 组件 | 版本 |
|---|---|
| Remote Agent | `0.2.1` |
| Agent Core | `5` |
| WebSocket Protocol | `2` |

Agent 握手会上报发布版本、Core 版本、协议版本和唯一 `build_id`。网关会显示“当前版本”“需要升级”“协议不兼容”“高于网关版本”或“未知版本”。当前不提供自动更新器。

同一 Profile ID 必须保持参数、结果、错误和安全语义一致。正式发布后的破坏性能力变更会使用新的 Profile 主版本或明确迁移路径，不会让同一个 Profile ID 静默表达两套行为。

## 未来方向

项目会继续沿着“网关统一、远端极简”的方向演进：

- Go、Rust、C 以及平台原生 Remote Agent 实现
- 手机与移动端的受限设备能力
- 树莓派、摄像头、小车和其他边缘设备 Profile
- 独立于普通 MCP 调用的大数据与实时媒体通道
- 多实例网关所需的连接协调和共享消息总线

这些是路线方向，不代表当前版本已经提供对应能力。新增 Profile 前必须先证明现有 Profile 无法诚实表达需求，项目不会为想象中的硬件提前扩张 Core。

## 开发

```powershell
python -m unittest discover -q
python -m compileall -q gateway remote_agent agent.py tests
node --check gateway/static/app.js
```

仓库结构与执行规范见 [AGENTS.md](AGENTS.md)，Profile/Action 扩展前必须阅读 [PROFILE_DEVELOPMENT.md](PROFILE_DEVELOPMENT.md)。

当前非目标包括：网关文件托管、Agent 分享、团队 RBAC、用户上传插件、持久 PTY、内置 LLM/聊天机器人以及没有真实 Handler 的空硬件 Profile。

## License

Remote Agent Gateway is licensed under the [Apache License 2.0](LICENSE).
