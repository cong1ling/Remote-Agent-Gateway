# Profile 开发规范

这份规范给后续维护网关和 Agent 的开发者使用。项目的目标是让 Agent 保持极简、让 MCP 工具保持稳定，因此 Profile 不是“想到一个功能就加一个”的插件清单。

## Profile 的边界

Profile 是网关定义的一份可验证能力契约，至少包含：

- 一个全局唯一的 ID，例如 `shell.v1`；
- 能力说明、Action、参数、返回值、错误和限制；
- 一个真正加载了 Handler 的远程实现；
- 每个对 AI 开放的 Action 对应一个固定的 `mcp_tool`。

网关的 Profile Catalog 是协议来源。Agent 只能声明自己实际加载的 Profile，不能通过握手动态注册任意 MCP 工具。MCP 工具始终由网关固定发布，Agent 详情只决定某个 Agent 是否可以使用它。

## 新增 Profile 前，先证明“现有的表达不了”

新增 Profile 的准入条件是：现有 Profile 或其 Action 无法在不歪曲契约的前提下表达需求，而且新需求具有独立、稳定的执行语义。

开发前必须先检查：

1. `gateway/profiles.py` 中是否已有相同或近似的 Profile、Action 和 `mcp_tool`。
2. `remote_agent/profiles/` 中现有 Handler 能否通过已有参数和返回契约完成需求。
3. 差异是否只是运行环境、Shell 方言、可执行文件或默认值变化。
4. 差异是否只是一个方便调用的别名、一个用户脚本或一次性的业务流程。
5. 新需求是否需要不同的状态模型、数据通道、交互方式、权限边界或外部协议。

如果只是前四类情况，不要新增 Profile：

- PowerShell、CMD、POSIX sh 是 `shell.v1` 的运行时方言，不是三个 Profile。
- 用 Shell 读取一次系统信息，不需要另造 `system_info.v1`。
- 给已有 Action 增加一个兼容的可选参数，优先评估扩展原有契约。
- 用户自己的脚本、插件或工作流不应变成网关标准 Profile。
- 不要为了让目录里“多一个能力”而硬凑 Profile。

只有在执行模型或契约确实不同，并且这种差异会影响 AI 的调用方式、返回数据或安全边界时，才考虑新 Profile。例如：

| 需求 | 处理方式 |
| --- | --- |
| PowerShell 与 POSIX 命令 | 继续使用 `shell.v1`，通过 runtime 声明方言 |
| 一次性 Shell 命令 | `shell.v1/exec` |
| 持久会话、远程 stdin、PTY、TUI | 评估独立的 `terminal.v1` |
| 可靠的结构化文本 read/write/edit | 只有 Shell 的传输语义明显不够时，才评估 `workspace.v1` |
| 大文件或二进制流 | 评估独立的数据通道/Profile，不塞进 `shell.v1` |
| 串口、GPIO 等硬件协议 | 进入硬件阶段后评估 `serial.v1`、`gpio.v1` 等 |

在开始编码前，先写清楚一句话：

> 现有的 ______ 无法表达 ______，因为 ______；新 Profile 的独立契约是 ______。

如果这句话写不清楚，通常说明还不该加 Profile。

## 版本和命名

- ID 使用 `<domain>.v<major>`，全局唯一、稳定、可读。
- 不要用操作系统或编程语言命名 Profile；实现语言写在 `implementations`，运行差异写在 `runtime`。
- 兼容性增强优先留在原 Profile；改变参数含义、返回结构、交互模型或安全边界时，才提高主版本号。
- Action 名称在同一 Profile 内唯一，`mcp_tool` 名称在整个网关内唯一，不能复用已有固定工具名。

## 实现顺序

1. 在 `gateway/profiles.py` 注册完整契约：说明、状态、实现入口、Action、参数、返回值、错误和限制。
2. 在 `remote_agent/profiles/` 实现 `ProfileHandler`，只实现注册表中声明的 Action，并在启动时返回真实 runtime 声明。
3. 在 `remote_agent/profiles/__init__.py` 接入 Handler；Builder 必须能够按 Profile 选择并打包它。
4. 在网关 MCP 层增加语义明确的固定工具。工具描述必须告诉 AI：

   > 调用前必须先对目标 `agent_id` 调用 `get_agent_capabilities`；只有 Agent 声明了对应 Profile，且能力详情中该 Action 的 `mcp_tool` 是当前工具时才能调用；网关会再次校验。

5. 网关调用路径同时校验用户所有权、Agent 是否声明 Profile、Action 是否存在以及返回值是否符合契约。工具描述是引导，不是权限边界。
6. 增加必要测试：声明校验、Handler 路由、Builder 打包、`tools/list` 与 `mcp_tool` 映射、无该 Profile 的 Agent 被拒绝、结果契约校验。
7. 更新 README 和根目录 `DECISIONS.md`，说明为什么现有 Profile 无法表达它，以及主要取舍。

## 不要做的事

- 不按每台 Agent 动态生成 MCP 工具。
- 不用万能的 `remote_call(profile, action, params)` 代替语义明确的固定工具。
- 不把 Agent 自己声明的任意字符串直接当成可用能力。
- 不把上传下载、审计、团队权限或插件市场顺手塞进一个看似相关的 Profile。
- 不为了未来可能用到而提前设计一组没有真实 Handler 的 Profile。

## 完成标准

一个新 Profile 只有在以下问题都能回答“是”时才算完成：

- 它解决的是现有 Profile 无法诚实表达的独立问题；
- 网关和 Agent 都有对应实现，且声明与实际 Handler 一致；
- AI 可以先发现 Agent，再从能力详情得到准确的 `mcp_tool` 和契约；
- 网关会在调用时重新校验所有权、能力和结果；
- 没有引入动态 MCP、隐含权限或不必要的持久化；
- 失败、限制、版本变化和迁移方式都有明确说明。
