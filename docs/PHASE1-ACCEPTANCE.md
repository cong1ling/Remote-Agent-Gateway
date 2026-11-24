# 一期验收：跨设备 AI 运维与风险控制

一期交付范围以 [PRD 一期要求和 MVP 验收](PRD-cross-device-ai-ops.md#11-一期范围) 为准。
这里把可自动化复跑、人工操作和现场验收分开记录。CI 已配置不等于对应平台已验收。

## 1. 从本地启动到端到端验收

按[健康检查运行手册](PHASE1-HEALTH-CHECK-ACCEPTANCE.md#自动化复跑)准备 Python 虚拟环境、Git、Node
与当前平台的原生 Codex CLI `0.169.0` 和 Claude Code `2.1.306`，分别设置 `RAG_TEST_CODEX`
和 `RAG_TEST_CLAUDE`，依次在 Windows、Linux 和 macOS 执行。先激活虚拟环境（或将其目录加入 `PATH`），确保 Agent 启动的测试 Shell
和测试进程使用同一个 `python`：

```text
python -m pip install -e .
python -m unittest tests.test_agent_hub tests.test_agent_builder tests.test_mcp_api tests.test_health_check_service tests.test_health_check_e2e tests.test_claude_runtime tests.test_operation_plan_service tests.test_operation_plan_e2e tests.test_build_verify_profile tests.test_managed_resources_e2e tests.test_phase1_site_acceptance -v
python -m unittest discover -q
python -m compileall -q gateway remote_agent tests agent.py
node --check gateway/static/app.js
node --check tests/browser_plan_smoke.mjs
git diff --check
```

- 健康检查链路：`Web API → Task → 设备原生 / Codex / Claude → Gateway Broker 逐项放行 → 四个 L0 观察 → 事件和证据`。
  端到端包括原生、Codex 和 Claude Code 三种方式，两个模型 CLI 使用本地替身，不需要登录。
  三个用例都应为 `ok`，不能跳过引擎用例。Claude Code 不开放原生工具，只能建议固定观察；
  未知请求不会触达设备观察，取消在途模型请求后仍无健康证据。
  定向验收还会取消 Codex 的一个待执行观察，核对其它观察仍有证据，且不生成完整健康结论。
- 受控计划链路：`创建计划 → 人工审批 → 一次性执行 → 两个真实 Agent → 文件读回校验 → 事件、证据、告警`。
  测试用新的工作台 ZIP、一次性注册令牌和设备 WebSocket；确认未批准、已过期、异账号、旧版本都不能执行，
  同一授权不得重复使用，重启不重放，事件可按游标续读。用真实 MCP 检查限定工具列表、不同 Key 隔离、
  L1 设备路径放行、L2 始终由人审批执行及撤销后无写入。
- 应用、构建与维护链路：`计划型 MCP → 本人登录批准并启动 → 设备端 Broker → 真实应用 / 固定 Python 单测 →
  结构化验证 → SQLite 时间线和证据`。验证未审批时应用进程、配置都不变，确认就绪探针从 `503` 变为 `200`；
  不同源码包不执行，匹配包在两个独立 Agent 上分别产生验证证据；某批次失败后不派发其后的独立步骤。
  取消或超时后验证 Runner 已退出且临时目录已删除；Windows 短暂的文件共享冲突只重试目录清理，不重新运行构建。
- 兼容链路：全量用例必须覆盖既有 MCP Shell/Workspace 实际调用、Task 取消和部分失败。
  Windows 进程树清理应在能够运行系统进程清理工具的普通终端验证，不能以跳过用例替代。
  macOS 需实际通过 `test_native_macos_reports_resources_processes_and_a_local_listener`，确认磁盘、
  CPU 负载、`vm_stat` 内存估算、真实进程表和测试监听端口都可观察，不能只用固定解析样本替代。
- 设备目录、停机和告警链路：验证旧数据库迁移和旧 Agent、同标签跨账号隔离、
  选择器锁定设备 ID、停机时最终派发门禁、恢复后旧审批不能重用、仅中断命中的在途请求，
  以及断线告警、重连自动解决和人工状态迁移互不越权。
- 定时巡检链路：`所有者授权 → 原生 L0 Broker → 证据摘要 → 变化与连续失败告警`。
  自动化覆盖实际原生 Agent 的首次巡检；跨周期和时间边界由定向测试推进时钟，不需等待 15 分钟。
  检查默认不启用、禁用后不启动、停机解除后需重新授权、过期停止、重启跳过旧周期及监测数据最小化。

要加跑真实桌面浏览器，准备 Playwright 和其 Chromium 浏览器，设置
`RAG_BROWSER_PLAYWRIGHT` 为 Playwright 包的 `index.mjs` 绝对路径，再运行
`python -m unittest tests.test_operation_plan_e2e.OperationPlanE2ETests -v`。已经准备的 Playwright 版本和
无头 Chromium 必须匹配，可用 `node <Playwright 包目录>/cli.js install chromium --only-shell` 安装。
如果安装时设置了 `PLAYWRIGHT_BROWSERS_PATH`，运行测试时沿用同一个值。用例运行在 `127.0.0.1`，
仅使用本次测试创建的临时账号和数据库。应打印 Chromium 版本及设备目录筛选、健康任务、安全控制、
巡检启停、告警处理记录、创建受限 Key、设备路径策略、L2 审批、维护窗口、执行与证据展示结果；
不设置该变量只验证 API 和设备，不算浏览器通过。

## 2. 人工复跑：审批后才修改文件

1. 用临时数据库运行 Gateway，登录测试账号。在工作台分别组装 `workspace.v1` 与 `shell.v1` Agent，
   在两个不同的测试工作目录启动。只将测试文件放入工作目录；不要在生产目录、管理员账号或含凭据的目录测试。
   `GET /api/agents` 中两台目标设备都应在线，声明 `task_protocol: 1` 和 `planned_actions: true`。
2. 在 Workspace 的工作目录创建 UTF-8 `demo.ini`，写一行 `level=notice`，用
   `Get-FileHash -Algorithm SHA256 .\demo.ini`（Windows）、`shasum -a 256 demo.ini`（macOS）或
   `sha256sum demo.ini`（Linux）取得实际字节的 SHA-256，并转成小写；也可单独执行 `workspace.hash`。
3. 打开「任务计划 → 新建计划」。输入目标，粘贴下面的步骤数组；把标记替换为实际设备 ID 与哈希。
   如用 API，将数组放入 `POST /api/task-plans` 的 `steps`，并提供 `objective`。建议先用同机两个 Agent 验证。

   ```json
   [
     {"key":"observe","agent_id":"<Workspace Agent ID>","operation":"workspace.hash","params":{"path":"demo.ini"}},
     {"key":"edit","agent_id":"<Workspace Agent ID>","operation":"workspace.edit","depends_on":["observe"],
      "params":{"path":"demo.ini","expected_sha256":"<64 位小写 SHA-256>","old_text":"level=notice","new_text":"level=info"}},
     {"key":"check","agent_id":"<Shell Agent ID>","operation":"shell.version","depends_on":["edit"],"params":{"tool":"git"}}
   ]
   ```

4. 预览应显示目标设备、工作目录范围、步骤依赖、精确输入、L2、执行时限和计划哈希。
   批准前执行 API 应返回 `409`，文件不变。批准第一版后，先「修订计划」改目标或修改后的值；
   旧计划哈希不得再启动任务。对新版本点击「批准此版本」，再点击「执行此版本」。
5. 任务应变为 `completed`。观察三个步骤按依赖完成：摘要、文件编辑与读回、另一 Agent 的版本检查。
   核对文件字节、三份证据、编辑的前后 SHA-256 和批准片段的 diff，以及同一个 Task 的审批、授权、验证时间线。
   改为「拒绝」复跑时不得出现任何设备授权；设备断线、重连、摘要冲突或取消后，不应自动重试。

| 接口 | 核对项 |
| --- | --- |
| `GET /api/tasks/{id}` | `plan.hash`、修订、设备/步骤、审批人、审批有效期和最终状态 |
| `PUT /api/tasks/{id}/plan` | body 带旧 `plan_hash` 和新 `plan`，旧审批失效 |
| `POST /api/tasks/{id}/approval` | 登录会话提交 `plan_hash` 与 `decision: "approve"` 或 `"deny"` |
| `POST /api/tasks/{id}/execute` | 提交当前 `plan_hash`，首次成功为 `202`，重放应为 `409` |
| `GET /api/tasks/{id}/events/live` | SSE 事件有序，`Last-Event-ID` 可续读；完成后收到 `task_finished` |
| `GET /api/tasks/{id}/evidence`、`/alerts` | 只有已验证动作产生成功证据；失败测试可有 `operation_plan_attempt` 和告警，不计作成功 |
| `POST /api/tasks/{id}/cancel`、`/steps/{step_id}/cancel` | 取消任务或单个步骤；已经产生的副作用不承诺回滚 |

人工审批和人工批准计划的启动需要网关登录会话，MCP Key 和设备凭据不能代替。L0 免审批；
L1 文本默认需要人工审批，只有设备所有者设置的精确路径允许免审；L2 配置始终需要人工审批。
敏感路径和不在目录内的命令不予派发。
受控动作复用 `workspace.hash/create/edit` 与 `shell.version`。额外动作只面向设备侧登记的应用和源码包，
不会接受任意 Shell、任意服务名、URL 或依赖安装命令。
计划文本只是用户报审材料，不上传给模型；普通事件记录摘要而不是文本。不要在计划中输入明文凭据。
设备应使用低权限账号、可信本地工具和隔离工作目录；这些限制不是操作系统沙箱。

## 3. 主控 Agent 复跑：用计划型 MCP Key

1. 在「MCP Keys → 创建 MCP Key」选择「仅受控计划」。把该 Key 配给主控 Agent，不要给它旧版直连 Key；
   已有 Key 或旧 API 省略 `scope` 时仍为直连权限。API 显式创建受限 Key 的 body 为
   `{"name":"运维主控","scope":"plan"}`。无论哪种 Key，都不具备网页登录权限。
2. MCP 客户端连接 `/mcp/`，使用 `Authorization: Bearer <测试 Key>`，调用 `tools/list`。
   应只有 `list_agents`、`get_agent_capabilities`、`create_operation_plan`、
   `get_operation_plan_status`、`start_operation_plan`。即使客户端手动调用 `remote_exec` 或 `remote_write_file`，
   网关也拒绝，不向设备转发。旧版直连 Key 维持原有工具清单和行为，不受计划策略约束。
3. 在新组装的 `workspace.v1` 设备上，用 `create_operation_plan({plan: {objective, steps}})` 提交
   `workspace.create`，例如 `params: {"path":"demo-note.txt","content":"fixture\n"}`；也可附加第二台设备的版本
   检查和 `depends_on`。此时计划应为 `waiting_approval`，启动被拒绝且测试文件不存在。
4. 到「远程 Agent → 能力 → 配置 L1 路径」添加 `demo-note.txt`。设备须声明 `planned_l1_policy`。
   路径按设备和字符串精确匹配，最多 16 条，无通配符；拒绝路径逃逸、敏感文件名和非 `.txt`/`.md` 后缀。
   重新提交后应为 `approved` 且 `requires_approval: false`，此状态不代表有人签批；提交工具不会自动启动。
   以同一 Key 调用 `start_operation_plan({task_id, plan_hash})`，再用
   `get_operation_plan_status({task_id})` 核对两个步骤完成。审计应显示 `operation_plan.device_l1_path`、
   策略修订和已验证读回。另一把 Key 即使同账号也不能直接读取或启动该计划。
5. 再提交一个免审草稿，清空设备路径后应不能启动旧哈希；重新添加同一路径也不能恢复旧租约。
   授权在派发和放行 Broker 时复查；撤销不能回滚已发放许可并执行的写入。把目标改成 `demo.ini` 会进入 L2；
   即使人已批准，主控 Key 也不能启动，须回管理端执行。撤销 Key 后，新 MCP 请求应返回 `401`。

## 4. 复跑应用恢复、同源构建和维护窗口

从已安装依赖的仓库根目录运行以下单个用例。它不依赖个人登录、Codex、生产环境或外置测试设备：

```text
python -m unittest tests.test_managed_resources_e2e -v
```

该用例启用临时回环 Gateway、测试账号、两个工作台 ZIP Agent 进程和一个独立测试应用，并输出相应 Task ID。
数据在用例结束时清理。可据此复跑到以下检查点：

| 链路 | 操作 | 预期结果 |
| --- | --- | --- |
| 应用诊断 | 用计划型 Key 提交 `service.inspect`，传 `{"service_id":"demo"}`，启动 L0 计划 | `degraded`、`config_invalid` 和风险告警；应用 `/ready` 为 `503` |
| 审批恢复 | 按配置 SHA-256 编辑 `service.ini`；随后 `service.restart` 绑定 `service_id` 与 `expected_generation`；最后 `service.inspect` | 未审批无写入、无重启；批准不等于执行。本人启动后检查文件读回、新代际、`verified: true`、独立 `/ready` 为 `200`；重用旧代际不得再重启 |
| 源码交接 | A、B 两端 `build.source` 核对相同的 `source_sha256`，A 的 `build.verify` 通过后才到 B 的 `build.verify` | 两份证据分别归属 A/B 且摘要一致，编译成功、实际运行的测试通过。B 换包时得到 `build.source_mismatch`，不运行测试 |
| 批次停止 | 在维护窗口内启动一个含两设备的已批准计划，其中首批某个测试失败 | 保留 `operation_plan_attempt` 和结构化失败计数，时间线出现 `maintenance.stopped`；后续批次没有 `action.authorized` |

如需在现场手动操作，不要对生产服务使用测试配置：

1. 设备 A 从工作台组装 `workspace.v1 + service_control.v1 + build_verify.v1`，设备 B 组装 `build_verify.v1`，
   各用各自的启动令牌和独立、低权限 OS 账号。托管 Profile 不得与 `shell.v1` 混装。
2. A 的工作目录只放测试配置（`service.ini` 首行 `mode=broken`）；在隔离终端运行
   `python -m tests.managed_service_fixture supervisor <service.ini 绝对路径> <工作区外的 services.json 绝对路径>`。
   记录它输出的本机就绪地址；启动 A 时需带 `--service-config <services.json 绝对路径>`。应用接口契约见
   [Profile 开发规范](../PROFILE_DEVELOPMENT.md#受控计划专用-profile)。
3. 把同一份**可信**、无需第三方依赖、根目录含 `tests/` 的 Python 源码 ZIP 放到 A、B 工作区之外。在两份工作台启动命令后加
   `--build-archive <本设备源码 ZIP 的绝对路径>`；A 仍需带 `--service-config`。首次启动前也可先用本节自动化夹具验证。
4. 在「任务计划」或固定 MCP 工具中按上表创建步骤，`depends_on` 填前序的 `key`。检查两台设备 ID 和摘要或服务代际，再登录
   批准并执行。要测试维护窗口，在页面输入本地起止时间；API 用 `maintenance.starts_at`、
   `maintenance.ends_at`（Unix 秒）和 `maintenance.max_concurrency`（1–5）。
   审批绑定预览批次；窗口外启动为 `409`，窗口内也不会自动启动，单批每设备最多一步。复查同一 Task ID 的步骤、事件和证据。
5. 替换 B 端的预置 ZIP 后重新提交绑定旧摘要的验证计划，检查无成功证据；另用真的失败测试包验证停批次。
   每次修改时间窗、对象或内容须重新预览并审批；不自动重试或回滚已执行动作。

### 同一网关验证 Windows + WSL

准备 Windows 和 WSL 两套独立 Python 3.11+ 环境，都安装项目依赖。镜像网络模式下，在 WSL 的仓库根目录运行：

```sh
python -m tests.wsl_mixed_acceptance /mnt/d/vibe-coding/Remote-Agent-Gateway/.venv
```

参数是 **Windows** 虚拟环境的 WSL 挂载路径，按实际安装位置替换。脚本使用同一个回环 Gateway
组装两份工作台 ZIP，分别启动 Linux 和 Windows Agent，不改系统网络设置，不启动个人 Codex。
预期输出 `Mixed OS acceptance`，同时包含两端健康检查 Task ID、人审后同源验证 Task ID 和共同 SHA-256。

## 5. 复跑目录、停机与告警

1. 用临时数据库、两个自有测试设备和两个账号。登录所有者，在「远程 Agent」为设备 A
   设置环境 `staging` 与标签 `canary, west`，在目录筛选对应平台、环境、标签和能力。
   `GET /api/agents?environment=staging&tags=canary&tags=west&capability=system_observe.v1`
   应只返回匹配且归本人的设备。计划型或旧 Key 均可调用 MCP `list_agents`，参数如下：

   ```json
   {"filters":{"environment":"staging","tags":["canary","west"],"capability":"system_observe.v1"}}
   ```

2. 从网页健康检查选择设备，或提交 `POST /api/tasks`：

   ```json
   {"kind":"health_check","target_selector":{"environment":"staging","tags":["canary"],"capability":"system_observe.v1"}}
   ```

   `202` 的任务 `target_selector.agent_ids` 必须是具体 ID；任务创建后编辑标签不改变目标。
   明确选 Codex 时另传 `requested_runtime: "codex"`。非法筛选、空选择器、没有符合资格的设备
   应拒绝，不扩大到所有设备。没有新版设备声明的旧 Agent 只用可信 Profile 中带平台的字段；
   无可用字段时平台为 `unknown`，不猜 OS 版本。
3. 新建并批准一份只修改测试文件的 L2 计划，不执行。在「安全控制」暂停「工作区写入与编辑」
   或暂停设备 A，执行旧版本应返回 `409`，文件保持原字节；恢复后重试旧版本仍为 `409`。
   再验证另一个账号不能暂停 A，暂停 Codex 不影响 A 的原生健康检查，设备暂停时心跳仍可见。
   动作类别只匹配列出的受控或直连入口；暂停工作区操作不限制旧版 Shell 的 OS 权限。
   需要阻止所有入口时暂停设备、整个账号或实例。
4. 仅在隔离测试实例，由能访问该实例数据库的 Gateway 主机管理员运行：

   ```text
   python -m gateway execution-stop status
   python -m gateway execution-stop pause
   python -m gateway execution-stop status
   python -m gateway execution-stop resume
   ```

   确认本命令的 `GATEWAY_DATABASE` 与正在运行的单进程实例是同一绝对路径。熔断时
   所有账号的新派发应被拦截，`/api/health`、登录、心跳和事件仍可访问。对在途执行，
   网关会请求取消，`interrupted` 是本次命中的请求数，不是终止确认。到设备检查进程和实际状态；
   未确认结果不自动重试，已提交副作用不回滚。
5. 在「风险告警」检查上述拦截含目标、动作、依据和处理建议，尝试确认及标记解决，历史应记录
   状态和处理人。「已解决」不等于设备完成回滚，也不会改变执行策略或计划审批。
   停止测试 Agent 触发离线告警，重连后核对此条告警自动解决；再核对版本变化只记组件类别。
   告警也可通过 `GET /api/alerts?status=blocked`、`GET /api/alerts/{id}` 和
   `PATCH /api/alerts/{id}`（`previous_status` 加 `status`）复核。未授权账号返回 `404`，
   旧状态提交返回 `409`，不能经告警接口写入 `approved`。

### 定时巡检与单步取消

1. 用最新版工作台 ZIP 注册专用测试 Agent。管理端「健康检查 → 定时只读巡检 → 授权巡检」，
   为单个测试设备选择 15、30 或 60 分钟。默认没有授权；每次授权最多有效 24 小时，实例同时
   最多执行四份巡检，一个账号最多同时授权八台。新授权会清空该设备的巡检比较基线。
2. 刷新页面，打开「最近巡检」，核对固定四个原生观察、`requested_runtime: null`、证据类型
   `health_monitor`。证据只包含平台、摘要指纹与计数或指标，不包括原始主机名、地址、磁盘路径或进程名。
   告警只记变化类别和必要的计数。进程计数比较只使用两次未截断、可用的采样。
3. 在「安全控制」暂停本账号的「定时只读巡检」后恢复。旧授权应保持「停机后需重新授权」，
   手动发起的原生健康检查仍可用。关闭或等待过期后，巡检不再派发；已有证据不删除。
   对应 API 是 `GET /api/health-monitors` 与 `PUT /api/health-monitors/{agent_id}`，请求仅有
   `interval_minutes`（`15`、`30`、`60` 或关闭时的 `null`）。
4. 在运行中的原生或 Codex 健康任务「查看详情 → 观察步骤」取消一个观察，或使用
   `POST /api/tasks/{task_id}/steps/{step_id}/cancel`。新版 Agent 通过 `health_step_cancel`
   声明支持；旧 Agent 仍可取消整任务。已验证的其它观察可保留为 `health_check_partial`；
   取消的观察不进入证据，不生成完整健康结论。取消请求不能保证撤销已经发生的观察。

### 设备宿主机启动模板

1. 在工作台生成并下载新 ZIP。确认有 Linux systemd、Windows 登录计划任务、macOS 用户
   LaunchAgent 的示例，以及包内的路径和前台注册说明。
2. 在每个目标平台用专用标准账号按包内 README，用同一份 `--config` 和 `--workdir` 前台注册，
   确认在线后停止前台进程。若启用 Runtime 或登记服务、构建 Profile，模板也要补上对应本地路径参数。
   保持启动程序、配置与预置资源在可写工作区之外，勿把令牌、密码写在模板里。
3. 人工审核后才在隔离验收设备启用模板，注销再登入或重启相应会话。用管理端核对同一设备 ID
   重新在线，再跑一轮健康检查；停止或卸载模板后确认不再自动启动。打包和结构测试不代表本步骤实测。

在隔离环境可先验证启动管理器能实际运行 ZIP 中的 Agent，而不启用自动启动：

```powershell
# Windows：在已登录、非管理员的测试终端执行，临时任务禁用登录触发器
python -m tests.windows_startup_acceptance --confirm-temporary-task

# Ubuntu WSL：仅在授权的测试实例，需 systemd 和没有其它进程的标准 nobody 测试账号
wsl.exe -d Ubuntu --user root --exec /usr/bin/env -i PATH=/usr/bin:/bin HOME=/nonexistent LANG=C.UTF-8 /usr/bin/python3 -IBS /mnt/d/vibe-coding/Remote-Agent-Gateway/tests/wsl_startup_acceptance.py --test-user nobody
```

Windows 应报告两次启动、同一个注册 ID 和两次健康任务，再删除临时任务。WSL 记录 systemd
临时单元实际的启动、重启和停止、低权限 UID、健康证据 SHA-256 与临时路径清理；
将命令里的仓库挂载路径替换为本地路径。

在已登录的原生 macOS 专用标准账号终端，安装项目依赖后执行：

```sh
python -m unittest tests.test_system_observe_profile.SystemObserveProfileTests.test_native_macos_reports_resources_processes_and_a_local_listener -v
python -m tests.macos_startup_acceptance --confirm-temporary-launchagent
```

第一条必须为 `ok`，跳过或只通过样本解析均不算原生验收。第二条只在当前用户 GUI 会话从临时目录
加载 ZIP 带的 LaunchAgent，不写入 `~/Library/LaunchAgents`，
不需要 `sudo`、外置 Gateway 或模型账号。预期输出 JSON：两轮健康 Task ID 和证据 SHA-256、
同一 Agent ID、`temporary_job_removed: true` 和 `login_trigger_tested: false`。每轮卸载后必须观察到
设备离线，且本地设备凭据不变。三种按需验证均不等于实际登录或重启自动启动测试。

## 6. 现场签收与差距

| 范围 | 签收依据 |
| --- | --- |
| 三平台接入、能力与版本 | 同一源码的三个 CI runner 或原生实测结果；同一网关看到三种 OS 的真实设备、Profile 和版本 |
| Runtime 与代理协议 | Codex 为主、Claude Code 为第二引擎；两者都做真实 CLI 回环测试，再用专用账号向真实模型复验。手机和硬件不属于一期 |
| 计划、审批、策略与复用 | Web 人工审批与 MCP 主控提交均有实链路；核对 L1 按设备路径授权、L2 审批、旧版失效、拒绝不派发、Workspace 读回和固定 Shell 动作 |
| 可靠性、事件与证据 | 按 Task ID 对照权限/取消/过期、断线、熔断后不续跑、部分完成和数据摘要；验证证据不是仅凭模型结论生成 |
| 最终人工验收 | 三平台实体设备、实际模型账号、桌面浏览器审批和证据展示，以及 PRD 剩余缺项逐项签收 |

现场复跑用自有设备、专用低权限账号和设备可达的 HTTPS/WSS 地址。每台平台设备独立注册，
不共享设备凭据。真实 Codex 配置与健康动作见[健康检查真设备验收](PHASE1-HEALTH-CHECK-ACCEPTANCE.md#真设备验收)。
登记提交版本或源码快照、OS/Python/Codex 版本、用例结果和 Task ID；不要上传凭据或真实配置文件内容。

### 三台设备一次复跑

1. 在同一 Gateway 组装当前源码的 Agent 包。分别在自有 Windows、macOS 和 Linux 设备用
   独立低权限账号启动，确认管理端三台在线、具体平台无误、各自能选择 Codex 和 Claude Code。
   不要把日常数据目录或个人配置交给 Agent。现场使用专门授权的模型账号；本地替身不算真模型实测。
2. 在仓库根目录用已设置好的 Python 环境执行。下面是 PowerShell 示例；先分别将四个变量设为
   测试 Gateway 的 HTTPS origin 和自己拥有的三个 Agent ID。只有显式回环地址允许 HTTP。

   ```powershell
   python -m tests.phase1_site_acceptance --gateway "$GatewayUrl" --agent-ids "$WindowsAgentId" "$MacOSAgentId" "$LinuxAgentId" --verify-claude > "$env:TEMP/health-acceptance.json"
   ```

   用已有测试账号交互登录。密码通过隐藏输入，勿在命令行传入。脚本默认运行原生和 Codex 两轮，
   加 `--verify-claude` 时另运行 Claude Code；不发起写入、审批或部署。每轮三台都必须返回四个经 Broker 放行的
   观察、正确的设备版本和会话、可重算 SHA-256 的证据，才会以 `0` 退出并输出脱敏 JSON。
   任何签收失败时，用错误里可用的 Task ID 对照管理端；不要直接重试未确认的有副作用计划。
3. 确认三个设备确为独立物理设备、两个 CLI 请求确实使用已授权真实模型。另在隔离工作区复跑
   第 2–5 节，人工验证 L2 批准前后文件和服务状态、熔断后旧授权不复活，以及分别安装、
   重启和卸载三种启动模板。归档脱敏报告、对应源码/版本和人工签收记录。

| 签收记录 | 必填证据 |
| --- | --- |
| 平台与安装 | 同一源码版本或快照摘要；Windows、macOS、Linux 各一条，记录 OS、Python、Agent ID、Profile、Runtime。启用 `shell.v1` 的测试设备另核对 Shell 方言。 |
| 健康检查 | 原生、Codex、Claude 各一轮三设备 Task ID；每台设备的四项 Broker 决策、证据 SHA-256，真模型轮次另记录受限会话 ID。 |
| 审批与故障 | 浏览器与 MCP 的测试结果，至少留批准、拒绝、取消、熔断的关联 Task 或告警 ID；核对未执行动作没有设备侧副作用。 |
| 启动与复连 | 每个平台的启动、实际登录/开机触发、重连与卸载结果；核对仍是同一 Agent ID，记录复连之后成功的健康 Task ID。 |

### PRD §11–12 对照

| 验收项 | 当前覆盖与未签收边界 |
| --- | --- |
| §12.1 设备接入 | Windows 与 Ubuntu WSL 已同时连接同一个 Gateway，并验证平台声明、健康和同源构建证据。原生观察支持管理员/root 状态、Linux 工具存在性和本机 TCP 监听摘要。目录支持标签、环境和能力选择。macOS runner 及三平台实体设备仍需签收。 |
| §12.2 任务执行 | 网页和计划型 MCP 可提交跨设备步骤；本人登录审批后显式执行。支持任务取消及计划单步取消；声明取消能力的新 Agent 支持健康观察单步取消。 |
| §12.3 风险控制 | 普通 L0 自动允许，L1 精确文本路径可显式授权；L2、运行源码测试和维护计划需人工启动。目录外和 L3 拒绝，停机优先于审批，旧授权不会复活，模型仍只能 L0。 |
| §12.4 证据与监控 | 有任务/设备/步骤事件、文件 diff 与摘要、服务就绪、构建证据和独立告警页。监控批次使用最小化摘要，不记录原始进程名、地址或路径。不要把凭据填进计划、文件 diff 或旧版 Shell；这些自由文本不具备通用脱敏保证。 |
| §12.5 故障处理 | 超时、取消、窗口到期、失败停批、部分成功；同一计划版本只能启动一次，同一步的 Broker 授权以唯一键去重。支持设备、Runtime、动作、账号与实例停机。Gateway 重启关闭挂起任务，不自动重放；取消与写后断线均需核对实际状态。 |
| §12.6 可扩展性 | 增加 Claude Code Runtime Adapter 不改 Task、Approval、Event、Evidence 核心模型；三类新增 Profile 不改 Task 协议。目标平台真模型仍需签收；具体硬件不是一期验收项。 |
| §3 场景 B / C / D | 回环测试应用恢复、预置同源 ZIP 在双 Agent 上编译与单测验收、维护窗口与失败停批均有本地实链路；不代表支持任意服务、部署工具链或无人值守调度。 |

§11.1 的核心框架已接通：Task/Step/Action、Broker、审批、取消、事件和证据不依赖具体模型；
设备 Profile 在握手中协商，Runtime 版本在派发和会话结果中校验，三平台验收仍以实测为准。
§11.2 已覆盖只读 Profile、步数和时限预算、设备标签与选择器、维护窗口、独立告警页、
Runtime 目录、契约测试、第二个受限引擎与三平台启动示例。Windows 计划任务和 WSL systemd
已验证基本生命周期；macOS LaunchAgent 和三个目标平台的实际登录/开机启动待签收。
手机与具体硬件保持二期。

### PRD §6.7 信号边界

| 信号 | 已验证的检测或预防 | 不计作已验收 |
| --- | --- | --- |
| 设备 | 断线、重连、连接替换、Agent/Runtime 版本变化；在数据源可用时告警 CPU 负载、内存和磁盘阈值；有期限巡检比较 OS/网络指纹及未截断的进程计数。已登记服务检查就绪状态与代际，指定文件用 SHA-256 检查。 | 重连频率分析、连续性能采样、进程行为追踪、任意系统服务诊断、工作区全量漂移扫描。 |
| Agent | 任务有时限与动作预算；计划外、重复和未经授权的 Runtime 动作由 Broker 拒绝并留事件；连续三次定时巡检失败告警。 | 跨任务 Action 频率异常模型、多 Agent 通用循环识别、持续计算资源归因。 |
| 操作 | 受控动作拒绝敏感路径与越界参数，精确绑定被修改配置的文件版本，L2 人审、L3 拒绝；不开放通用命令、提权、批删、安装或上传动作。 | 不是 OS 沙箱或通用入侵检测。旧版直连 Shell 与宿主机进程不受计划策略隔离；不能监测所有凭据读取、提权、启动项、防火墙、依赖安装或网络外传。不自动修复或回滚。 |

## 7. 实测记录

### 2026-11-23：健康检查与文件审批基线

- Windows（Python 3.13.9，沙箱外）和 Ubuntu WSL（x86-64，Python 3.12.3，最新工作树的独立源码快照）
  各通过 **118 项**全量测试，无跳过。两平台均使用原生 Codex CLI `0.169.0` 与回环模拟模型，
  通过工作台 ZIP 注册两套真实 Agent 进程。没有使用个人账号或真实模型登录。
- 端到端覆盖模型只读观察、L2 审批和修订、计划型 Key 的直连拒绝和同账号 Key 隔离、L1 设备授权和撤销、
  跨设备依赖、重放拒绝、SSE 游标、文件读回 SHA-256 与 diff。另验证排队后撤销 Key 不派发、
  重启保留已验证证据且关闭挂起动作。用上一版建成的临时 SQLite 验证旧 Key 仍可校验、设备默认不免审。
- Windows Chromium `147.0.7727.15`（1440×900）完成登录、创建计划型 Key、设备 L1 策略配置、
  L2 审批、执行与证据展示；真实 Agent 写入结果由测试进程另行读回。
- Windows 和 WSL Python 编译通过；管理端和浏览器测试的 JavaScript 语法及 `git diff --check` 通过。
- macOS、实际模型账号和不同物理设备未现场签收。

### 2026-11-24：应用、构建、维护窗口与 Runtime 兼容

- Windows（Python `3.13.9`）和 Ubuntu WSL（Linux x86-64，Python `3.12.3`）各通过 **131 项**全量测试，
  无跳过。两个环境都运行原生 Codex CLI `0.169.0`，模型响应由回环测试替身提供。
- 独立应用的就绪探针实测 `503 → 200`。未审批时无写入和重启；通过后文件、应用新代际和就绪都单独核对。
  另验证摘要不匹配不会运行、测试失败记录不是成功证据、窗口外不派发，以及失败不启动后续批次。
- Windows 无头 Chromium `151.0.7922.34` 在实际管理端完成登录、创建计划型 Key、设备路径配置、维护预览、
  L2 审批、执行和证据展示。CI 工作流提供三平台自动化入口，Windows runner 同时配置了该浏览器测试。
- 单回环网关联调了 Windows 原生 Agent 和独立 WSL Agent，健康任务从两端各收到一份证据；
  人工批准后，Windows 的验证依赖 Linux 成功，双方验证同一 SHA-256。
- 本次工作机的日志：`.venv/phase1-win-final.log`、`.venv/phase1-wsl-final.log` 和
  `.venv/phase1-wsl-mixed.log`。Task ID 用于对照本次输出；隔离测试数据库在用例结束时已清理。
- **未签收**：原生 macOS、本次工作树的三平台 CI 实跑、三台实体设备联调、专用账号的真实模型调用。
  CI 配置和模拟模型均不替代这些现场证据。

### 2026-11-24：设备目录、停机、告警与启动模板

- Windows Python `3.13.9` 与 Ubuntu WSL Python `3.12.3` 对本轮实现各通过 **140 项**全量测试，
  无跳过。两平台都运行原生 Codex CLI `0.169.0`，由回环模型测试替身提供响应；不使用个人账号。
- Windows Chromium `151.0.7922.34` 验证编辑设备资料、组合筛选、创建健康任务、账号暂停与恢复、
  查看并确认风险告警、审批 L2 计划、分批执行与显示文件验证证据。
- 定向回归核对设备停机只取消命中的直连请求，禁用 Codex 不取消同设备原生任务，
  暂停后立即恢复也不会发出正在等待的 `allow`。真实双 Agent 验证实例 CLI 熔断期间
  仍可查询健康接口，且恢复后已批准的旧版本依旧不能执行；处理告警不改变该结果。
- Windows 与 Ubuntu WSL 通过同一回环 Gateway 完成健康检查与同源构建验证。
  本轮临时 Task ID：`task_1ee00f8fd0fb461fa4f6ce3db8588d55`（健康）、
  `task_7e5e042b56b746dfa9d55a4f3a6533c0`（构建）。
- 验证日志：`.venv/phase1-win-controls-alerts.log`、`.venv/phase1-wsl-controls-alerts.log`、
  `.venv/phase1-wsl-controls-alerts-mixed.log`。新 ZIP 的三平台模板只做了打包与结构校验，
  未安装服务或计划任务。原生 macOS、三平台实体设备、当前源码三平台 CI 和真模型仍待现场签收。

### 2026-11-24：巡检、单步取消与现场验收入口

- Windows Python `3.13.9` 全量 **172 项通过，无跳过**。Ubuntu WSL Python `3.12.3` 的独立源码快照
  **171 项通过，1 项跳过**（Windows 专用令牌与 IP Helper API 用例）。两端运行真实原生 Codex CLI
  `0.169.0`，模型响应使用回环替身，不使用个人账号或真模型。
- 两个平台分别核对临时回环监听器出现在本机监听表。macOS 的 `vm_stat` 与 `netstat` 解析经过
  样本测试；没有原生 macOS 运行证据。
- 真实 Codex 流程取消一个待执行观察，其它三个仍有验证证据，任务为 `partially_completed`，
  不产生完整健康报告。定向回归覆盖在途原生动作取消和已经等待发送的 `allow` 改为拒绝。
- 真实 Agent 完成首次定时巡检；定向测试核对 OS/网络和进程计数变化、三次连续失败、过期和停机
  解除后不复跑。Windows Chromium `151.0.7922.34` 实测巡检启停、停机、告警、人审 L2、
  维护窗口和文件证据呈现。
- 同一回环 Gateway 的双 Agent 测试通过：健康 `task_0c7c74e80a234ba4bf6a18aa9974382e`，
  人审后构建 `task_cae45c217a5044b6abd0f8761a664b1d`，两端验证的同源 ZIP SHA-256 为
  `1ce84eaad91a503cc77945ff779876bacd62b3cf942c899250cd9fc9c7bbb2ce`。
  本机日志见 `.venv/phase1-win-monitor-site.log`、`.venv/phase1-wsl-monitor-site.log`、
  `.venv/phase1-wsl-monitor-mixed.log`。Python 编译、管理端与浏览器用例 JS 语法、
  `git diff --check` 均通过。
- 三台设备的现场脚本已通过模拟设备的契约和 Web 进程内测试，**尚未连接三台实体设备**。
  最终签收需使用同一源码的 Windows/macOS/Linux 实机、授权真模型及实际安装的启动模板；
  本地替身、WSL、打包测试和 CI 配置均不替代此证据。

### 2026-11-24：双引擎、启动生命周期与构建清理回归

- Windows（Python `3.13.9`）全量 **178 项通过，无跳过**；Ubuntu WSL（Python `3.12.3`，独立源码快照）
  **177 项通过，1 项跳过**，跳过的是 Windows 令牌与 IP Helper API 专用用例。
  两端均跑了原生 Codex `0.169.0` 和 Claude Code `2.1.306`，模型请求只进回环测试替身。
  Claude 未知动作不触达观察，取消在途模型请求不产生成功健康证据。
- Windows Chromium `151.0.7922.34` 实测登录、受限 Key、目录筛选、巡检启停、执行闸门、告警处理、
  L2 审批、维护窗口与证据展示。独立应用恢复实测 `503 → 200`：`task_925faf20188f4e3a9b50669665995b12`。
- Windows 直接把构建解释器加入 Job，避免虚拟环境启动器先派生子进程。已退出的进程仍可能遇到短暂文件共享冲突，
  仅对该错误在半秒窗口内重试清理，不重跑测试；超窗或其它错误仍按失败处理。构建定向 **11 项通过**，
  人为延迟 Job 绑定后复跑取消与超时 **50/50 通过**，均校验解释器 PID、临时目录与请求状态。
- 同一回环 Gateway 上的 Windows 和 WSL 健康任务 `task_3b6bcc7444c64945b4d7298e22058cfb`，
  人工批准后的构建任务 `task_61f18d6b3d7748abb36dd219e0c61be4`；两端源码
  SHA-256 `6fab4a10464cd84b946e053d585a0de2b176ec1b71f457600c0fbc8aaa8e4f7f` 一致。
- Windows 计划任务按需启动两次并沿用同一注册 ID，两轮健康任务
  `task_9b182e8504a84613b8384bbaea268c00`、`task_06d47a3211d047c787c2bee60bff731f` 后停止并移除。
  WSL systemd `255.4` 用低权限测试账号启动、重启、停止临时单元，保留同一 ID，
  并清理单元与测试目录；健康任务 `task_0dc22ef793464caaa1dd4dab6e3fd0af`、`task_9deeb1e0d3614b4792e2021d3e0e77b1`。
  两轮都没有启用登录或开机触发。
- 本机日志：`.venv/phase1-win-job-final.log`、`.venv/phase1-wsl-job-final.log`、
  `.venv/phase1-win-timed-job-final.log`、`.venv/phase1-win-scheduler.log`、
  `.venv/phase1-wsl-systemd-startup.log`。Windows/WSL Python 编译、前端与浏览器用例 JS 语法、
  `git diff --check` 均通过。隔离测试数据库已经清理，Task ID 用于对照日志。
- **仍待签收**：当前源码的三平台 CI 实跑、原生 macOS、三台独立设备同网关、授权真实模型，
  以及各平台实际登录/开机启动。WSL 不等同于独立 Linux 实机，回环模型响应不等同于真实模型签收。

### 2026-11-24：macOS 实机验收入口与 POSIX 进程采集回归

- 用 WSL 临时进程 `Idle Worker` 复现旧列顺序漏报；修复后原生进程快照保留了完整进程名。
  定向回归另验证了应用路径含多个空格时的进程名、CPU 和内存字段。
- Windows **180 项通过，1 项跳过**（原生 macOS）；Ubuntu WSL 独立源码快照 **179 项通过，2 项跳过**
  （原生 macOS、Windows 专用接口）。两端使用真实 Codex 和 Claude Code CLI 与回环模型替身。
  Windows Chromium `151.0.7922.34` 的审批、执行与证据流程通过；Python 编译、JS 语法和空白检查通过。
- 同一回环 Gateway 再次完成双端健康检查 `task_046b826d0cfc4bc0a7699ca94641b791` 与人工批准的构建
  `task_308c2c196b7f4366ae1843b1a0fbb9ef`，两端源码 SHA-256
  `9ceafe49aa9de0478a11ccad5a80e924a41358371f35cb5a8e59b69072c6d635` 一致。
- Mac 专有采集有一个必须在原生系统通过的用例；新增临时 LaunchAgent 验收入口，已验证其打包模板、
  路径与凭据参数约束、禁止常驻配置及非 macOS 拒绝。**本轮没有原生 Mac，未加载 LaunchAgent，
  未验证登录自启动**；这些入口和基线均不代替 §6 的实机签收。
- 本机日志：`.venv/phase1-wsl-posix-process-name.log`、`.venv/phase1-win-macos-readiness-full.log`、
  `.venv/phase1-wsl-macos-readiness-full.log`、`.venv/phase1-wsl-macos-readiness-mixed.log`。
