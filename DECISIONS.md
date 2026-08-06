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

## 2026-08-06：首次注册前验证设备凭据存储

- **决策**：设备凭据的启动状态机由 Agent Core 统一管理。无 config 且有 Enrollment Token 时，Core 必须在联网前验证目标目录可创建并完成同目录原子写入；有 config 且再次传入 Token 时拒绝启动。默认仍使用组装包内 `.agent/config.json`，服务、只读目录和容器可通过 `--config` 指向外部持久化路径。
- **原因**：网关会在返回独立设备凭据前消费一次性 Token。如果远程端随后才发现目录不可写，会产生无法重连的孤儿 Agent；已有 config 被新 Token 覆盖还会意外替换原有稳定身份。
- **替代方案**：始终写入用户主目录；让网关保存明文设备凭据供再次下载；实现带持久化确认的两阶段注册协议；继续在注册成功后直接尝试写文件。
- **取舍**：写入预检无法覆盖预检后磁盘瞬时故障，但能在不增加协议状态和网关秘密存储的前提下拦截常见权限、只读文件系统和路径错误。高级部署需要显式规划持久化 config 路径。
- **影响**：Agent Core 升级为 v2，Agent 发布版本升级为 0.1.1，WebSocket 协议仍为 v2。旧 Agent 可以继续连接但会显示需要升级；新工作台包自动包含该保护。

## 2026-08-06：Profile Action 显式映射固定 MCP 工具

- **决策**：网关维护的每个 Profile Action 都声明一个固定 `mcp_tool` 名称。该映射通过 Profile Catalog、Agent 能力详情和 MCP `get_agent_capabilities` 返回，并在网页协议窗口展示工具名与调用形式；测试保证所有映射名称真实存在于 MCP `tools/list`。
- **原因**：Profile 描述远程能力契约，MCP 工具是本地 AI 的调用入口。只返回 `shell.v1/exec` 而不声明 `remote_exec` 会依赖模型从两份描述中自行推断，未来 Profile 和工具增多后容易选错入口。
- **替代方案**：继续依赖工具描述推断；按 Agent 动态注册 MCP 工具；暴露万能 `remote_call(profile, action, params)`；让一个 Profile 只能对应一个工具。
- **取舍**：Action 契约增加一个很小的映射字段，并需要保持注册表与 MCP 工具同步；换取确定的发现链路和强类型工具，同时避免动态工具膨胀及传统 MCP 聚合网关形态。
- **影响**：当前 `shell.v1/exec` 映射到 `remote_exec`。未来一个 Profile 可以包含多个 Action，并分别映射到多个固定、语义明确的 MCP 工具。

## 2026-08-06：每台 Agent 暂时最多并发执行五个请求

- **决策**：第一版在网关 Hub 和官方 Agent Core 两侧都硬编码每台 Agent 最多同时处理 5 个请求。第 6 个请求立即返回明确的忙碌错误，不进入等待队列；共享常量旁保留 TODO，后续改为可配置的每 Agent 策略。
- **原因**：MCP 客户端可以并行调用工具，同一用户也可能同时连接多个 AI 客户端。没有上限时，误调用、重试或泄露的 Key 可以在一台远程设备上瞬间创建大量进程并耗尽资源。
- **替代方案**：完全不限制；只在网关限制；只在 Agent 限制；使用信号量无限排队；立即实现用户级、Profile 级和资源感知调度。
- **取舍**：固定值 5 简单且足以保护 MVP，但无法针对高性能服务器和低功耗设备分别调优。立即拒绝可能要求 AI 稍后重试，但比隐藏排队后在 MCP 客户端超时更可预测。
- **影响**：Agent 发布版本升级为 0.1.2、Core 升级为 v3，WebSocket 协议仍为 v2。不同 Agent 的并发计数相互独立，任务完成、超时、取消或断线后释放名额。

## 2026-08-06：PowerShell 对象输出在进程退出前完成默认格式化

- **决策**：`shell.v1` 使用 PowerShell 时，将用户命令所在脚本块显式接入 `Out-Default`，等待 PowerShell 的默认格式化管线把对象写入 stdout 后，再按已捕获的执行状态退出进程。Agent 发布版本升级为 0.1.3，Core 与 WebSocket 协议版本保持不变。
- **原因**：PowerShell 的 `Get-Item`、`Get-Location`、`Select-Object` 等命令返回对象，宿主会延迟将其格式化为终端文本。原包装脚本紧接着执行 `exit`，可能在格式化完成前结束进程，造成命令成功且副作用已发生，但 stdout 为空；字符串和原生命令输出则不一定触发该问题。
- **替代方案**：要求 MCP 客户端为每条命令手动追加 `Out-String`；删除显式退出并放弃准确传递原生命令退出码；把 PowerShell 对象强制序列化为 JSON；在网关端猜测并修补空输出。
- **取舍**：保持普通 PowerShell 终端的默认文本表现以及现有 `stdout`、`stderr`、`exit_code` 契约，也保留原生命令退出码；对象仍按 PowerShell 默认视图展示，而不是变成新的结构化返回协议。
- **影响**：Windows PowerShell 与 PowerShell 7 的对象输出不再因包装脚本提前退出而消失。`Set-Content` 等本来没有输出的命令仍会返回空 stdout，调用方在需要确认副作用时仍应显式检查目标状态。

## 2026-08-06：CI 与只读环境通过一次性消费的环境变量加载设备凭据

- **决策**：Agent Core 增加只读环境变量凭据源，`--config-env NAME` 从指定变量读取 Base64 JSON 设备凭据。变量在解析时立即从 Agent 进程环境中删除，避免后续 Profile 子进程继承；该模式禁止 Enrollment Token 和凭据写回。工作台 ZIP 同时提供 GitHub Actions 手动会话模板与保护 `.agent/` 的 `.gitignore`。Agent 发布版本升级为 0.1.4、Core 升级为 v4，WebSocket 协议仍为 v2。
- **原因**：GitHub Actions 等托管 Runner 每次启动都是临时文件系统，无法依赖注册后生成的本地 config；把已有设备身份保存为 CI Secret 可以让同一 Agent ID 跨任务重连。若仅把 Secret 留在进程环境中，`shell.v1` 执行 `env` 或 `Get-ChildItem Env:` 时会暴露 Agent 自身凭据，因此必须在 Handler 创建子进程前消费并删除。
- **替代方案**：每次流水线使用新的 Enrollment Token；把 config 放进 Git 仓库、Artifact 或 Cache；增加可长期复用的第三种 CI Token；让网关预创建并导出设备凭据；为 GitHub 单独实现 OAuth App 与 Runner 管理系统。
- **取舍**：不新增网关协议、秘钥类型和 CI 管理页面，文件配置与环境变量配置共享同一设备身份校验；代价是用户仍需先完成一次正常注册并手动把 config 写入 CI Secret。模板只面向手动、限时调试会话，不承诺常驻 Runner 或流水线编排。
- **影响**：同一 Python Agent 可以运行在 GitHub Actions、Docker、Kubernetes Secret 和其他只读环境；Runner 会按实际系统重新声明 `shell.v1` 方言。设备凭据变量不会进入远程 Shell，但 Agent 仍拥有 CI Job 自身可访问的文件和其他环境信息，使用者必须避免向该 Step 注入无关生产秘钥。

## 2026-08-06：`shell.v1` 显式关闭子进程标准输入

- **决策**：官方 Python `shell.v1` 启动每个 Shell 进程时将 stdin 连接到 `DEVNULL`，不再继承 Agent 进程的本地终端输入。Agent 发布版本升级为 0.1.5，Core 与 WebSocket 协议版本保持不变。
- **原因**：`shell.v1/exec` 的契约只有一次性 `command` 输入，没有远程 stdin 或交互终端。未显式配置 stdin 时，操作系统可能让子进程继承运行 Agent 的本地控制台，导致等待输入的远程命令读取本地用户在 Agent 窗口键入的内容，或者因部署环境不同而随机阻塞、收到 EOF。
- **替代方案**：保留操作系统默认继承行为；把本地终端输入转发为远程 stdin；立即增加可交互 PTY；为 `exec` 增加一次性 stdin 参数。
- **取舍**：需要交互输入的程序现在会立即收到 EOF，而不能借用本地键盘；换取跨前台终端、后台服务、容器和 CI 一致的非交互语义，并消除本地输入被远程命令意外消费的通道。
- **影响**：普通命令、文件操作、stdout/stderr 捕获和退出码不变。编辑器、REPL、密码提示和 TUI 仍不属于 `shell.v1`；未来若有明确需求，应单独设计受限的一次性 stdin 或 `terminal.v1`。
