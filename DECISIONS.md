# 决策记录

本文件记录项目实施期间由执行者自主作出的实质性决策，供用户集中审阅。用户已经明确指定的要求不重复记为自主决策，普通编码步骤和过程流水账也不记录。

每项决策使用以下格式：

## YYYY-MM-DD：决策标题

- **决策**：最终采用的方案。
- **原因**：作出该选择的依据。
- **替代方案**：曾考虑但未采用的主要方案。
- **取舍**：该方案获得了什么，又放弃了什么。
- **影响**：涉及的模块、行为、兼容性或后续工作。

---

## 2026-08-06：采用单体 Python 网关与无构建链管理端

- **决策**：第一版使用 FastAPI 承载 Web API、Agent WebSocket 和管理页面，使用 Python 标准库 `sqlite3` 持久化数据；管理端采用静态 HTML、CSS 和 JavaScript 单页界面，不引入 Node 构建链。
- **原因**：当前目标是尽快形成可自行部署的完整闭环。单进程架构能共享在线 Agent 连接状态，标准库 SQLite 足以支撑第一版用户规模，无构建链前端可显著降低安装和部署成本。
- **替代方案**：Django 全栈；FastAPI + SQLAlchemy + Vue/Vite；前后端独立部署。
- **取舍**：获得更少依赖和更简单的部署，暂时放弃复杂 ORM、前端组件生态和多进程水平扩展。
- **影响**：网关第一版以单个 Uvicorn worker 运行；未来需要水平扩展时，应把连接路由和任务状态迁移到共享协调层。

## 2026-08-06：MCP 使用官方 SDK 的 Streamable HTTP 传输

- **决策**：网关作为唯一 MCP Server，使用官方 Python MCP SDK 提供 Streamable HTTP 接口，并使用独立 MCP Access Key 进行 Bearer 鉴权。
- **原因**：这与 Claude、WorkBuddy 等远程 MCP 客户端的接入模型一致，也避免自行实现 MCP JSON-RPC 和会话协议。
- **替代方案**：本地 stdio 转接器；自行实现 MCP 协议；SSE 旧传输。
- **取舍**：获得标准协议兼容性，但客户端必须支持远程 Streamable HTTP 和自定义 Authorization Header；必要时以后再补本地 stdio 兼容器。
- **影响**：MCP Access Key 与网页会话、Agent 注册令牌完全分离，工具通过鉴权上下文确定用户身份。

## 2026-08-06：Agent 使用 WebSocket，TLS 由反向代理终止

- **决策**：Agent 与网关之间使用版本化 JSON WebSocket 协议；本地开发使用 `ws`，生产环境通过 Caddy、Nginx 等反向代理提供 `wss`。
- **原因**：WebSocket 满足主动出站、双向下发、心跳和未来流式输出需求；将证书管理交给成熟反向代理可保持 Python 服务简单。
- **替代方案**：原始 TCP + TLS；网关进程直接管理 TLS 证书；gRPC 双向流。
- **取舍**：获得良好的网络兼容性和简单证书运维，增加一层生产部署组件。
- **影响**：应用协议与传输层分离，后续替换 Go/Rust 网关或其他传输时可以保留 Profile 语义。

## 2026-08-06：第一版安全模型采用所有权隔离和高熵凭据

- **决策**：用户密码使用 `scrypt` 加盐哈希；网页使用可撤销会话；MCP Key、一次性 Enrollment Token 和 Agent 设备凭据均为独立高熵随机值，数据库只保存哈希。Agent 注册后自动换取独立设备凭据。
- **原因**：不同凭据代表不同主体和生命周期，分离后可以单独撤销并准确审计；高熵令牌使用快速哈希保存仍可抵御数据库明文泄漏。
- **替代方案**：一个用户共用单一秘钥；JWT 承载所有身份；保存可恢复的明文令牌。
- **取舍**：凭据类型更多，但网页只需让用户直接管理 MCP Key 和 Enrollment Token，设备凭据由 Agent 自动管理。
- **影响**：第一版不做 Agent 分享与 RBAC；每个 Agent 只对 owner 可见，owner 对其声明的 `shell.v1` 拥有完整调用权。

## 2026-08-06：保留旧脚本名作为兼容入口

- **决策**：将旧 PoC 的 `local_server.py` 和 `remote_client.py` 替换为薄兼容入口，分别启动新网关和新 Agent；所有真实实现只保留在 `gateway` 包和 `agent.py` 中。
- **原因**：旧文件名可能已被本地配置或早期使用命令引用，直接删除会制造不必要的迁移故障，同时保留两套实现又会造成协议和安全模型分裂。
- **替代方案**：直接删除旧脚本；继续维护旧 TCP PoC；把旧代码移动到 legacy 目录。
- **取舍**：保留向后可发现性，仅增加两个极小入口文件，不继续支持旧明文 TCP 协议。
- **影响**：旧 `.mcp.json` 仍需由用户更新为新的远程 MCP URL 和 Access Key；旧上传下载工具不再存在。

## 2026-08-06：生产 TLS 与单进程部署边界

- **决策**：应用自身提供 HTTP/WS，生产环境由反向代理统一升级为 HTTPS/WSS；第一版明确只运行一个 Uvicorn worker。
- **原因**：反向代理能可靠处理证书续期和 WebSocket，单进程则确保 Agent 在线连接和 MCP 请求路由位于同一内存空间。
- **替代方案**：应用直接加载证书；多 worker 加 Redis 路由；独立连接网关服务。
- **取舍**：部署需要一个反向代理，且单实例吞吐存在上限，但大幅降低第一版分布式协调复杂度。
- **影响**：README、Docker Compose 和 Caddy 示例均按此边界提供；未来水平扩展需要引入连接粘性或共享消息总线。

## 2026-08-06：Shell 超时和取消终止整个进程组

- **决策**：Agent 启动每个 Shell 请求时创建独立进程组；发生超时、取消或连接中断时终止整个进程树，而不是只终止 Shell 父进程。
- **原因**：命令通常由 Shell 再启动 Python、训练器等子进程。只杀父进程会让子进程继续消耗算力并占用输出管道，违背取消和超时语义。
- **替代方案**：只调用 `process.kill()`；不提供可靠取消；要求命令自行处理信号。
- **取舍**：Windows 需要调用系统自带的 `taskkill`，Unix 需要独立进程组，但能保证第一版行为符合用户预期。
- **影响**：`shell.v1` 的超时与网关取消会清理整棵任务进程树；远程命令不应依赖脱离当前任务长期驻留的后台子进程。

## 2026-08-06：命令输出按通道限制为 2 MiB

- **决策**：Agent 持续排空 stdout 和 stderr，但每个通道只保留前 2 MiB，超出部分丢弃并附加明确的截断标记。
- **原因**：第一版尚不支持流式输出，无上限缓冲会让高输出命令耗尽 Agent 内存，停止读取又可能填满操作系统管道并阻塞远程进程。
- **替代方案**：无限制调用 `communicate()`；达到上限后停止读取；第一版直接实现完整流式传输。
- **取舍**：保证固定内存上限和任务可完成性，超长日志无法完整返回；用户可以改用远程命令把完整日志写入文件。
- **影响**：`shell.v1` 的 stdout、stderr 返回可能包含 `[output truncated by remote agent]`；未来引入流式 Profile 时可以提供完整增量输出。

## 2026-08-06：依赖版本采用兼容范围而非完全锁定

- **决策**：Python 包声明 FastAPI、MCP SDK、Uvicorn 和 WebSocket 客户端的兼容主版本范围，并要求现代 FastAPI 与 MCP SDK；构建系统显式包含 `setuptools` 和 `wheel`。
- **原因**：项目既要能从源码安装，也要能构建 wheel。MCP SDK 使用较新的 Starlette，旧 FastAPI 与其不兼容；完全锁死补丁版本则会阻止用户获得兼容范围内的安全修复。
- **替代方案**：固定当前环境的每个精确版本；不提供 Python 包；维护单独 requirements 文件。
- **取舍**：安装更灵活、包构建自包含，但未来依赖发布不兼容更新时仍需调整上限。
- **影响**：部署应通过 `pip install .` 或 Dockerfile 安装声明依赖，不应复用一个包含冲突版 FastAPI 的全局环境。

## 2026-08-06：远程 Agent 拆分为固定 Core 与 Profile Handler

- **决策**：将原本集中在 `agent.py` 的实现迁入独立 `remote_agent` 包；Core 只负责凭据、Manifest、连接、重连、协议解析和请求路由，具体能力由实现统一接口的 Profile Handler 提供。根目录 `agent.py` 仅保留兼容入口。
- **原因**：远程部署物需要能够独立移植和按需组装，新增硬件或其他标准能力时也不应修改连接核心。Handler 注册表还能确保 Agent 只声明实际加载的 Profile。
- **替代方案**：继续维护单文件 Agent；通过条件分支把所有能力写在 Runtime 中；立即开放 Python 第三方插件 API。
- **取舍**：文件数量增加，但职责和构建边界清晰；当前 Handler 仍在同一进程中运行，暂不承担第三方插件隔离成本。
- **影响**：Python Agent 默认由 `manifest.json` 选择 `shell.v1`；未来构建器可以复制固定 Core、选择 Profile 模块并生成新的 Manifest，而网关协议不需要改变。

## 2026-08-06：Agent 工作台只组装可信 Profile，注册 Token 与下载包分离

- **决策**：网关提供用户级 Agent 工作台。用户从管理员维护的 Profile Catalog 中选择能力，网关复制固定 Python Core 和对应 Handler、生成 Manifest 并输出 ZIP；一次性 Enrollment Token 只在创建响应和启动命令中展示，不写入 ZIP。
- **原因**：用户需要可直接搬运的最小远程目录，但普通用户不能通过组装页面引入任意执行代码。Token 与包分离后，ZIP 可以安全地先行传输，只有持有一次性命令的人能够把设备绑定到账号。
- **替代方案**：下载始终包含全部 Profile；允许用户上传 Python 插件；把 Enrollment Token 固化进配置文件；只提供源码目录而不提供网页构建。
- **取舍**：当前 Profile Catalog 仍由代码维护，新增 Profile 需要管理员发布新版本；换取构建结果可审计、协议定义一致和更小的供应链攻击面。
- **影响**：首个构建目标仅为 Python，下载包包含 `run.py`、固定 Core、选中 Profile、Manifest 和 `requirements.txt`。组装记录及下载均按用户隔离，并与 Enrollment Token 共享过期时间。

## 2026-08-06：`shell.v1` 显式声明自动检测到的 Shell 方言

- **决策**：官方 Python Agent 启动时只检测一次 Shell，并在运行期间固定使用该执行器。Windows 依次选择 `pwsh`、`powershell.exe`、`cmd.exe`，Linux 与 macOS 使用 `/bin/sh`；Agent WebSocket 协议升级为 v2，Profile 声明必须携带操作系统、方言和可执行文件名。网关保存并向网页及 MCP 暴露该声明，同时校验 `shell.v1/exec` 的返回结构。
- **原因**：Shell 命令语言并不跨平台统一，单独声明 `shell.v1` 无法让 AI 判断应该生成 PowerShell、CMD 还是 POSIX Shell 命令。显式选择执行器还能避免 Python 根据宿主环境隐式决定 Shell，确保同一 Agent 运行期间语义稳定。
- **替代方案**：继续使用 `create_subprocess_shell` 的系统默认行为；拆分为 `powershell.v1`、`cmd.v1` 和 `posix-shell.v1`；要求所有系统安装统一 Shell；对旧握手格式保留兼容分支。
- **取舍**：Profile Catalog 仍只需要维护一个 `shell.v1`，但命令本身仍需符合设备声明的方言。早期 Agent 脚本无法继续连接，需要重新组装或注册；换取无双协议分支的清晰契约。
- **影响**：`list_agents`、`get_agent_capabilities`、Agent 管理页面和 Profile Catalog 会展示运行时方言及输入输出契约；Windows 执行不再默认落入 `cmd.exe`，PowerShell 可用时优先使用 PowerShell。现有账号、MCP Key 和 Enrollment Token 数据不受影响。

## 2026-08-06：心跳检测在 Hub 内立即注销失活连接

- **决策**：`AgentHub.heartbeat` 在关闭超时或发送失败的 WebSocket 后，立即从在线连接表移除对应 Agent 并失败其挂起请求，不再返回一个依赖调用方二次处理的 stale ID 列表。
- **原因**：仅关闭 WebSocket 会把最终清理依赖于连接处理协程稍后收到断开事件；关闭握手延迟或异常时，Agent 可能继续被短暂判定为在线，挂起请求也无法及时结束。
- **替代方案**：由应用心跳循环消费 stale 列表；继续依赖 WebSocket 处理器的 `finally` 清理；增加单独的离线清理任务。
- **取舍**：WebSocket 处理器随后再次注销时会成为无操作，但 Hub 自身的在线状态和请求生命周期变得即时且自洽。
- **影响**：心跳超时后网页和 MCP 会更快显示 Agent 离线，发往该 Agent 的待处理命令立即收到离线错误。

## 2026-08-06：Agent 握手携带发布版本与唯一构建 ID

- **决策**：Agent 在 v2 握手中必须上报 `agent_version`、`core_version`、`protocol_version` 和 `build_id`。工作台使用 Bundle UUID 作为每次 ZIP 的唯一构建 ID，源码 Manifest 使用 `source-dev`；网关在 Agent 记录中持久化版本对象，并计算当前、需要升级、协议不兼容、高于网关或未知状态。
- **原因**：协议升级后旧 Agent 离线暴露了版本不可见的问题。只有 Profile 和在线状态不足以判断设备运行的是哪套 Core、来自哪个构建，以及是否需要重新部署。
- **替代方案**：只显示 WebSocket 协议版本；依靠文件 Hash 识别 Agent；立即实现远程自动更新器；把版本仅写进日志而不持久化。
- **取舍**：Manifest、握手和数据库增加少量字段，发布版本仍需由代码维护；换取网页、MCP 和故障排查中的明确版本依据，同时保持 Agent 不具备自更新和远程下载代码的能力。
- **影响**：现有数据库启动时新增 `version_json` 列且旧记录显示未知版本；新工作台包可以追溯到具体 Bundle。后续实现升级提示或发布流程时可以复用该数据，而无需改变 Agent 权限模型。

## 2026-08-06：运行时信息附着到具体 Profile 展示

- **决策**：Agent 卡片不再把 `Windows · PowerShell` 作为独立的全局运行时徽标，而是将运行时和对应能力组合展示，例如 `shell.v1 | Windows · PowerShell`。没有运行时声明的旧 Profile 仍显示普通能力标签。
- **原因**：PowerShell 是 `shell.v1` 的执行方言，不代表整个 Agent。未来同时装配多个 Profile 时，独立运行时徽标无法说明它属于哪个能力，容易让用户误以为所有能力都通过 PowerShell 实现。
- **替代方案**：维持独立的 Shell 运行时徽标；把操作系统和所有运行时拆成两组全局标签；等第二个 Profile 发布后再调整。
- **取舍**：当前卡片标签会稍宽，但能力与运行环境的关系变得明确，并且可以自然扩展到多个 Profile；设备级平台信息暂不新增数据库字段。
- **影响**：仅调整概览和远程 Agent 卡片的前端展示，不改变 Agent 协议、Profile 声明、数据库或 MCP 返回结构。
