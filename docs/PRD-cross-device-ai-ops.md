# 跨设备 AI 运维与风险监控平台

## 文档状态

- **状态**：一期核心链路已实现，平台与现场签收见验收手册
- **日期**：2026-11-22
- **一期范围**：Windows、macOS、Linux PC/服务器
- **二期范围**：Android、iOS 等移动设备
- **关联项目**：Remote Agent Gateway

> 本文是产品需求草案，不是已经批准的协议或实现方案。未完成评审前，不据此进行大范围实现或破坏现有 Profile 契约。

### 当前开发切片

已落地并通过真实本地 Agent 链路验证的范围：

- WebSocket v2 可选 Task Protocol v1、Feature/Runtime 能力协商
- `system_observe.v1` 四个固定只读 Action
- Task、Step、Action、Event、Evidence、Alert 的 SQLite 持久化
- `POST /api/tasks` 健康检查入口及事件、证据、告警查询
- Windows 与 Ubuntu WSL 已验证真实 Agent 和原生 Codex app-server，并可在同一任务中分别提交证据（模型服务使用本地测试替身）
- Codex 可发起四个固定观察，由 Gateway 逐项授权；macOS 已纳入 CI 矩阵，尚无实际验收结果
- 受控原生任务支持跨设备步骤依赖、按计划版本审批、一次性执行、实时事件及文件读回证据
- 计划型 MCP Key 支持主控 Agent 提交计划；设备所有者可以对精确文本路径授予 L1 免审批许可，L2 始终由人审批执行
- 登记应用的诊断、人工批准恢复与新代际就绪验证；两 Agent 对相同源码 ZIP 的固定 Python 验证
- 维护计划绑定窗口、批次和并发上限，手动启动、失败停批；静态 Runtime 目录约束派发和会话版本
- 所有者可显式授权有期限的原生只读巡检；健康检查支持新版 Agent 的单步取消，缺失观察不计为完整通过
- Claude Code 支持第二种受限 Runtime，仅可提交无参观察名称，由原有 Broker 验证；两种引擎都不能替代 L2 人审

受控计划未开放任意 Shell、通用修复、移动端、硬件控制和完整 Codex 工具编排。
受控计划与健康检查的完整验证入口及尚未签收项见 [一期验收](PHASE1-ACCEPTANCE.md)。

### 初审结论

本轮初审确认产品方向成立，但补充以下必须遵守的实现边界：

1. 长任务不能直接复用当前一次性 `request/result` 语义，必须使用独立的 Task、Step、Event 和 Evidence 生命周期。
2. 只有能提供 Action 级拦截或等效受控执行能力的本地 Runtime，才能进入完整风险控制模式；否则必须显式标记为受限安全模式。
3. 当前 SQLite + 单 Uvicorn Worker 的部署形态可以支持一期单实例，但不代表高可用或多 Worker 横向扩展能力。
4. 新引擎和新硬件通过 Adapter、Device Agent 和静态 Profile 扩展，不引入用户运行时上传插件，也不绕过既有 Profile 契约。
5. 一期产品优先完成 PC 运维闭环，移动端和具体硬件控制仅保留协议扩展点，不进入一期交付。

技术边界、协议演进和数据模型见配套系统分析文档。

## 1. 产品概述

### 1.1 产品定位

Remote Agent Gateway 演进为一个面向个人和小型团队的跨设备 AI 运维与风险监控平台：

> 用户通过一个 Gateway 管理多台电脑、服务器和 CI 设备；每台设备上的本地 AI Runtime（例如 Codex 或 Claude）负责理解和执行任务，Gateway 负责设备发现、任务路由、权限控制、风险拦截、实时监控和证据审计。

平台不把模型、聊天机器人或多 Agent 编排逻辑强行塞入 Gateway。Gateway 继续作为跨设备能力平面和安全边界；本地 AI Runtime 作为设备上的执行 Agent；上层主控 Agent 或独立 Orchestrator 负责规划任务和协调多个设备。

### 1.2 一期目标

在 Windows、macOS、Linux 三类 PC/服务器上实现一个可验证的闭环：

```text
创建任务
  → 选择设备
  → 本地 AI 生成计划
  → 风险评估
  → 用户审批
  → 执行受控 Action
  → 实时监控
  → 结果验证
  → 证据归档
```

一期不追求覆盖所有操作系统能力，而是保证同一套任务协议可以根据设备实际能力，在三类平台上安全执行、报告差异并完成验证。

### 1.3 二期方向

移动端不纳入一期交付。二期再根据平台能力分别支持：

- Android：设备监控、受限执行和用户确认操作
- iOS/iPadOS：告警接收、任务审批、设备状态和用户主动诊断
- 移动端作为跨设备运维的移动控制台和风险响应入口

移动设备不要求复用 PC Agent 的完整 Shell 模型，也不要求具备与 PC 相同的后台执行能力。

## 2. 背景与问题

### 2.1 当前问题

当前 Remote Agent Gateway 已具备：

- 远程 Agent 注册、心跳和重连
- Agent 所有权和用户隔离
- `shell.v1` 非交互命令执行
- `workspace.v1` 文本文件读取、写入和精确编辑
- MCP 固定工具和 Profile 能力发现
- Shell 超时、取消和并发限制
- 基于 Agent 运行时的命令策略

这些能力可以支撑远程开发和基础远程运维，但仍缺少：

- 本地 Codex/Claude Runtime 的统一接入
- 跨设备任务模型和任务状态机
- Agent-to-Agent 的结构化任务交接
- Action 级权限和审批
- 计划与实际执行的差异监控
- 风险评分、告警和紧急停止
- 跨设备执行证据和审计时间线
- 断线恢复、幂等执行和失败验证

### 2.2 用户痛点

用户希望对多台不同平台设备进行统一的 AI 辅助运维，但当前通常需要：

- 手动登录不同电脑或服务器
- 为不同系统记忆不同命令
- 在多个 AI 工具之间复制日志和上下文
- 无法清晰知道 AI 实际执行了哪些动作
- 难以在高风险操作前进行审批
- 设备掉线后无法判断操作是否已经完成
- 出现问题后无法还原完整执行链路

## 3. 目标用户与典型场景

### 3.1 目标用户

1. **个人开发者**
   - 管理本地开发机、家用服务器和 CI 环境
   - 使用 AI 诊断构建失败、环境异常和部署问题

2. **小型团队运维人员**
   - 管理少量 Windows、macOS、Linux 设备
   - 需要统一的健康检查、告警、审批和审计

3. **开发负责人**
   - 协调开发机、测试机、构建机和部署机
   - 关注任务交接、变更证据和结果验证

4. **安全或风险负责人**
   - 关注高风险命令、敏感文件访问、权限提升和异常行为
   - 需要在执行前阻止或审批高风险动作

### 3.2 一期核心场景

#### 场景 A：跨设备健康检查

用户要求检查一组设备的运行状态：

1. 主控 Agent 查询设备能力和在线状态
2. 根据平台能力选择 Windows、macOS、Linux 设备
3. 并行收集系统、磁盘、网络、进程和服务状态
4. 汇总结构化结果
5. 对异常设备生成告警和后续诊断建议

#### 场景 B：故障诊断与审批修复

某台服务器服务异常：

1. 观察 Agent 读取服务状态和相关日志
2. 诊断 Agent 分析日志和配置
3. 生成包含命令、文件变更和风险等级的修复计划
4. 用户审批计划
5. 设备上的本地 AI Runtime 按计划执行
6. Gateway 实时记录 Action、输出和文件 diff
7. 验证服务恢复情况
8. 归档完整证据

#### 场景 C：跨设备开发验证

开发机修改代码后，需要在其他设备验证：

1. 开发设备上的 AI Runtime 修改工作区
2. 执行本地静态检查和测试
3. 将构建或测试任务交给兼容的 Linux/Windows 构建设备
4. 汇总构建日志和测试结果
5. 失败时生成下一步诊断任务

该场景用于验证跨设备任务交接，但一期产品优先级低于 AI 运维和风险监控。

#### 场景 D：计划维护窗口

用户为一组设备创建维护任务：

1. 预先生成执行计划
2. 指定维护时间窗口和目标设备
3. 设定最大并发数和失败停止策略
4. 统一审批
5. 分批执行
6. 每批次完成后验证
7. 发现异常时暂停剩余批次

## 4. 产品原则

### 4.1 Gateway 是安全边界，不是模型运行时

- Gateway 不运行 LLM
- Gateway 不保存本地 Runtime 的长期模型凭据
- Gateway 不替代上游 Agent Runtime 的规划能力
- Gateway 负责身份、所有权、能力、路由、策略和审计

### 4.2 Action 优先于自由对话

Agent 之间不以自由文本聊天作为主要协作协议，而是围绕结构化对象协作：

- Task
- Plan
- ActionRequest
- Approval
- ActionResult
- Event
- Evidence
- Alert
- Artifact

自由文本可以作为说明和上下文，但不能作为权限判断或执行授权的唯一依据。

### 4.3 设备能力必须真实声明

设备不能只声明“Windows”“Linux”或“支持 Shell”，而必须声明：

- 操作系统和版本
- Shell 方言和可执行文件
- 本地 Runtime 及版本
- 可用工具
- 可用服务管理器
- 可访问工作区
- 需要用户确认的能力
- 当前权限和信任等级

### 4.4 默认拒绝，逐步授权

- 任务权限绑定到具体设备、工作区和有效期
- 高风险 Action 默认需要审批
- 未声明的能力不能调用
- 任务范围外的资源访问必须拒绝
- 设备掉线后默认停止未完成的受控 Action

### 4.5 可观察、可解释、可停止

用户必须能够知道：

- AI 计划做什么
- 实际做了什么
- 当前在哪里执行
- 为什么被允许或拦截
- 谁批准了操作
- 结果是否验证

用户必须可以停止：

- 当前 Action
- 当前任务
- 某台设备的所有任务
- 某个本地 Runtime
- 全局任务执行

### 4.6 可扩展性优先

一期虽然只交付 Windows、macOS 和 Linux PC/服务器，但产品协议不能围绕某一个模型供应商或某一类设备硬编码。

扩展能力分为三个相互独立的维度：

```text
Runtime：谁负责理解和规划任务
Profile：设备可以执行什么能力
Device：能力实际运行在哪里
```

后续接入新引擎时，只增加 Runtime Adapter；后续接入新硬件时，只增加设备 Agent、Profile 和对应实现。Task、Approval、Event、Evidence 和风险策略不应因为新增引擎或硬件而重写。

所有扩展都必须声明：

- 能力和版本
- 输入、输出和错误契约
- 取消和超时语义
- 所需权限
- 风险等级
- 是否需要用户确认
- 最低兼容 Agent 版本

### 4.7 固定协议，替换实现

平台应保持稳定的核心协议和可替换的外围实现：

- Gateway 不依赖某个 Runtime 的内部输出格式
- Task 不依赖具体设备类型
- 风险策略不依赖具体操作系统命令
- Profile 不依赖某个模型供应商
- 新实现通过能力声明参与调度，而不是通过隐式约定工作

## 5. 产品架构

```text
用户 / 上游主控 Agent
          ↓
任务编排层 / Orchestrator
  计划、分工、依赖、上下文和结果汇总
          ↓
Remote Agent Gateway
  认证、设备目录、能力路由、策略、审批、审计
          ↓
设备 Agent
  连接、生命周期、Action Broker、证据采集
          ↓
本地 AI Runtime
  Codex / Claude / 其他兼容 Runtime
          ↓
本地能力
  workspace.v1 / shell.v1 / 未来标准 Profile
```

### 5.1 Orchestrator

Orchestrator 负责：

- 将用户目标拆分为任务和步骤
- 选择目标设备
- 根据能力和依赖安排执行顺序
- 管理多个 Agent 之间的结构化交接
- 汇总日志、结果和证据
- 在失败时生成诊断或补救步骤

一期可以由现有上游 AI Runtime 或外部服务承担，不要求 Gateway 内置完整 Orchestrator。

### 5.2 Gateway

Gateway 负责：

- 用户认证和设备所有权
- Agent 注册、在线状态和能力目录
- Task、Step、Action 和 Approval 的路由
- 风险策略评估
- Action 审批和拒绝
- 事件流和审计记录
- 设备级并发和任务级配额
- 结果验证和证据关联

Gateway 不直接理解 Codex 或 Claude 的内部推理格式，只处理统一的 Runtime 和 Action 协议。

### 5.3 设备 Agent

设备 Agent 负责：

- 主动连接 Gateway
- 声明平台、Runtime 和能力
- 管理本地 Runtime 进程
- 向 Action Broker 提交动作请求
- 执行已授权的 Profile Action
- 捕获 stdout、stderr、文件 diff 和状态变化
- 处理取消、超时、重连和本地清理

### 5.4 Local Runtime Adapter

使用统一接口适配不同本地 AI Runtime。Codex 和 Claude 是一期候选实现，不是协议层面的唯一选择。

```text
LocalAgentRuntime
├── detect()
├── get_version()
├── start_task()
├── stream_events()
├── request_action()
├── cancel_task()
├── get_status()
└── collect_evidence()
```

一期至少预留：

- `CodexAdapter`
- `ClaudeAdapter`

后续可以接入：

- 其他本地编码 Agent
- IDE 内置 Agent
- 企业内部 Agent
- CI / Build Agent
- 专用运维 Agent
- 不同模型供应商的 Agent Runtime

Adapter 不应将供应商特有的命令、输出格式、审批模式和会话状态泄漏到 Gateway 的公共协议中。

### 5.5 Runtime Registry

Gateway 需要维护 Runtime 的能力目录，而不是只保存一个字符串名称：

```text
RuntimeRegistry
├── provider_id
├── runtime_version
├── adapter_version
├── supported_platforms
├── session_modes
├── approval_modes
├── supported_profiles
└── capability_flags
```

设备连接时声明实际可用的 Runtime，任务调度时根据：

- Runtime 是否在线
- Runtime 版本是否兼容
- 是否支持所需 Profile
- 是否支持 Action 级拦截
- 是否满足风险策略

选择合适的执行节点。

### 5.6 硬件和新设备扩展

硬件接入不能直接复用 PC 的完整 Shell 模型。新设备应通过独立的 Device Agent 和标准 Profile 表达能力：

```text
Device Agent
  → Device Profile
  → Profile Action
  → Hardware Adapter
  → 设备系统或硬件接口
```

未来可以支持：

- Android / iOS
- NAS、路由器和家庭服务器
- 边缘计算设备
- IoT 设备
- 工业设备
- 机器人或专用执行终端

这些设备可以只实现观察、告警、审批或少量受限控制 Action，不要求具备 `shell.v1`。

新增硬件类型的最小接入要求：

1. 声明设备身份、平台和能力
2. 定义独立的 Profile 契约
3. 实现 Action、结果和错误语义
4. 接入统一权限和风险策略
5. 支持取消、超时、离线和版本兼容
6. 为关键 Action 提供证据和验证结果

新增硬件不应迫使 Gateway 增加一套专用任务协议，也不应通过万能 `remote_call` 绕过 Profile 契约。

### 5.7 Action Broker

Action Broker 是本地 Runtime 和系统能力之间的中间层：

```text
本地 AI Runtime
        ↓
Action Broker
        ↓
Gateway Policy / Approval
        ↓
Profile Action
        ↓
本地系统
```

它负责：

- 识别 Runtime 请求的工具动作
- 转换为统一 `ActionRequest`
- 提交 Gateway 风险策略
- 等待自动允许、人工审批或拒绝
- 记录动作前后的输入和结果
- 阻止 Runtime 绕过 Gateway 直接获得完整系统权限

如果某个供应商 Runtime 在一期无法拦截内部工具调用，则必须运行在受限模式，并明确标记为 `limited_safety`，不能宣称具备完整 Action 级风险控制。

## 6. 功能需求

### 6.1 设备目录与能力发现

#### 必须支持

- 查看设备在线、离线和最近心跳
- 查看设备类型、操作系统和版本
- 查看本地 Runtime 及版本
- 查看支持的 Profile、Action 和运行时
- 查看设备权限和信任等级
- 按平台、标签、环境和能力筛选设备
- 根据能力选择任务目标

#### 设备声明示例

```json
{
  "agent_id": "agent_xxx",
  "device_type": "desktop",
  "os": {
    "family": "linux",
    "version": "Ubuntu 24.04"
  },
  "runtimes": [
    {
      "provider": "claude",
      "version": "unknown",
      "status": "available"
    }
  ],
  "profiles": [
    "shell.v1",
    "workspace.v1"
  ],
  "capabilities": [
    "service.inspect",
    "docker.inspect"
  ],
  "trust_level": "managed"
}
```

### 6.2 任务和步骤

#### Task

Task 表示用户或主控 Agent 需要完成的目标，至少包含：

- `task_id`
- `created_by`
- `objective`
- `target_selector`
- `risk_policy`
- `approval_policy`
- `deadline`
- `status`
- `created_at`
- `updated_at`

#### Step

Step 表示任务中的一个可执行或可验证步骤，至少包含：

- `step_id`
- `task_id`
- `depends_on`
- `target_agent_id`
- `action`
- `inputs`
- `expected_result`
- `retry_policy`
- `status`
- `evidence_ids`

#### 任务状态

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

### 6.3 计划与审批

计划必须在执行前呈现：

- 目标设备
- 工作目录
- 计划步骤
- 预计命令或工具动作
- 预计文件变更
- 风险等级
- 需要的权限
- 网络和外部资源访问
- 超时和资源预算
- 失败处理和回滚方式

审批结果必须记录：

- 审批人
- 审批时间
- 审批范围
- 审批是否一次性
- 审批有效期
- 审批后计划是否发生变化

计划发生实质变化时，原审批自动失效。

### 6.4 Action 级权限控制

权限至少分为四级：

| 等级 | 示例 | 默认行为 |
|---|---|---|
| L0 | 读取系统状态、日志、版本 | 自动允许 |
| L1 | 修改工作区、执行构建和测试 | 可按设备策略自动允许 |
| L2 | 安装依赖、修改配置、重启服务 | 需要审批 |
| L3 | 权限提升、生产变更、凭据、防火墙、持久化 | 强制审批或禁止 |

权限判断至少考虑：

- 用户身份
- 主控 Agent 身份
- 设备所有权
- 设备环境
- Profile 和 Action
- 工作目录
- 目标路径
- Shell 方言
- 当前 OS 账号权限
- 维护窗口
- 风险等级
- 任务有效期

### 6.5 现有 Profile 复用

一期复用现有 Profile：

#### `workspace.v1`

用于：

- 读取 UTF-8 文本
- 创建或安全覆盖文件
- 基于 SHA-256 精确编辑
- 返回 diff 和版本信息

#### `shell.v1`

用于：

- Git
- 构建和测试
- 日志检索
- 进程和服务检查
- 容器操作
- 平台相关诊断

注意：`shell.v1` 继承设备账号权限，不是操作系统沙箱。更细粒度的运维能力应通过新的结构化 Profile 表达，而不是长期依赖任意命令字符串。

### 6.6 结构化运维能力

一期建议新增或预留一个只读运维 Profile，例如：

```text
system_observe.v1
├── get_host_info
├── get_resource_status
├── get_processes
├── get_services
├── get_network_status
├── get_storage_status
└── collect_diagnostics
```

Windows、macOS、Linux 可以使用不同底层实现，但保持统一的结果契约。

涉及修改系统的能力应单独评估是否属于新的 Profile，不应把所有操作继续放进一个万能 `remote_exec`。

### 6.7 风险监控

风险监控按以下三层定义目标信号。哪些信号已有实测、哪些仍是缺项，以
[一期验收的信号边界](PHASE1-ACCEPTANCE.md#prd-67-信号边界) 为准；未覆盖的信号不计作已验收。

#### 设备风险

- 设备离线
- Agent 重连异常
- CPU、内存、磁盘异常
- 网络变化
- OS 或 Runtime 版本变化
- 服务和进程异常
- 工作区漂移

#### Agent 风险

- Action 频率异常
- 连续失败或重复重试
- 任务范围外访问
- 多 Agent 循环调用
- 执行时间和资源消耗异常
- 计划与实际动作偏离
- 未经授权的 Runtime 行为

#### 操作风险

- 访问敏感路径
- 读取或输出凭据
- 权限提升
- 修改生产配置
- 删除大量文件
- 修改启动项或持久化配置
- 修改防火墙或网络
- 安装未知依赖
- 访问外部网络或上传数据

### 6.8 风险告警

告警至少包含：

- 告警 ID
- 风险类型
- 风险等级
- 目标设备
- 关联任务和步骤
- 触发动作
- 触发依据
- 推荐处理
- 当前状态

告警状态：

```text
open
acknowledged
approved
blocked
resolved
ignored
```

### 6.9 实时事件与证据

事件至少包括：

```text
task.created
task.planned
approval.requested
approval.granted
approval.denied
action.requested
action.allowed
action.blocked
action.started
action.output
action.file_changed
action.completed
action.failed
device.offline
task.verified
```

证据包括：

- 命令和参数
- stdout/stderr
- 文件 diff
- 文件 SHA-256
- 设备状态快照
- 相关日志片段
- 审批记录
- 风险判断
- 验证结果

日志和证据必须自动脱敏，避免保存密码、Token、Cookie 和私钥。

### 6.10 多引擎和多硬件接入

#### Runtime 接入要求

新增 Runtime 必须通过统一 Adapter 接口接入，并提供：

- 版本和健康状态
- 任务启动和取消
- 计划和 Action 事件
- 工具调用或受限执行模式
- 输出和错误流
- 资源使用情况
- 最终结果和证据

如果 Runtime 无法提供 Action 级事件，必须明确标记为受限安全模式，并限制其可执行的 Profile、风险等级或工作目录。

#### 硬件接入要求

新增硬件设备必须通过 Device Agent 或兼容的边缘适配器接入，不要求硬件运行完整 Python Agent。硬件只需要实现它实际支持的 Profile 和 Action。

硬件 Profile 需要明确：

- 设备状态模型
- 可观测指标
- 控制动作
- 物理风险
- 用户确认要求
- 离线时的行为
- 动作完成后的验证方式

例如，未来机器人动作可能需要“用户确认 + 安全区域检查 + 执行后传感器验证”，不能简单映射为普通 Shell 命令。

#### 扩展兼容性

- 新 Runtime 不应改变既有 Task、Approval 和 Event 语义
- 新设备不应改变既有 PC Profile 的行为
- 新 Profile 必须声明主版本和最低 Agent 版本
- 破坏性能力变更必须升级 Profile 主版本或提供迁移路径
- Gateway、Agent Core 和 Adapter 需要有独立版本信息

### 6.11 取消、熔断和紧急停止

必须支持：

- 取消当前 Action
- 取消当前 Step
- 取消整个 Task
- 停止设备上的所有受控任务
- 禁用某个本地 Runtime
- 暂停某类高风险 Action
- 全局紧急熔断

默认行为：

- Agent 断线后停止未完成的受控 Action
- Gateway 重启后不重复执行未确认的 Action
- 结果丢失时通过幂等键和设备状态重新确认

## 7. 跨平台需求

### 7.1 Windows

一期必须支持：

- PowerShell 优先
- Windows PowerShell 回退
- CMD 最低回退
- Windows 进程树终止
- Windows 路径和盘符
- 管理员权限检测
- 基础进程和服务检查

后续支持：

- Windows Service
- Task Scheduler
- Windows 防火墙
- 注册表和启动项风险检测

### 7.2 macOS

一期必须支持：

- POSIX Shell
- CPU、内存、磁盘、网络状态
- 进程检查
- 基础服务和端口检查
- Apple Silicon / Intel 架构识别

后续支持：

- `launchd`
- Homebrew
- macOS 隐私权限状态
- LaunchAgent / LaunchDaemon 风险检测

### 7.3 Linux

一期必须支持：

- POSIX Shell
- CPU、内存、磁盘、网络状态
- 进程检查
- 基础服务和端口检查
- root / sudo 权限识别
- Docker 或 Podman 能力识别

后续支持：

- `systemd`
- 包管理器识别
- cron 和持久化入口检测
- 容器和 Kubernetes 运行时

### 7.4 Windows WSL

一期不将 Windows 原生环境和 WSL 合并为一个逻辑 Agent。

如果需要管理 WSL，优先把 WSL 作为独立 Linux Agent 运行，由 Gateway 分别记录：

- Windows Agent
- WSL Agent

这样可以避免路径、进程树、权限和网络语义混淆。

## 8. 安全与信任模型

### 8.1 身份链

每次 Action 必须能追溯完整身份链：

```text
用户
  → 主控 Agent / Orchestrator
  → Gateway
  → 设备 Agent
  → 本地 Runtime
  → Action
```

### 8.2 任务级委托凭据

不使用长期 MCP Key 作为所有操作的唯一授权。任务执行时生成短期委托凭据，绑定：

- `task_id`
- `source_agent_id`
- `target_agent_id`
- `runtime`
- `workspace`
- `allowed_profiles`
- `allowed_actions`
- `risk_level`
- `expires_at`

任务过期、取消或完成后，凭据失效。

### 8.3 敏感数据

- 默认禁止读取凭据目录和敏感文件
- 进入模型上下文前进行脱敏
- stdout、stderr、diff 和事件统一脱敏
- 不把 Gateway 的长期 Secret 注入设备 Shell
- 不把本地 Runtime 凭据上传到 Gateway
- 支持短期 Secret Broker，但不作为一期必需功能

### 8.4 不可信上下文

日志、代码、README、Issue、网页和其他 Agent 输出默认视为不可信数据，不得直接改变系统策略或授予权限。

任务上下文必须区分：

```text
system
user
agent
device
file
external
```

## 9. 可靠性需求

### 9.1 幂等性

所有可重试 Action 必须有：

- `idempotency_key`
- 目标设备
- 目标工作区或资源
- 执行前状态
- 执行后状态

无法保证幂等的 Action 必须明确标记，并在重试前要求重新确认。

### 9.2 断线处理

设备断线时：

- 标记设备离线
- 终止或挂起相关任务
- 清理 Gateway 挂起请求
- 记录最后一次已确认事件
- 重连后重新声明能力和 Runtime 版本
- 不自动重复执行未确认 Action

### 9.3 资源预算

任务支持配置：

- 最大运行时间
- 最大 Action 数量
- 最大 Shell 调用次数
- 最大文件修改数量
- 最大输出大小
- 最大并发数
- 最大重试次数

超过预算时任务暂停或失败，并生成告警。

## 10. 非功能需求

### 10.1 可审计性

所有允许、拒绝、审批、执行、取消和验证事件都必须可查询。

### 10.2 可解释性

用户能看到：

- 为什么选择这台设备
- 为什么允许或拒绝某个 Action
- 当前风险等级如何计算
- 实际执行与计划的差异

### 10.3 可扩展性

- 新 Runtime 通过 Adapter 接入
- 新平台通过设备 Agent 和 Profile 实现
- Gateway 不依赖某个供应商的内部输出格式
- Profile 继续遵守静态注册和固定 MCP 工具原则

### 10.4 数据最小化

- 不保存不必要的原始模型上下文
- 日志和证据默认脱敏
- 支持配置证据保留时间
- 凭据和长期 Secret 不进入普通审计日志

## 11. 一期范围

### 11.1 一期必须完成

1. Windows、macOS、Linux 设备接入
2. 面向多 Runtime 的 Adapter 抽象
3. 至少一个本地 Runtime 的实际适配
4. Task / Step / Action 基础模型
5. Action Broker
6. 任务计划和人工审批
7. 风险等级和基础策略
8. 实时事件和执行证据
9. 任务取消、超时和断线清理
10. `workspace.v1` 和 `shell.v1` 复用
11. 基础设备健康检查
12. 跨平台能力声明
13. Runtime 和 Profile 的版本兼容机制
14. 不依赖具体 Runtime 的 Task、Approval、Event、Evidence 契约

### 11.2 一期应该完成

- 第二个本地 Runtime Adapter
- `system_observe.v1` 只读 Profile
- 任务级资源预算
- 设备标签和选择器
- 维护窗口和分批执行
- 基础风险告警页面
- Windows/macOS/Linux 服务启动模板
- Runtime Registry
- Profile / Device Adapter 的契约测试

### 11.3 一期不做

- Android / iOS Agent
- 手机远程控制
- 浏览器或桌面 GUI 自动化
- 摄像头、麦克风、屏幕控制
- 企业 MDM
- 任意第三方插件市场
- Gateway 内置 LLM
- 复杂组织 RBAC
- 完整自动回滚系统
- 全量基础设施编排
- 具体 IoT、机器人或工业硬件控制

一期不实现移动端和其他硬件，但必须保留后续通过 Device Agent、Profile 和 Hardware Adapter 接入的协议边界。

## 12. MVP 验收标准

### 12.1 设备接入

- 可以分别接入一台 Windows、macOS、Linux 设备
- 每台设备能够声明正确的 Shell、OS、Runtime 和 Profile
- 设备离线和重连状态可以被准确记录

### 12.2 任务执行

- 用户可以创建一个目标明确的运维任务
- 主控 Agent 可以将任务拆分为多个 Step
- Step 可以路由到不同设备
- 任务可以等待审批后继续执行
- 用户可以取消任务和单个 Action

### 12.3 风险控制

- L0/L1 操作可以按策略自动执行
- L2 操作触发审批
- L3 操作默认拒绝或强制审批
- 计划变更后原审批失效
- 被拦截的操作不会触达目标设备

### 12.4 证据与监控

- 每个 Action 都有状态、输入、输出和时间线
- 文件修改可以查看 diff 和版本信息
- 命令输出和事件可以关联到具体设备和任务
- Secret 不会出现在普通日志和证据中
- 任务完成前必须有可验证的结果

### 12.5 故障处理

- Action 超时后可以终止本地进程
- 设备断线后不会无条件重复执行
- 重复提交同一个 idempotency key 不会造成重复变更
- 任务部分失败时可以准确显示已完成和未完成步骤

### 12.6 可扩展性

- 在不修改 Task、Approval、Event、Evidence 核心模型的情况下，可以增加一个新的 Runtime Adapter
- 在不修改 Gateway 主任务协议的情况下，可以增加一个新的 Device Profile
- 新 Runtime 和新 Profile 都可以声明版本、能力、权限和风险要求
- 新增的受限实现不会被错误地当成具备完整 Shell 能力的 PC Agent

## 13. 里程碑建议

### M0：协议和安全模型

- 确定 Task、Step、Action、Approval、Event、Evidence 模型
- 确定身份链和任务级委托凭据
- 确定 Action Broker 边界
- 验证本地 Runtime 是否能提供足够的 Action 级事件

### M1：单设备闭环

- 一台 PC
- 一个 Runtime
- 任务启动、输出、取消和超时
- Workspace 和 Shell 受控执行
- 基础审计

### M2：三平台接入

- Windows
- macOS
- Linux
- 能力声明和平台差异
- 基础健康检查

### M3：多设备编排

- 目标选择器
- Step 依赖
- 跨设备交接
- 并发、重试和断线恢复

### M4：风险监控

- 风险分级
- 人工审批
- 命令和文件风险检测
- 告警
- 熔断和紧急停止

### M5：第二个 Runtime 和维护场景

- 第二个 Local Runtime Adapter
- 分批维护
- 维护窗口
- 结果验证
- 基础回滚策略

### M6：扩展能力验证

- Runtime Registry
- Runtime Adapter 契约测试
- Profile / Device Adapter 契约测试
- 一个不具备完整 Shell 的模拟设备 Profile
- 验证新增 Runtime 和设备不会改变既有 Task、Approval、Event 和 Evidence 语义

## 14. 待确认问题

以下问题会影响后续技术方案，暂不在本草案中替用户做最终决定：

1. 一期首先适配 Codex 还是 Claude，还是先做统一 Adapter 接口后同时适配？
2. Orchestrator 是否继续由外部上游 AI Runtime 承担，还是增加独立的本地编排服务？
3. 本地 Runtime 是否能够提供可拦截的工具调用事件；如果不能，一期是否接受 `limited_safety` 模式？
4. 一期优先实现健康检查、故障诊断还是计划维护？
5. 是否允许用户在设备本地保存 Runtime 凭据，Gateway 只保存身份和任务元数据？
6. 一期的设备 Agent 是否需要提供后台服务安装脚本，还是先支持前台运行？
7. 风险证据默认保留多久，是否需要导出为 JSON 或审计报告？

## 15. 产品边界总结

一期产品不是：

- 远程桌面
- 通用 Shell 沙箱
- 手机管理平台
- 任意 MCP Server 聚合器
- 多 Agent 聊天室
- 自动替用户承担全部生产风险的黑盒系统

一期产品是：

> 一个以 Remote Agent Gateway 为安全边界、以本地 Codex/Claude 为执行 Agent、以结构化任务和 Action 为协作协议、面向 Windows/macOS/Linux 的跨设备 AI 运维与风险监控平台。

它同时保留清晰的扩展边界：

> 后续可以接入更多本地 AI Runtime、更多设备类型和更多硬件能力，而不需要重写 Gateway 的任务、权限、风险和审计核心。
