# 项目执行规范

请在当前仓库中完成该项目的第一版可用 MVP，并遵循以下工作规范。

## 工作方式

1. 不使用 Skills 中规定的流程，包括 TDD、Git Worktree、功能分支、复杂规划文档和强制代码审查流程。
2. 直接在当前 `main` 分支工作，不创建其他分支或 Worktree。
3. 后端使用 Python 实现，架构应清晰、优美、职责分明，避免为了抽象而抽象。
4. 不要求测试先行。只在安全、鉴权、权限隔离、协议路由等关键功能，或容易回归的逻辑上编写必要测试。
5. 除 README、部署说明和真正必要的设计说明外，不随意创建文档、计划文件或过程记录。
6. 自主完成所有合理的产品、架构、后端和前端技术决策。遇到不确定事项时，根据项目目标、现有代码和工程常识选择最合适的方案并继续推进，不因非关键问题停下来询问用户。
7. 不要停留在方案或半成品阶段。持续完成实现、验证和修复，直到 MVP 闭环可用。
8. 每完成一个小而完整、能够独立说明的功能后，可以创建一次 Git 提交。提交信息应简洁明确，并必须使用 Conventional Commits 前缀，例如 `feat:`、`fix:`、`refactor:`、`test:`、`docs:` 或 `chore:`。
9. 保留用户已有文件和修改，不进行无关重构或破坏性操作。
10. 以实际可运行效果为完成标准，不以文件数量、页面数量或形式上的流程为标准。
11. 所有由执行者自主作出的产品、架构、协议、安全、数据模型、依赖、前端和部署决策，都必须及时追加到根目录的 `DECISIONS.md`。每条记录至少包含日期、决策、原因、考虑过的替代方案、主要取舍和影响范围，供用户后续集中审阅。不要用该文档记录普通编码步骤或无意义的过程流水账。

## 前端原则

1. 前端保持简单、克制、实用，首先服务于注册登录、秘钥管理、Agent 管理和状态查看等核心工作流。
2. 前端技术方案由执行者自主决定，可以使用 Vue 或其他适合当前项目的方案，不需要就技术选型询问用户。
3. 优先选择开发和部署成本低、与 Python 网关集成自然的实现，不为展示效果引入不必要的复杂度。
4. 必须完成必要的加载、空数据、错误、成功反馈和移动端适配，不能只实现静态页面。

## 项目目标

实现一个可自行部署的远程 Agent MCP 网关平台。用户注册后，可以创建 MCP Access Key 和一次性 Agent Enrollment Token；远程 Agent 主动连接网关并注册到该用户账户；Claude、WorkBuddy 等客户端使用 MCP Access Key 连接网关，列出自己的 Agent、查询能力并执行远程 Shell 命令。

## 第一版必须完成

- Python 网关服务
- SQLite 数据存储
- 用户注册、登录和用户隔离
- 多个可命名、可撤销和可轮换的 MCP Access Key
- 短期有效、一次性使用的 Agent Enrollment Token
- Agent 首次注册后获得独立设备凭据，并在重启后自动重连
- Agent 的稳定 ID、名称、描述、Profile、在线状态和最后在线时间
- WebSocket 长连接、认证、心跳、断线检测和自动重连
- Agent 工作台：选择网关维护的标准 Profile，组装并下载独立 Python Agent ZIP；普通用户优先使用该入口
- 手动接入（高级）：仅签发 Enrollment Token，不生成 Agent 文件，只适用于已经持有兼容 Agent 程序或使用自动化部署的用户
- `shell.v1` 标准能力及可查询的完整输入、输出、限制和运行时 Shell 方言
- 固定 MCP 工具：
  - `list_agents`
  - `get_agent_capabilities`
  - `remote_exec`
  - 必要时增加 `get_task` 和 `cancel_task`
- 一个克制、实用的网页管理界面
- 本地开发使用 WS，生产环境支持通过反向代理使用 WSS
- 必要的安全校验、错误处理、关键测试、运行说明和部署配置
- 合理的 `.gitignore`，不得提交 `.mcp.json`、秘钥、数据库、缓存和运行产物

## 核心设计约束

- 远程 Agent 是极简执行器，不是 MCP Server。
- 网关是唯一 MCP 出口。
- Agent 只按网关定义的协议声明标准 Profile，不能动态注册任意 MCP 工具。
- `shell.v1` 是网关预定义的标准能力，远程端必须真正实现对应处理器。
- Profile 的名称、说明和操作定义由网关维护。
- Profile 定义是固定协议契约，不要求所有 Agent 使用逐字节相同的实现。用户可以修改自己设备上的实现逻辑，但修改后若不再遵守契约，由该 Agent 自行承担连接失败、执行失败或结果被网关拒绝的后果。
- 普通用户不能上传、动态下载或在运行时注册自定义 Profile/Handler。自行部署的网关管理员可以通过修改网关与 Agent 源码并发布新版本扩展标准 Profile，这属于部署级扩展，不属于用户动态 MCP 聚合。
- 第一版不使用文件 Hash、代码签名或远程证明来强制 Agent 实现一致；网关通过协议版本、Profile 白名单、参数校验、结果校验和所有权隔离建立边界。
- Agent 的 `description` 仅描述设备用途，不参与权限判断，也不是系统提示词。
- MCP Access Key 与 Agent Enrollment Token 必须分离。
- Agent Enrollment Token 仅用于首次注册，不作为长期设备凭据。
- 第一版权限采用所有权隔离：Agent 仅对所属用户可见，该用户拥有 Agent 的完整 `shell.v1` 权限。
- 不实现上传下载。网关不拥有或管理用户文件。
- 不把项目扩展成传统 MCP 聚合网关。

## Agent 与 Profile 当前协议

- 远程实现固定拆分为 `remote_agent/core/` 与 `remote_agent/profiles/`。Core 负责凭据、Manifest、WebSocket、心跳、重连和协议路由；Profile Handler 负责具体能力执行。
- Agent WebSocket 协议当前为 v2。握手中的 `profiles` 必须是结构化声明，至少包含 Profile ID 及该实现的 runtime 信息；不兼容早期仅发送字符串数组的旧 Agent。
- 项目尚未正式发布，不为旧 Agent 草案维护双协议或兼容分支。协议发生破坏性修正时，应给出明确升级错误，并要求重新组装或注册 Agent；不得为兼容未发布草案破坏当前架构。
- Agent 只能声明本地实际加载了 Handler、且网关 Profile Registry 已定义的能力。首次注册保存 Profile ID；重连时 ID 必须一致，运行时信息可以随当前环境更新。
- `shell.v1` 表示“使用 Agent 启动时检测并声明的系统 Shell 方言执行命令字符串”，而不是一套跨平台统一的命令语言。
- 官方 Python `shell.v1` 在启动时只检测一次 Shell，并在该进程生命周期内固定使用：
  - Windows：依次选择 `pwsh`、`powershell.exe`、`cmd.exe`
  - Linux/macOS：使用 `/bin/sh`
  - 找不到可用 Shell：不能声明或启动 `shell.v1`
- Agent 必须上报 `os`、`dialect` 和 `executable`；网关必须保存并通过网页、`list_agents` 和 `get_agent_capabilities` 向用户及 AI 暴露。
- `remote_exec.command` 必须符合 Agent 声明的方言。网关至少校验 `shell.v1/exec` 返回的 `stdout: string`、`stderr: string` 和 `exit_code: integer`。
- stdout 与 stderr 各最多捕获 2 MiB，超出部分附加截断标记；超时、取消、断线和心跳失活必须清理命令进程树及 Hub 中的连接和挂起请求。

## 第一版暂不实现

- 上传下载
- Agent 共享
- 团队 RBAC
- 路径级 Shell 权限
- 用户上传插件、自定义 Handler、插件市场和运行时插件下载
- Agent 二进制 Hash 强校验、代码签名和远程证明
- 硬件 Profile
- Go 或 Rust 重写
- 复杂审计系统
- 与 MVP 无关的高级功能

## 最终验收闭环

新用户可以注册登录，通过 Agent 工作台组装 ZIP 并获得一次性 Agent Token，在另一台机器运行启动命令；网页显示 Agent 在线及其操作系统和 Shell 方言；Claude 或 WorkBuddy 使用 MCP Access Key 连接网关，调用 `list_agents` 找到该 Agent，调用 `get_agent_capabilities` 获取完整契约和运行时方言，并通过 `remote_exec` 成功执行符合该方言的目录查询等命令并获得结果。

请直接开始实施，自主推进到上述闭环真正可运行并经过验证。
