# 一期验收：本地 Runtime 只读健康检查

## 验收边界

操作者只能选择自己已注册的在线设备。每台设备只执行
`system_observe.v1` 的 `get_host_info`、`get_resources`、`get_processes`、
`get_network`，不接收参数，不执行远程网络探测、Shell 指令、文件修改或自动修复。
原生模式不需要模型。Codex 模式不静默回退，缺少任一有效观察或 Codex 未完成 turn 时，
该设备不产生成功证据；其它成功设备的证据保留。

Codex 首版仅验证 CLI `0.169.0`。配置专用 `CODEX_HOME`，不要复用日常 Codex
目录、项目工作区、第三方 MCP、插件、Hook 或自定义指令。建议用专用低权限
OS 账号；`managed_limited` 不是操作系统沙箱。模型服务仅收到固定提示词、
系统类型、资源百分比和计数，不收到设备凭据、任务标题、主机名、地址、
磁盘路径或进程名称。原始观察摘要仅通过设备连接
返回网关、供该设备所有者查看。原生观察可报告当前 Windows 管理员 / POSIX root 状态，以及
Linux 上 `sudo`、Docker、Podman 是否在 PATH，不尝试提权或连接守护进程。网络观察只读取本机接口
与 TCP 监听表，不扫描地址、不探测远程端口。macOS 可从固定系统接口估算内存，输出缺失或
不可解析时只记录可验证的总量。缺少内存利用率会生成观测缺失告警，不代表设备故障。

## 真设备验收

1. 启动单实例 Gateway。同机验证可在安装依赖后运行 `python -m gateway`，默认只监听
   `127.0.0.1:8000`；先确认 `/api/health` 返回 HTTP `200`，再用浏览器打开管理端。
   跨物理设备时按 [README](../README.md) 配置目标设备可达的 HTTPS/WSS 地址，
   不要把测试数据库或注册凭据暴露给公网。
2. 登录管理端，在「Agent 工作台」装配包含 `system_observe.v1` 的 Python Agent，
   每台设备独立下载 ZIP 并解压，按包内说明安装依赖。使用已验证版本的原生
   Codex 可执行文件（Windows 用 `codex.exe`，不要用 `.cmd` 启动器）。
3. 在每台测试设备上创建本地专用 Codex 目录，并用测试账号或测试模型凭据完成
   该目录下的 Codex 登录。不要把任何凭据写入项目仓库。用工作台给出的首次启动命令，
   在末尾加上 `--codex-bin` 和 `--codex-home`。例如：

   ```powershell
   $codexBin = 'C:\path\to\codex.exe'
   $env:CODEX_HOME = "$HOME\.rag-codex-health"
   New-Item -ItemType Directory -Force -Path $env:CODEX_HOME | Out-Null
   & $codexBin --version
   & $codexBin login
   python run.py --gateway '<Agent WS/WSS 地址>' --token '<本设备注册令牌>' --codex-bin $codexBin --codex-home $env:CODEX_HOME
   ```

   ```sh
   CODEX_BIN=/absolute/path/to/codex
   export CODEX_HOME="$HOME/.rag-codex-health"
   mkdir -p "$CODEX_HOME"
   "$CODEX_BIN" --version
   "$CODEX_BIN" login
   python3 run.py --gateway '<Agent WS/WSS 地址>' --token '<本设备注册令牌>' \
     --codex-bin "$CODEX_BIN" --codex-home "$CODEX_HOME"
   ```

   首次注册后使用同一设备配置，重启时去掉 `--token`。浏览器中 Agent 应在线；
   `GET /api/agents` 应显示 `task_protocol: 1`、`action_interception: "limited"`、
   `runtimes[].provider_id: "codex"` 及 `mode: "managed_limited"`。这些字段表示
   已启用适配器，不代表实际模型身份或连通性已经验证。
4. 打开「健康检查 → 开始健康检查」，选择 Codex 和一台或多台在线设备。也可
   用已登录会话执行 `POST /api/tasks`，请求体如下，预期 HTTP `202`：

   ```json
   {"kind":"health_check","agent_ids":["<自有设备 ID>"],"requested_runtime":"codex"}
   ```

5. 在「事件与证据」或下列 API 验证同一个 Task ID：

   | 查询 | 必须看到 |
   | --- | --- |
   | `GET /api/tasks/{id}` | `requested_runtime: "codex"`；成功设备四步完成；任务为 `completed` 或有其它失败设备时 `partially_completed` |
   | `GET /api/tasks/{id}/events` | 每个成功设备先有 `agent.runtime_started`，随后四个 `action.requested` 均为 L0 `allow`，四个 `agent.action_completed`，最后 `agent.runtime_finished` |
   | `GET /api/tasks/{id}/evidence` | 每个成功设备有一份证据及 SHA-256、四类带 UTC 时间的观察和 Codex `runtime.session_id` |
   | `GET /api/tasks/{id}/alerts` | 有阈值超限或观测缺失时展示对应告警；不自动修改系统 |

6. 验证拒绝与故障：对未开启 Codex 的设备显式选择 Codex 应返回 `422`；
   另一账号不能创建针对该设备的任务，也不能查看其证据。用正在运行的测试任务点击
   「取消」，或调用 `POST /api/tasks/{id}/cancel`，确认设备终止会话、不再开始新动作。
   断开多设备任务中的一台设备，检查断开设备失败、其它设备的证据保留；重连后手动
   建立新任务，不续跑此前的 turn。

新版 Agent 声明 `health_step_cancel` 时，可在健康任务详情取消该设备的某个观察。
其它观察仍可完成；不完整的设备只保留 `health_check_partial` 证据，不计入健康通过。
要对同一 Gateway 的 Windows、macOS、Linux 一次复跑，参见[三台设备一次复跑](PHASE1-ACCEPTANCE.md#三台设备一次复跑)。

## 第二引擎：Claude Code

Codex 仍是首选。Claude Code 使用单独的受限打印模式：固定提示词只给出四个观察名称，
模型输出必须恰好给出这四个名称各一次、不带参数。设备按顺序请求 Broker，采集数据和结果
校验由 Agent/Gateway 完成，不回传设备观察内容给 Claude。未启用、版本不匹配、越界输出、
模型失败或缺少任一已验证观察，都不能算作完成；不会回退到其它引擎。

1. 使用启用 `system_observe.v1` 的新 ZIP。由设备所有者准备可信、位于工作区外的
   Claude Code `2.1.306` 原生文件（Windows 用 `claude.exe`），选择账号有权访问的具体模型 ID。
2. 给设备 Agent 进程注入专用 `RAG_CLAUDE_API_KEY`，不要使用设备凭据、日常 Claude 会话、
   通用环境里的 API Key，或把密钥写入命令行、启动模板、项目或验收报告。适配器只将专用密钥
   交给本地 CLI，并使用 Anthropic API；请使用专用标准系统账号。`managed_limited` 不是 OS 沙箱。
3. 保持已注册的 `--config` 和相同的工作区，在启动参数中加 `--claude-bin <绝对路径> --claude-model <模型 ID>`。
   可与设备原有 Codex 参数并存。「健康检查」应显示 `Claude Code（受限只读）`。
4. 明确选择 Claude 或发送 `requested_runtime: "claude"`。检查目录声明与会话证据中的
   `provider_id: "claude"`、`runtime_version: "2.1.306"`、`adapter_version: "0.1.0"`、
   `mode: "managed_limited"`，以及四个独立 L0 决策。三台现场验收用 `--verify-claude` 启用第三轮。
5. 要在本地自动化验收，把 `RAG_TEST_CLAUDE` 指向同一版本原生可执行文件，再运行
   `python -m unittest tests.test_health_check_e2e -v`。测试固定使用回环替身与合成凭据，
   包含未知观察拒绝和模型请求中取消；不需要且不使用真实密钥。

## 自动化复跑

从仓库根目录运行，使用 Python 3.11 或以上。先把虚拟环境加入 `PATH`；
仅用绝对路径运行虚拟环境的 Python 不够，旧 MCP 回归用例还会在设备 Shell 中调用 `python`。

Windows PowerShell：

```powershell
python -m venv .venv
$env:Path = "$(Join-Path $PWD '.venv\Scripts');$env:Path"
$env:RAG_TEST_CODEX = 'C:\path\to\codex.exe'
& $env:RAG_TEST_CODEX --version
```

macOS、Linux 或 WSL（不与 Windows 共用虚拟环境）：

```sh
python3 -m venv "$HOME/.venvs/rag-health-check"
. "$HOME/.venvs/rag-health-check/bin/activate"
export RAG_TEST_CODEX=/absolute/path/to/codex
"$RAG_TEST_CODEX" --version
```

确认输出为 `codex-cli 0.169.0`。WSL 应使用 Linux 原生二进制，不要使用 Windows 的 `codex.exe` 或 `.cmd`。接着执行：

```text
python -m pip install -e .
python -m unittest tests.test_codex_runtime tests.test_task_runtime tests.test_health_check_service tests.test_health_check_e2e -v
python -m unittest discover -q
node --check gateway/static/app.js
git diff --check
```

`tests.test_health_check_e2e` 通过本地 HTTP 组装并下载真实 Agent，注册设备并跑
WebSocket Task、网关策略、四个系统观察和 SQLite 证据。启用 `RAG_TEST_CODEX` 后，
还会通过真实 Codex app-server 请求 loopback 模拟模型，验证完整工具调用、
模型遗漏动作时失败、取消及审计，以及发给模型的工具和数据投影。
该测试不读取个人 Codex 配置、不需要实际模型账号；未设置变量时
只跳过真实 Codex 用例，原生真实 Agent 用例仍运行。

`.github/workflows/test.yml` 为 Linux、Windows、macOS 配置相同的真实二进制
验证。只有对应 runner 实际通过，才能标记对应平台的自动化验收通过；
实际模型登录与两台物理设备联调仍按「真设备验收」单独签收。

## 一期完整签收

本手册只签收首个 **L0 健康检查** 场景。人工审批、计划变更失效、批准后执行、
文件校验及三平台签收门槛见 [一期验收](PHASE1-ACCEPTANCE.md)。
旧 Shell/Workspace MCP 工具仍按原有路径运行，不受健康检查或受控计划的策略覆盖。

## 本轮实测（2026-11-23）

- Windows：真实 Codex CLI `0.169.0` 与回环模拟模型。沙箱外全量 107 项通过，包含旧 Shell/Workspace 和新增四个只读工具的真实 MCP 调用；本轮复跑健康检查端到端 2 项也通过，无跳过。既有 `taskkill` 超时用例在沙箱内受进程清理限制，沙箱外已通过。
- Linux（Ubuntu WSL，x86-64，Python 3.12.3）：使用 Linux 原生 Codex CLI `0.169.0` 与回环模拟模型，健康检查端到端 2 项、全量 107 项均通过，无跳过；旧 MCP 实链路和 Python 编译也通过。
- 同机双 Agent：Windows 和 Ubuntu WSL 分别运行原生 Codex，经同一 Gateway 完成同一个健康任务。核对了 8 个允许的观察、2 份证据和 2 个不同的 Codex 会话。这不是两台物理设备的现场验收。
- JavaScript 语法、静态首页 HTTP 和 `git diff --check` 通过。内置浏览器未能连接临时服务，浏览器点击验收未完成。
- macOS runner、真实模型账号及不同物理设备联调尚未验收，不计为通过。
