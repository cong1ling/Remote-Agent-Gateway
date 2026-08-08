# Remote Agent Gateway 项目执行规范

本文件面向在当前仓库中工作的 AI、自动化开发者和贡献者。开始修改前，先理解项目定位、目录职责和不可破坏的协议边界，再直接在现有架构上完成实现与验证。

## 项目介绍

Remote Agent Gateway 是一个可自行部署的远程 Agent MCP 网关。Claude、WorkBuddy、OpenClaw 或其他 MCP Client 只连接网关；远程电脑、服务器、CI 和未来的边缘设备运行轻量 Agent，并主动通过 WebSocket 连接网关，无需开放入站端口、SSH 或单独运行 MCP Server。

```text
AI / MCP Client
      ↓ MCP Streamable HTTP
Gateway：认证、用户隔离、能力目录、请求路由、结果校验
      ↓ WS / WSS（Agent 主动出站）
Remote Agent Core
      ↓
Profile → Action → 本地系统能力
```

项目的核心价值不是聚合任意 MCP Server，也不是再造一个包含模型、记忆和聊天渠道的本地 AI Agent。网关负责远程设备与能力平面；上游 Agent Runtime 负责模型和用户交互；远端 Agent 只负责执行实际加载的标准能力。

当前 Python 版本已经完成可用闭环：用户注册登录、MCP Key、一次性 Enrollment Token、Agent 工作台、设备凭据、WebSocket 心跳重连、固定 Profile、MCP 调用和网页管理。当前发布版本以 `remote_agent/core/version.py` 和 `pyproject.toml` 为准。

## 当前能力

### 标准 Profile

- `shell.v1`
  - Action：`exec`
  - MCP：`remote_exec`
  - 使用 Agent 启动时检测并声明的系统 Shell 方言执行命令。
- `workspace.v1`
  - Action：`read`、`write`、`edit`
  - MCP：`remote_read_file`、`remote_write_file`、`remote_edit_file`
  - 在 `--workdir` 下提供受路径约束的 UTF-8 文本操作。

### 固定 MCP 工具

- `list_agents`
- `get_agent_capabilities`
- `remote_exec`
- `remote_read_file`
- `remote_write_file`
- `remote_edit_file`

网关不按 Agent 动态生成工具。AI 的标准调用顺序是：

```text
list_agents
→ get_agent_capabilities(agent_id)
→ 读取 Profile Action 的 mcp_tool
→ 调用对应固定 MCP 工具
```

## 仓库目录

```text
.
├── gateway/                         # Python 网关
│   ├── app.py                       # FastAPI 应用组装、生命周期与心跳任务
│   ├── web_api.py                   # 登录、Key、Token、Agent、Builder 等网页 API
│   ├── mcp_api.py                   # 固定 MCP 工具及所有权/能力路由
│   ├── agent_api.py                 # Agent WebSocket 接入与握手流程
│   ├── agent_hub.py                 # 在线连接、并发、挂起请求和断线清理
│   ├── agent_protocol.py            # Agent v2 协议与版本校验
│   ├── agent_builder.py             # Python Agent ZIP 组装
│   ├── profiles.py                  # 网关 Profile Catalog、契约和结果校验
│   ├── database.py                  # SQLite 数据访问
│   ├── security.py                  # 密码和 Token 生成/哈希
│   └── static/                      # 原生 HTML/CSS/JavaScript 管理端
├── remote_agent/                    # 官方 Python Remote Agent
│   ├── cli.py                       # 启动参数、Manifest、config 和 Handler 装配
│   ├── manifest.json                # 源码运行 Manifest
│   ├── core/                        # 凭据、版本、连接、心跳、重连和请求路由
│   └── profiles/
│       ├── base.py                  # ProfileContext、ProfileHandler、ActionHandler
│       ├── registry.py              # 静态 Profile Factory Registry
│       ├── common/                  # 跨 Action 复用的底层实现
│       ├── shell_v1/                # Shell Profile 与 exec Action
│       └── workspace_v1/            # Workspace Profile 与 read/write/edit Action
├── tests/                           # 关键协议、安全与回归测试
├── deploy/                          # 反向代理示例
├── compose.yaml / Dockerfile        # 容器部署
├── agent.py                         # 源码兼容启动入口
├── README.md                        # 面向用户的介绍、运行和部署说明
├── PROFILE_DEVELOPMENT.md           # Profile/Action 扩展的权威设计规范
├── DECISIONS.md                     # 自主产品与架构决策记录
└── AGENTS.md                        # 当前文件，开发执行规范
```

## 不可破坏的架构边界

1. 网关是唯一 MCP 出口；Remote Agent 不是 MCP Server，也不运行 LLM。
2. Agent 只声明本地实际加载且网关 Registry 已定义的标准 Profile。
3. Profile 是固定协议契约，Handler 是具体实现；不同语言可以实现同一契约。
4. 每个对 AI 开放的 Action 必须映射到真实存在的固定 `mcp_tool`。
5. 不使用万能 `remote_call(profile, action, params)` 替代语义明确的强类型工具。
6. Profile 通过源码、Registry 和 Builder 静态组合，不是用户运行时插件系统。
7. 普通用户不能上传 Handler、动态注册工具、下载执行插件或建立插件市场。
8. Core 只处理连接与协议生命周期；具体系统能力必须位于 Profile/Action。
9. 网关 Profile Catalog 是 AI 可见契约的来源；Agent Registry 只维护可执行 Factory。
10. `v0.2.1` 是首个公开技术预览和契约基线；不为它之前的草案维护双协议和兼容分支。此后的破坏性 Profile 变更必须升级主版本 ID、协议版本或提供明确迁移路径。
11. 不把聊天机器人、模型 Runtime、Memory、Skill 或多 Agent 编排塞入 Gateway。OpenClaw 等属于可接入的上游，而不是需要重写的功能。
12. 硬件、移动端和边缘设备属于未来 Profile/多语言 Agent 方向，不应在没有稳定需求和真实 Handler 时提前扩张 Core。

## 认证与所有权

- MCP Access Key、Agent Enrollment Token 和长期设备凭据是三种不同凭据，不得混用。
- Enrollment Token 短期有效且只能用于首次注册；注册成功后由 Agent 保存独立设备凭据。
- 数据库只保存 Token 哈希，不保存可再次读取的明文 Secret。
- Agent 只能由所属用户通过 MCP 和网页访问；`description` 只描述设备用途，不参与权限判断，也不是 Prompt。
- 已有 config 与新 Enrollment Token 同时出现时必须拒绝启动，避免覆盖设备身份。
- Agent 首次注册前必须验证 config 目录可创建并可原子写入，避免 Token 消费后无法保存设备凭据。
- `.mcp.json`、`.agent/`、数据库、日志、构建产物和所有 Secret 不得提交。

## Agent WebSocket 与版本协议

- 当前 Agent WebSocket 协议为 v2。
- 握手中的 `profiles` 必须是结构化声明，至少包含 Profile ID 和 runtime 对象；不接受早期字符串数组格式。
- Agent 必须上报 `agent_version`、`core_version`、`protocol_version` 和 `build_id`。
- 首次注册保存 Profile ID；重连时 ID 集合必须一致，runtime 可以根据当前环境更新。
- 网关持久化版本信息并计算当前、需要升级、协议不兼容、高于网关或未知状态。
- 每台 Agent 在网关和官方 Agent 两侧最多同时执行 5 个请求；第 6 个立即返回忙碌，不排队。相关常量旁必须保留未来策略可配置的 TODO。
- 心跳失活、连接断开、取消和超时必须清理 Hub 挂起请求及相关本地进程。

## 兼容性政策

“尽量兼容”按契约层次执行，不等于永远接受旧实现：

- **传输兼容**：WebSocket/MCP 协议版本相同且消息语义未变时，旧 Agent 可以继续连接。
- **Profile 兼容**：同一 Profile ID 必须保持参数、结果、错误和安全语义一致。只改代码实现而不改变对外行为属于兼容；改变同一输入的含义属于破坏性变更。
- **实现版本兼容**：Registry 可以声明 `minimum_agent_version`，用于阻止旧 Handler 执行新契约。检查必须发生在 Token 消费和 Hub 注册之前。
- **可兼容变更**：新增可选元数据、放宽结果解析、修复不改变契约的实现错误，可以在原版本上演进；不得把新增必填字段或新增必需 Action 伪装成兼容变更。
- **破坏性变更**：正式发布后必须使用新的 Profile 主版本 ID、协议版本或明确迁移路径。不得让同一个 Profile ID 在不同 Agent 版本上静默代表两套语义。
- **当前基线**：`workspace.v1/write` 的安全语义修正在 `v0.2.1` 首次公开预发布前完成，因此使用最低版本检查拒绝更早草案；从 `v0.2.1` 起，同名 Profile 受公开契约约束。
- **用户实现**：不要求源码逐字节一致；只要实际 Handler 遵守同一契约即可。无法遵守契约的实现不能声明该 Profile。

## `shell.v1` 约束

- `shell.v1` 表示按 Agent 声明的 Shell 方言执行命令，不是一套跨平台统一命令语言。
- Windows 检测顺序：`pwsh` → `powershell.exe` → `cmd.exe`。
- Linux/macOS 优先 `/bin/sh`，缺失时从 PATH 查找 `sh`；仍只承诺 `posix-sh`。
- 启动时只检测一次，进程生命周期内不切换执行器。
- runtime 必须声明 `os`、`dialect` 和 `executable`。
- `remote_exec` 返回 `stdout`、`stderr` 和 `exit_code`；stdout/stderr 各最多 2 MiB。
- 子进程 stdin 固定为 `DEVNULL`。编辑器、REPL、密码提示、SSH 交互、TUI 和 PTY 不属于 `shell.v1`。
- PowerShell 对象输出必须完成默认格式化，不能因为包装脚本提前退出而丢失 stdout。
- 超时、取消和断线必须终止整个命令进程树。
- `--workdir` 只是 Shell 初始目录，不是 Shell 权限沙箱。Shell 拥有 Agent 系统账号的完整权限。

## `workspace.v1` 约束

`workspace.v1` 是受工作目录约束的结构化 UTF-8 文本通道，不是上传下载、分布式文件系统或操作系统沙箱。

- 路径必须是 `--workdir` 下使用 `/` 分隔的相对路径。
- 拒绝绝对路径、驱动器路径、`..` 和解析后逃出工作目录的符号链接。
- 首版仅支持 UTF-8 文本；保留 UTF-8 BOM 和原有 CRLF/LF 风格。
- 单文件最多 8 MiB，单次读取输出最多 50 KiB/2000 行，单次写入最多 2 MiB。
- `read` 返回整个文件的 SHA-256，并通过 `truncated`、`lines` 和 `total_lines` 声明读取范围。
- `write.expected_sha256` 必须显式提供：
  - `null`：只允许创建不存在的文件，已存在则 `conflict`。
  - SHA-256：只允许覆盖内容版本完全匹配的现有文件。
  - 缺少字段、存在性不符或版本不符均拒绝；不提供 `force` 无条件覆盖。
- 网关必须拒绝 Agent 0.2.0 及更早版本声明 `workspace.v1`；兼容检查必须发生在 Enrollment Token 消费和 Hub 注册之前。
- `edit` 必须携带最近一次 `read` 返回的 SHA-256，且 `old_text` 必须精确匹配一次；返回 diff、新 SHA 和首个修改行。
- write/edit 使用同目录临时文件和原子替换，避免半写；网络断线仍可能出现“已执行但响应丢失”，调用方应重新 read 验证。
- 当前不为 `workspace.v1` 增加 `list`。远程编码推荐组合 `shell.v1 + workspace.v1`：Shell 负责目录发现、搜索、Git、构建和测试，Workspace 负责可靠内容操作。workspace-only 只承诺已知路径操作。
- 同时加载 `shell.v1` 时，Workspace 路径边界不能限制 Shell；真正隔离依赖 OS 用户、容器或沙箱。

## Profile 与 Action 扩展

扩展前必须完整阅读 `PROFILE_DEVELOPMENT.md`。新增 Profile 的准入问题是：

> 现有的 ______ 无法表达 ______，因为 ______；新 Profile 的独立契约是 ______。

- 操作系统、Shell 方言、参数默认值、便利别名或用户脚本差异通常不构成新 Profile。
- Profile 内所有 Action 必须由声明该 Profile 的 Agent 完整实现；当前不支持 Action 子集声明。
- 新增 Action 时同步完成：网关 Catalog、固定 MCP 工具、远端 ActionHandler、Profile 装配、Builder 打包、参数/结果校验和关键测试。
- 只有真正跨 Action 的底层实现才能进入 `profiles/common/`；不要为了“插件化”制造抽象层。
- 不因为未来可能支持摄像头、手机、小车或 ESP32 就提前创建空 Profile。

## 工作方式

1. 不使用 Skills 中规定的 TDD、Git Worktree、功能分支、复杂计划文档或强制审查流程。
2. 直接在当前 `main` 分支工作，不创建其他分支或 Worktree。
3. 后端继续使用 Python；优先遵循现有 FastAPI、SQLite、Profile/Action 和原生前端结构。
4. 架构应清晰、职责分明，但避免为未来假设过度抽象。
5. 不要求测试先行。鉴权、所有权、路径隔离、协议路由、并发、凭据、安全写入和容易回归的逻辑必须有必要测试。
6. 自主完成合理的产品、架构、后端和前端决策；非关键不确定项按项目目标和工程常识继续推进，不停下来反复询问。
7. 不停留在方案或半成品阶段。实现、验证、修复并完成用户要求的闭环。
8. 保留用户已有文件和修改，不进行无关重构、批量格式化或破坏性操作。
9. 除 README、部署说明、Profile 规范和真正必要的设计说明外，不随意新增文档、计划文件或过程流水账。
10. 以实际可运行效果为完成标准，不以文件数量或形式流程为完成标准。

## 决策记录

所有由执行者自主作出的产品、架构、协议、安全、数据模型、依赖、前端或部署决策，都必须及时追加到根目录 `DECISIONS.md`。

每条至少包含：

- 日期与决策
- 原因
- 考虑过的替代方案
- 主要取舍
- 影响范围

普通编码步骤、测试输出、修复过程和无意义流水账不要写入 `DECISIONS.md`。单纯修复明显 Bug、对齐既有决策或修改文案通常不需要新增决策。

## 前端原则

- 当前前端是 `gateway/static/` 下的原生 HTML/CSS/JavaScript，与 Python 网关同源部署。没有明确收益时不要引入 Vue、React、Node 构建链或组件库。
- 页面保持简单、克制、实用，优先服务注册登录、Key、Agent 工作台、Agent 状态与能力查看。
- 必须处理加载、空数据、错误、成功反馈和移动端布局。
- Agent 卡片只显示能力数量入口；具体 Profile、runtime、Action/MCP 映射按需在弹窗中展示，不要重新把所有标签平铺回卡片。
- 协议弹窗采用固定头部、独立滚动内容和固定底部操作区；从 Agent 能力弹窗进入时保留返回路径。
- 修改 `app.js` 或 `styles.css` 后同步更新 `index.html` 的静态资源版本参数，避免浏览器继续使用旧缓存。
- 前端交互修改至少运行 JavaScript 语法检查；涉及布局时应尽可能用真实浏览器和目标视口验证。

## 验证要求

根据改动风险选择验证范围，完成前至少执行相关检查：

```powershell
python -m unittest discover -q
node --check gateway/static/app.js
git diff --check
git status --short --branch
```

- 协议、Builder、Profile 或 Agent Core 变更必须验证真实的 Gateway → Agent → MCP 链路，而不只依赖单元测试。
- Agent 安全修正需要验证工作台新 ZIP 包包含新实现；已经运行的旧 Agent 不会因网关代码变化自动更新。
- MCP Schema 变更需要检查实际 `tools/list`，确保必填、nullable 和工具描述与远端 Handler 一致。
- 前端缓存、弹窗和响应式布局问题应通过实际 HTTP 静态资源和浏览器渲染验证。
- 不得在未运行验证时声称“已修复”“测试通过”或“闭环完成”。

## Git 与提交

- 每完成一个小而完整、能够独立说明的功能，可以创建一次提交。
- 提交信息使用 Conventional Commits 前缀：`feat:`、`fix:`、`refactor:`、`test:`、`docs:` 或 `chore:`。
- 提交信息简洁、明确，描述结果而不是过程。
- 推送前确认工作树只包含本次相关修改，并运行 `git diff --check` 和必要测试。
- 不使用 `git reset --hard`、破坏性 checkout 或其他会丢失用户修改的操作。
- 不提交 `.mcp.json`、Token、设备 config、SQLite、日志、缓存、下载 ZIP、截图和运行产物。

## 当前非目标

- 上传下载和网关文件托管
- Agent 共享、团队 RBAC 和复杂审计
- 路径级 Shell 权限或把 `--workdir` 伪装成 Shell 沙箱
- 用户上传插件、自定义 Handler、插件市场和运行时插件下载
- Agent 二进制 Hash 强校验、代码签名和远程证明
- 自动更新器
- 持久 PTY、交互式终端和实时媒体通道
- 在 Gateway 内实现 LLM、聊天机器人或 OpenClaw 类 Agent Runtime
- 尚无真实需求和实现的硬件 Profile

## 完成标准

修改完成意味着：代码遵守上述边界，相关测试通过，必要的真实链路已验证，文档与契约没有互相冲突，Secret 与运行产物未进入 Git，并且用户可以按预期使用最终功能。
