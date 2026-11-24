# 跨设备 AI 运维与风险监控系统分析

## 文档状态

- **状态**：已接入 Codex、Claude Code 只读检查与受控原生计划，现场签收见验收手册
- **日期**：2026-11-22
- **关联 PRD**：`docs/PRD-cross-device-ai-ops.md`
- **一期平台**：Windows、macOS、Linux PC/服务器
- **二期平台**：Android、iOS 及其他硬件

> 本文用于在进入开发前确认系统边界、协议演进和主要技术风险。本文不代表已经批准的实现计划，也不要求本阶段立即修改现有代码。

### 实现状态补充

当前代码已将本文的核心边界落实为首个可运行切片：旧的直接
`request/result` 路径保持不变，新增 Task Protocol 仅对声明
`task_protocol >= 1` 的 Agent 开放；健康检查只允许
`system_observe.v1` 的 L0 只读 Action，并将事件、证据和告警写入 SQLite。
显式选择 `requested_runtime: "codex"` 后，每台目标设备启动一次独立 Codex
app-server 会话。Codex 经设备端 Broker 请求四个预授权范围内的 L0 观察，
每项请求仍需 Gateway 单独决策。Claude Code 使用一次性无工具会话，只可建议同样的四个无参观察，
由 Adapter 经现有 Broker 请求，不把观察内容返回 Claude。两种引擎均为 `managed_limited`，
不进入写入、Shell 或自动修复路径；缺少任一已验证观察或运行时失败，不产生完整健康证据。
原生计划在同一 Task 协议内按 `planned_actions` 协商，只开放已定义的文件操作、固定版本检查、
已登记应用的诊断/恢复和预置源码的固定构建验证。服务代际和源码摘要约束实际执行对象。
设备连接、策略修订与动作输入绑定计划哈希。L1 默认需人工审批，仅可按设备精确文本路径放行；
L2 仍须人工批准后显式执行，设备读回确认后才保存文件变更证据。
计划型 MCP Key 不能直连 Shell/Workspace、不能审批，只能提交、查询和启动自己提交的免审批计划。
可选维护窗口和批次写入计划哈希；窗口内人工启动，未验证的批次阻止后续派发。
静态 Runtime 兼容目录复核会话模式和 CLI/Adapter 版本，不兼容时保留原生能力，不静默回退。
实际配置、数据边界及复跑入口见 [一期验收](PHASE1-ACCEPTANCE.md)。

## 0. PRD 初审结论

### 0.1 已确认方向

PRD 的核心方向成立：

1. Gateway 继续作为跨设备能力平面和安全边界。
2. 本地 Codex、Claude 或其他 AI Runtime 作为设备上的执行 Agent。
3. Windows、macOS、Linux 是一期交付平台。
4. 移动端和更多硬件保留扩展边界，不进入一期交付。
5. 现有 `shell.v1` 和 `workspace.v1` 作为 PC 端基础能力继续复用。
6. 任务、审批、风险、事件和证据需要成为新的平台级抽象。

### 0.2 初审发现的关键偏差风险

#### 风险一：长任务不能复用短请求协议

当前 Agent 协议主要是：

```text
Gateway request
  → Agent result
```

这个模型适合一次性 `shell.v1/exec` 和 `workspace.v1` Action，不适合：

- 多分钟或多小时任务
- 任务计划和审批
- 多步骤依赖
- 实时事件
- 中途取消和恢复
- 多设备交接

因此长任务必须使用独立的 Task/Step/Event 生命周期。不能把当前 `request/result` 的 `params` 扩展成一个万能任务对象。

#### 风险二：Runtime 是否能提供 Action 级拦截仍需验证

如果本地 AI Runtime 只能被当作一个普通子进程启动，Gateway 可能只能看到最终 stdout，而看不到 Runtime 内部的每一个工具调用。

完整风险控制的前提是：

```text
Runtime → Action Broker → Gateway Policy → Profile Action
```

如果某个 Runtime 不能提供 Action 级事件或工具调用拦截，必须标记为 `limited_safety`，并限制可用 Profile、工作目录或最高风险等级。不能把“启动命令被审批”误认为“后续所有行为都被审批”。

#### 风险三：一期不是高可用架构

当前项目使用 SQLite 和单进程内存 AgentHub。这个部署模型适合一期单实例：

- Gateway 只有一个 Uvicorn Worker
- 在线连接和挂起请求在内存中
- SQLite 负责持久化用户、Agent 和凭据

一期可以增加持久化 Task/Event 状态，但不能宣称多 Worker、高可用、跨实例连接迁移或严格的分布式调度能力。

#### 风险四：扩展性不能变成动态插件系统

未来接入更多引擎和硬件时，扩展必须遵循：

```text
新引擎 → Runtime Adapter
新设备 → Device Agent + Profile
新硬件 → Hardware Adapter + Profile Action
```

不引入普通用户运行时上传 Handler、下载执行插件或动态注册 MCP 工具。扩展能力应通过代码、Registry、版本和契约测试进入发布版本。

### 0.3 本系统分析做出的约束

1. 现有直接 MCP 工具继续保留，作为低层兼容路径。
2. 长任务使用独立 Task 协议，不改变既有 Profile Action 的语义。
3. 现有 WebSocket v2 继续服务旧 Agent；新 Task 能力通过特性协商和独立消息类型接入。
4. 只有完整 Action 拦截的 Runtime 才能进入完整风险模式。
5. 一期只做单 Gateway、单 Worker、SQLite 持久化和有限并发。
6. 新 Profile、新 Runtime、新设备都必须声明版本、权限、风险和兼容条件。

## 1. 系统目标与边界

### 1.1 系统目标

系统需要让用户或上游主控 Agent 能够：

- 发现多台设备及其真实能力
- 为一个目标创建跨设备任务
- 将任务拆分为可追踪的步骤
- 让本地 AI Runtime 生成计划和执行请求
- 在 Action 级别执行权限和风险检查
- 对高风险步骤进行人工审批
- 实时观察任务、设备和 Action 状态
- 在断线、超时和部分失败时保持状态一致
- 通过证据确认任务是否真正完成
- 在不重写核心任务协议的情况下扩展新的引擎和设备

### 1.2 一期系统边界

一期只支持：

- Windows、macOS、Linux PC/服务器
- 主动出站 WebSocket Agent
- 现有 `shell.v1` 和 `workspace.v1`
- 至少一个本地 AI Runtime Adapter
- 基础 `system_observe.v1` 只读能力
- 单 Gateway、单 Worker、SQLite
- Web 管理端审批和审计

一期不支持：

- Android/iOS 常驻控制 Agent
- 浏览器和桌面 GUI 自动化
- 多 Gateway 高可用
- 多 Worker 共享在线连接
- 用户自定义运行时插件
- 任意硬件的通用控制协议
- 让 Gateway 自身运行模型

### 1.3 分层职责

```text
上游主控 Agent / Orchestrator
  负责目标、计划、步骤依赖和结果汇总

Gateway
  负责身份、能力、策略、审批、路由、任务状态和证据

设备 Agent
  负责本地连接、Runtime 生命周期、Action Broker 和能力执行

本地 Runtime
  负责理解任务、生成计划、提出 Action 和解释结果

Profile Action
  负责实际的 Shell、Workspace 或设备能力操作
```

Gateway 不负责替本地 Runtime 规划所有操作，也不把本地 Runtime 的私有协议直接暴露给 MCP Client。

## 2. 核心概念模型

### 2.1 Task

Task 是用户或主控 Agent 提交的目标，描述“要完成什么”。

建议字段：

```text
task_id
user_id
created_by
objective
target_selector
requested_runtime
risk_policy
approval_policy
resource_budget
deadline
status
created_at
updated_at
```

### 2.2 Step

Step 是 Task 中有明确输入、输出、依赖和目标设备的工作单元。

建议字段：

```text
step_id
task_id
depends_on
target_agent_id
required_capabilities
action_type
inputs
expected_result
idempotency_key
retry_policy
status
started_at
completed_at
```

### 2.3 ActionRequest

ActionRequest 是实际需要执行的系统动作，不等同于模型文本。

建议字段：

```text
action_request_id
task_id
step_id
agent_id
profile
action
params
scope
risk_level
policy_context
approval_requirement
expires_at
```

### 2.4 Approval

Approval 是对计划或一组明确 Action 的授权。

建议字段：

```text
approval_id
task_id
step_id
approver_user_id
approved_scope
plan_hash
decision
reason
created_at
expires_at
```

计划、目标设备、工作目录、命令范围或风险等级发生实质变化时，原 Approval 必须失效。

### 2.5 Event

Event 是任务和设备生命周期中的不可变事实。

建议字段：

```text
event_id
task_id
step_id
agent_id
event_type
sequence
payload
created_at
```

事件采用至少一次投递语义，消费者必须根据 `event_id` 或 `(task_id, sequence)` 去重。

### 2.6 Evidence

Evidence 是用于证明 Action 或 Task 结果的材料：

- 命令和参数摘要
- stdout/stderr
- 文件 diff
- 文件前后 SHA-256
- 系统状态快照
- 日志片段
- 审批记录
- 验证结果

Evidence 必须支持脱敏、哈希和保留期限，不能默认保存所有原始模型上下文。

### 2.7 Alert

Alert 是风险引擎或设备观察器产生的可处理事件。

建议字段：

```text
alert_id
task_id
agent_id
severity
category
trigger
recommendation
status
created_at
resolved_at
```

## 3. 系统组件

### 3.1 Task Service

职责：

- 创建和读取 Task
- 生成和更新 Step
- 校验状态迁移
- 检查依赖
- 计算待执行步骤
- 处理取消、超时和预算
- 调用 Policy Service 和 Agent Hub

Task Service 不直接执行 Shell 或文件操作。

### 3.2 Policy Service

职责：

- 根据用户、设备、Profile、Action 和风险上下文计算决策
- 返回 `allow`、`approval_required`、`deny`
- 生成命中的规则 ID 和解释
- 失效过期授权
- 检查计划哈希和 Action 哈希

Policy Service 的判断不能只依赖模型描述，必须基于结构化 Action。

### 3.3 Approval Service

职责：

- 创建审批请求
- 展示计划和风险
- 记录批准、拒绝和过期
- 使计划变更后的审批失效
- 提供紧急停止和全局熔断

AI Agent 不能自动批准自己的高风险 Action。人工审批必须来自独立的用户身份。

### 3.4 Event Store

职责：

- 追加任务、设备和 Action 事件
- 为 Web UI 和主控 Agent 提供事件查询
- 支持断线后的补偿读取
- 关联 Evidence 和 Alert
- 自动脱敏和按策略清理

一期可以使用 SQLite 表实现 Event Store，不要求引入独立消息队列。

### 3.5 Agent Hub

现有 AgentHub 继续负责：

- 在线连接
- 心跳
- 直接 Profile 请求
- 请求取消
- 连接断开清理

新增 Task 通道不应把所有逻辑塞进现有 `agent_hub.py`。建议由独立的 TaskChannel/TaskDispatcher 负责 Task 消息，AgentHub 只提供连接发送和设备在线能力。

### 3.6 Runtime Registry

Runtime Registry 记录本地 Runtime 的可用性和兼容信息：

```text
provider_id
runtime_version
adapter_version
supported_platforms
supported_profiles
session_modes
approval_modes
action_interception
health_status
```

其中 `action_interception` 至少有：

```text
full
limited
none
```

只有 `full` 或被明确限制后的 `limited` Runtime 才可以参与对应风险等级的任务。

### 3.7 Device Capability Registry

设备能力可以先保存在现有 `agents` 记录的 JSON 字段中，避免一期过度拆表；但协议上应按以下概念组织：

```text
device_type
platform
platform_version
profiles
runtime_declarations
tool_inventory
service_managers
workspace_scopes
trust_level
```

后续设备和 Profile 数量增加后，再迁移到独立能力表。

## 4. 协议演进

### 4.1 兼容原则

当前协议为 WebSocket v2，已有消息：

```text
hello
ready / registered / error
request
result
cancel
ping / pong
```

建议一期不要修改既有 `request/result` 的语义，保持旧 Agent 和现有 Profile 正常工作。

新 Task 能力采用：

- `hello` 中增加可选 Feature 声明
- 独立的 Task 消息类型
- 独立的 Task 协议版本
- 没有 Task Feature 的旧 Agent 不接收 Task 消息

如果未来需要改变既有消息的字段意义或生命周期，再升级 WebSocket 主协议版本。

### 4.2 Feature 协商

设备在握手时可声明：

```json
{
  "features": {
    "task_protocol": 1,
    "runtime_adapter": true,
    "action_interception": "full",
    "event_stream": true,
    "evidence": true
  }
}
```

Gateway 根据 Feature 决定是否向该设备调度长任务。

### 4.3 Task 消息

推荐的消息类型：

```text
task_start
task_ack
task_event
task_action_request
task_action_decision
task_cancel
task_result
task_error
```

示例流程：

```text
Gateway → task_start
Agent → task_ack
Agent → task_event(plan_proposed)
Agent → task_action_request
Gateway → task_action_decision
Agent → task_event(action_started)
Agent → task_event(action_output)
Agent → task_event(action_completed)
Agent → task_result
```

### 4.4 消息标识

必须区分：

```text
task_id
step_id
action_request_id
request_id
event_id
idempotency_key
```

不能用一个 `request_id` 同时表达短请求、长任务、事件和重试。

### 4.5 投递语义

一期采用：

- Task 状态持久化
- Event 至少一次投递
- Event 可补偿读取
- Action 通过 `idempotency_key` 去重
- 不承诺跨设备分布式 exactly-once

如果远端执行成功但响应丢失，Gateway 必须先查询设备状态或 Evidence，再决定是否重试。

## 5. 本地 Runtime 接入

### 5.1 Adapter 生命周期

```text
detect
  → validate_version
  → start_session
  → receive_plan_or_task
  → emit_action_request
  → receive_action_decision
  → emit_action_events
  → finish_or_cancel
  → collect_evidence
```

### 5.2 Runtime 模式

#### Managed Full

- Runtime 工具调用可被 Action Broker 捕获
- 每个 Action 经过 Policy Service
- 可进入完整风险控制范围

#### Managed Limited

- 只能捕获部分 Action
- 必须限制工作区、Profile、风险等级或任务时长
- 结果中明确标记安全覆盖范围

#### Unmanaged

- 无法观察内部 Action
- 只能作为开发调试或只读场景使用
- 不得执行 L2/L3 操作

### 5.3 凭据边界

- 本地 Runtime 的供应商凭据留在设备上
- Gateway 不保存本地 Runtime 的长期 API Key 或登录态
- Device Agent 不将 Runtime 凭据注入普通 Shell
- stdout、stderr、事件和 Evidence 统一脱敏
- 后续如需 Secret Broker，应使用短期、按任务和按 Action 授权的凭据

### 5.4 Runtime 进程隔离

一期至少要求：

- 固定工作目录
- 明确环境变量白名单
- 不继承 Gateway 或 Agent 的敏感环境变量
- 进程树可取消
- 资源预算可检查
- Provider 版本和 Adapter 版本可记录

真正的系统级权限隔离仍由低权限 OS 用户、容器或虚拟机提供，不能由 Adapter 自己伪装成沙箱。

## 6. Action Broker 和 Profile

### 6.1 Action Broker 处理链

```text
Runtime tool call
  → 解析 Provider Action
  → 转换为标准 ActionRequest
  → 检查 Task Scope
  → Policy decision
  → Approval check
  → Profile Action
  → 记录 Event / Evidence
  → 返回结构化结果
```

### 6.2 现有 Profile

#### `shell.v1`

继续用于：

- Git
- 构建
- 测试
- 搜索
- 普通诊断命令
- 平台相关脚本

但不能把它当成真正的 Shell 沙箱，也不能以命令黑名单代替 OS 隔离。

#### `workspace.v1`

继续用于：

- 读取 UTF-8 文件
- 创建或安全覆盖文件
- 基于 SHA-256 的精确编辑
- 返回 diff 和新版本

### 6.3 一期新增候选 Profile

建议新增只读优先的 `system_observe.v1`：

```text
get_host_info
get_resource_status
get_processes
get_services
get_network_status
get_storage_status
collect_diagnostics
```

不同 OS 使用不同实现，但结果契约保持统一。

### 6.4 后续硬件 Profile

硬件不强行实现 `shell.v1`。未来可通过独立 Profile 表达：

```text
mobile_observe.v1
iot_control.v1
robot_action.v1
edge_health.v1
```

每个硬件 Profile 必须独立定义：

- 设备状态
- Action
- 物理风险
- 用户确认
- 离线行为
- 完成验证

## 7. 任务状态机

### 7.1 Task 状态

```text
created
planned
waiting_approval
approved
queued
running
waiting_device
partially_completed
verification_failed
completed
failed
cancelled
rolled_back
```

### 7.2 状态迁移原则

- `created` 只能进入 `planned` 或 `cancelled`
- `planned` 如果涉及高风险操作，必须进入 `waiting_approval`
- 审批通过后进入 `approved`
- 设备可用且依赖满足后进入 `queued`
- Action 真正开始后进入 `running`
- 所有验证通过后进入 `completed`
- 任一步骤失败不自动假设整个 Task 已失败，先进入 `partially_completed` 或 `verification_failed`
- 取消后不再启动新 Action
- `rolled_back` 只表示回滚验证完成，不表示所有外部副作用已恢复

### 7.3 Step 依赖

一期支持有向无环依赖：

```text
observe
  ↓
diagnose
  ↓
approve
  ↓
repair
  ↓
verify
```

不在一期实现任意循环工作流或无限 Agent 互相委派。

## 8. 风险策略

### 8.1 风险等级

```text
L0：只读观察
L1：工作区编辑、构建、测试
L2：依赖、配置、服务和环境变更
L3：生产、权限、凭据、网络、防护和持久化
```

### 8.2 决策结果

```text
allow
approval_required
deny
```

Policy Service 返回结果时必须包含：

- 命中的规则
- 风险等级
- 允许范围
- 需要的审批
- 用户可读解释

### 8.3 计划偏离

以下变化应使原审批失效：

- 目标设备变化
- 工作目录变化
- Profile 或 Action 变化
- 命令内容变化
- 文件修改范围扩大
- 风险等级提高
- Runtime 或 Agent 版本变化

### 8.4 Prompt Injection

来自文件、日志、Issue、网页和其他 Agent 的内容默认是不可信数据：

```text
system policy > user approval > agent plan > device data > file/external text
```

低信任内容可以作为诊断输入，但不能直接修改权限、审批状态或任务范围。

## 9. 数据持久化

### 9.1 现有表复用

继续使用现有：

- `users`
- `web_sessions`
- `mcp_keys`
- `enrollment_tokens`
- `agents`
- `agent_bundles`

### 9.2 一期新增表

#### `tasks`

保存 Task 目标、所有权、状态、策略和预算。

#### `task_steps`

保存 Step 依赖、目标设备、Action、重试和状态。

#### `task_approvals`

保存审批人、审批范围、计划哈希、决定和有效期。

#### `task_events`

保存不可变事件序列、事件类型和脱敏 Payload。

#### `task_evidence`

保存 Evidence 类型、摘要、哈希、存储位置和保留期限。

#### `risk_alerts` / `risk_alert_transitions`

保存按账号隔离的设备或任务告警，可关联设备、任务、Step 和 Action；
变更状态时追加处理人、来源和时间。旧 `task_alerts` 数据迁移保留，告警状态不能授予 Action 权限。

#### `execution_stops`

保存实例、账号、设备、Runtime 和操作类别的暂停状态，以及最新停机修订。
计划和健康任务绑定创建时的修订基线；适用范围后续曾暂停，即使已经解除也不可续跑旧授权。

#### `runtime_sessions`

保存设备上的 Runtime Provider、版本、Adapter 版本、模式和状态。

一期的 Agent 能力可以继续使用 JSON 字段；Task 和 Event 不建议只放在 JSON 中，否则不利于状态查询和审计。

### 9.3 SQLite 约束

一期继续使用 SQLite：

- WAL 模式
- 单 Gateway 进程
- 单 Worker
- 任务和事件持久化
- 在线连接和短期 Pending Future 仍在内存

不在一期实现：

- 多 Worker 共享 AgentHub
- 跨实例任务锁
- 分布式队列
- 多 Gateway 主备

## 10. API 和入口

### 10.1 Web 管理 API

一期优先提供 Web API 和管理页面：

```text
POST   /api/tasks
GET    /api/tasks
GET    /api/tasks/{task_id}
GET    /api/tasks/{task_id}/events
POST   /api/tasks/{task_id}/approve
POST   /api/tasks/{task_id}/cancel
GET    /api/tasks/{task_id}/evidence
GET    /api/alerts
POST   /api/alerts/{alert_id}/ack
```

高风险审批优先由登录用户通过 Web 页面完成，避免 AI 自己调用工具批准自己的操作。

### 10.2 MCP 入口

现有固定 MCP 工具继续保留：

```text
list_agents
get_agent_capabilities
remote_exec
remote_read_file
remote_write_file
remote_edit_file
```

Task 能力稳定后，再增加固定、强类型的 MCP 工具，例如：

```text
create_task
get_task_status
get_task_events
get_task_evidence
cancel_task
```

不增加动态生成的设备专属 MCP 工具，也不使用万能 `remote_call`。

### 10.3 Agent WebSocket

保留现有直接 Profile 请求：

```text
request → result
```

新增 Task Feature 后增加独立 Task 消息，不复用短请求字段表达长任务。

## 11. 跨平台实现

### 11.1 Windows

- `pwsh` → `powershell.exe` → `cmd.exe`
- 记录 Shell 方言和可执行文件
- 进程树终止使用 Windows 机制
- 权限和管理员身份作为风险上下文
- 服务、计划任务、防火墙属于后续结构化能力

### 11.2 macOS

- `/bin/sh` 或 PATH 中的 `sh`
- 记录 CPU 架构和系统版本
- 基础进程、端口、磁盘和网络观察
- `launchd` 属于后续结构化能力

### 11.3 Linux

- `/bin/sh` 或 PATH 中的 `sh`
- 记录发行版、内核和架构
- 基础进程、端口、磁盘和网络观察
- `systemd`、Docker/Podman 属于结构化能力

### 11.4 WSL

一期不把 Windows 原生和 WSL 合成一个 Agent。需要管理 WSL 时，运行独立 Linux Agent。

## 12. 可靠性和故障处理

### 12.1 Agent 断线

发生断线时：

1. Gateway 标记设备离线
2. 失败或暂停依赖该设备的运行中 Step
3. 取消能取消的本地 Action
4. 保留最后确认的事件序列
5. 设备重连后重新声明能力和 Runtime
6. 只有在任务策略允许时才恢复等待中的 Step

### 12.2 响应丢失

远端可能已经执行成功，但结果返回前连接断开。此时：

- 不直接重试非幂等 Action
- 优先查询设备状态和 Evidence
- 使用 `idempotency_key` 判断是否已经执行
- 无法确认时进入人工确认或 `verification_failed`

### 12.3 超时与取消

- Task 超时不等于所有子进程都已终止
- Agent 必须终止对应进程树
- Gateway 必须记录取消发送和设备确认状态
- 无法确认终止时生成风险告警

### 12.4 资源预算

任务级预算至少包括：

- 最大运行时间
- 最大 Action 数
- 最大 Shell 调用数
- 最大文件修改数
- 最大输出大小
- 最大重试次数
- 最大并发数

## 13. 安全威胁与防护

| 威胁 | 防护 |
|---|---|
| 设备凭据泄露 | 哈希存储、短期任务凭据、设备端独立凭据 |
| Agent 越权 | 用户所有权、Task Scope、Profile/Action 校验 |
| Prompt Injection | 不可信上下文分级、策略不受文本覆盖 |
| Shell 绕过策略 | 低权限 OS 用户、容器/虚拟机、Action Broker |
| Secret 泄露 | 环境变量消费、日志脱敏、敏感路径保护 |
| 重放请求 | Task/Action 过期、idempotency key、序列号 |
| 结果伪造 | 设备身份绑定、Evidence 哈希、验证步骤 |
| 连接劫持 | TLS、设备凭据、连接绑定 |
| 无限执行 | 超时、预算、并发、紧急停止 |
| 误操作 | 计划预览、风险分级、审批和二次验证 |
| 多 Agent 循环 | Step 依赖、委派深度和任务预算 |

## 14. 代码演进建议

以下是建议的职责边界，不是最终文件名：

```text
gateway/
  task_models.py
  task_service.py
  task_protocol.py
  task_dispatcher.py
  policy_service.py
  approval_service.py
  event_store.py
  evidence_service.py
  runtime_registry.py

remote_agent/
  core/
    task_runtime.py
    action_broker.py
  runtimes/
    base.py
    codex.py
    claude.py
  profiles/
    system_observe_v1/
```

现有模块的演进原则：

- `gateway/agent_hub.py` 保持连接和直接请求职责，不继续堆积 Task 业务
- `gateway/database.py` 增加迁移和持久化方法，但不把策略判断写入数据库层
- `gateway/mcp_api.py` 只负责 MCP 参数和用户上下文，不直接实现完整任务状态机
- `remote_agent/core/runtime.py` 增加消息分发能力，但 Provider 细节放到 `runtimes/`
- `remote_agent/profiles/` 继续使用静态 Registry，不引入运行时插件下载

## 15. 测试与验证策略

### 15.1 单元测试

- Task 状态迁移
- Step 依赖解析
- Approval 失效
- Policy 风险判断
- Task Scope 校验
- Event 去重
- Action 幂等键
- Secret 脱敏
- Runtime Registry 兼容性

### 15.2 Adapter 契约测试

使用 Fake Runtime 验证所有 Adapter 必须支持：

- 启动
- 事件流
- Action 请求
- Action 决策
- 取消
- 超时
- 结果
- Evidence

不要求单元测试依赖真实 Codex/Claude 登录态。

### 15.3 Profile 契约测试

新增 Profile 必须验证：

- Catalog 声明
- 固定 MCP 工具映射
- Agent Registry 装配
- Builder 打包
- 参数和结果校验
- 最低 Agent 版本
- 关键安全边界

### 15.4 端到端测试

至少覆盖：

1. Windows Agent 连接并执行观察任务
2. Linux Agent 连接并执行观察任务
3. macOS Agent 的协议和手动烟囱测试
4. Task 审批后执行
5. Task 被拒绝后不触达设备
6. Action 事件和 Evidence 可查询
7. Agent 断线后的任务状态
8. 重复 Action 不产生重复变更
9. 风险 Action 被拦截
10. 脱敏数据不出现在日志和证据

### 15.5 Provider 集成测试

真实 Provider 测试应作为显式、可选的集成测试：

- 不把个人登录态提交到仓库
- 不在普通 CI 默认运行
- 使用隔离测试目录和非生产设备
- 记录 Provider 版本和 Adapter 版本

## 16. 开发阶段建议

### Phase 0：确认协议和可行性

- 选定首个本地 Runtime
- 验证是否能获取 Action 级事件
- 确定 Task、Action、Event、Evidence 数据结构
- 确定短请求与长任务的边界
- 确定风险模式和审批范围

### Phase 1：单设备 Task 垂直闭环

- SQLite Task/Event 表
- Task 状态机
- Web 创建、查看、取消
- Fake Runtime
- Action Broker
- 事件和 Evidence
- 断线、超时和取消

### Phase 2：首个真实 Runtime

- 实现一个 Runtime Adapter
- 接入设备 Agent
- 实现 Runtime Feature 协商
- 验证工作目录、环境变量和进程生命周期
- 仅开放 L0/L1

### Phase 3：风险与观察能力

- 实现 `system_observe.v1`
- 风险等级和 Policy Service
- 审批页面
- L2/L3 阻断
- 告警和紧急停止

### Phase 4：多设备调度

- 设备选择器
- Step 依赖
- 并发限制
- 任务级预算
- 多设备事件聚合
- 失败验证和有限重试

### Phase 5：第二个 Runtime 和平台差异

- 第二个 Runtime Adapter
- Windows/macOS/Linux 差异能力
- 服务管理和容器能力
- Runtime Registry 完整化

### Phase 6：扩展验证

- 一个不具备完整 Shell 的模拟设备 Profile
- 新 Profile 契约测试
- 验证新增 Runtime 不改变 Task/Approval/Event/Evidence
- 为二期移动端和硬件控制保留协议边界

## 17. 开发前必须确认的问题

1. 首个真实 Runtime 先适配 Codex 还是 Claude？
2. 该 Runtime 是否可以拦截每一个工具调用？
3. 如果无法完整拦截，第一阶段是否只允许 L0/L1？
4. Orchestrator 继续由外部上游 Agent 承担，还是新增独立服务？
5. Task 创建第一版通过 Web API、Web UI 还是固定 MCP 工具？
6. 一期是否先做 `system_observe.v1`，再做修改型运维 Action？
7. 是否接受一期单 Gateway、单 Worker、SQLite，不承诺高可用？
8. Windows/macOS/Linux 的 Agent 是否先以前台进程运行，再提供服务安装模板？
9. 第一条真实验收链路选“健康检查”还是“故障诊断与审批修复”？
10. 本地 Runtime 的凭据是否始终只保留在设备端？

## 18. 评审结论门槛

进入开发前，至少需要确认：

- 产品范围以一期 PC、二期移动端为准
- Runtime Adapter 和 Device/Profile Adapter 的扩展边界
- Action 级安全模式和受限 Runtime 的处理方式
- Task/Step/Event/Evidence 的核心语义
- 首个 Runtime 和首个验收场景
- 一期单实例部署限制
- 不引入动态用户插件和万能远程调用

在上述内容通过评审后，再进入实现计划和代码开发阶段。
