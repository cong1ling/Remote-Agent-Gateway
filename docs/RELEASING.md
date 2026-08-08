# Remote Agent Gateway 发布规范

本文件是项目发版的权威操作说明，面向维护者、AI 和自动化开发者。用户提出“发布”“发包”“打版本”“打 Tag”或“创建 Release”时，必须先完整阅读本文件，再执行任何版本修改或远端发布操作。

当前自动化入口：

- `.github/workflows/test.yml`：`main` 推送、Pull Request 和手动触发时，在 Windows 与 Ubuntu 上运行验证。
- `.github/workflows/release.yml`：推送 `v*` Tag 后，在 Windows 与 Ubuntu 上复验，构建 wheel、生成 SHA-256，并创建或更新 GitHub Pre-release。

`v0.2.1` 是首个公开技术预览和 Profile 契约基线。

## 发布原则

1. Tag 是发布结果，不是测试按钮。必须先验证准备发布的提交，再推送 Tag。
2. 已推送的公开 Tag 不得移动、覆盖或复用；发现问题时发布新的修正版。
3. 自动 Workflow 负责重复执行和产物一致性，不能代替兼容性判断、版本选择和用户文案。
4. Release Notes 面向用户说明影响，不是原样粘贴提交记录。
5. 不把本地 GitHub Token、设备凭据或其他 Secret 写入仓库。Release Workflow 使用 GitHub 自动提供的最小权限令牌。
6. 工作树不干净、版本来源不一致、测试失败或兼容性结论不清楚时不得发布。

## 选择版本号

项目发布版本使用 `vMAJOR.MINOR.PATCH` Tag，Python 包和官方 Agent 使用不带 `v` 的同一版本号。

- **PATCH**：不改变公开契约的 Bug 修复、安全加固、性能改进、文档和内部实现调整，例如 `0.2.1 -> 0.2.2`。
- **MINOR**：新增向后兼容的 Profile、固定 MCP 工具或明显的新产品能力，例如 `0.2.x -> 0.3.0`。
- **Profile 主版本**：同一输入的含义、参数、结果、错误或安全语义发生破坏性变化时，必须发布新的 Profile ID，例如 `workspace.v2`。不能只升级包版本后继续沿用旧 ID。
- **协议版本**：WebSocket 消息结构或语义发生破坏性变化时，升级 `PROTOCOL_VERSION`，并明确旧 Agent 的处理方式。
- **稳定版**：`1.0.0` 和非 Pre-release 发布必须由用户明确决定，不能由自动工具自行推断。

`0.x` 技术预览仍然遵守已经公开的 Profile 契约。Technical Preview 表示尚未承诺生产级稳定性，不表示可以静默改变同名 Profile。

## 兼容性审计

确定版本号前，逐项检查本次变化：

1. Profile 的参数、返回值、错误名称、限制或安全含义是否变化。
2. 是否新增必需 Action，或者旧 Agent 无法完整实现现有 Profile。
3. MCP 工具 Schema、Profile Catalog 和远端 Handler 是否仍一致。
4. 是否需要升级 `AGENT_VERSION`、`CORE_VERSION` 或 `PROTOCOL_VERSION`。
5. 是否需要声明 `minimum_agent_version` 并要求用户重新组装 Agent。
6. 数据库、环境变量、部署方式、凭据或权限边界是否变化。
7. 旧配置、旧数据库和已注册设备是否可以继续使用。

只要用户需要执行迁移、重新组装 Agent、更换配置或重新授权，就必须写入 Release Notes 的“兼容性与升级”部分。

## 版本来源

每次发布至少检查并同步：

- `pyproject.toml` 中的 `project.version`
- `remote_agent/core/version.py` 中的 `AGENT_VERSION`
- `remote_agent/core/version.py` 中确有行为变化时才更新的 `CORE_VERSION`
- `remote_agent/core/version.py` 中确有协议破坏时才更新的 `PROTOCOL_VERSION`
- README 顶部版本徽标与“版本与兼容性”表格
- 受影响 Profile 的 `minimum_agent_version`
- 与版本或升级要求相关的测试和文案

Tag 必须严格等于 `v` 加 `project.version`。Release Workflow 会再次校验 Tag、包版本和 Agent 版本，不一致时拒绝构建发布。

## 发布前准备

先同步远端并确认发布范围：

```powershell
git fetch origin --tags
git status --short --branch
git describe --tags --abbrev=0
git log --oneline <上一个Tag>..HEAD
git diff --stat <上一个Tag>..HEAD
```

阅读实际差异，不要只根据提交标题猜测用户影响。将变化分为：

- 用户可见的新功能
- 用户可见的修复
- 安全和兼容性变化
- 部署或配置变化
- 仅内部重构、测试、文档和维护

完成版本修改后运行：

```powershell
python -m unittest discover -q
python -m compileall -q gateway remote_agent agent.py tests
node --check gateway/static/app.js
git diff --check
git status --short --branch
```

只有 `.github/workflows/` 发生变化时，才额外运行 Actions 专用静态检查：

```powershell
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12 .github/workflows/test.yml .github/workflows/release.yml
```

涉及包内容时还要构建并检查 wheel：

```powershell
python -m pip install --upgrade build
python -m build --wheel
```

构建产物放在 `dist/`，不得提交。确认 wheel 包含：

- `gateway/static/index.html`
- `gateway/static/app.js`
- `gateway/static/styles.css`
- `remote_agent/manifest.json`
- Apache-2.0 `LICENSE`

发布准备使用简洁的 Conventional Commit 提交，例如：

```text
chore: prepare v0.2.2 release
```

先推送 `main`，等待 Tests Workflow 的 Windows 与 Ubuntu Job 全部成功，再创建 Tag。

## Release Notes

GitHub 的 `--generate-notes` 会提供提交、Pull Request、贡献者和 Full Changelog 基础信息，但维护者或 AI 仍必须补充用户真正关心的内容。

推荐模板：

```markdown
## 亮点

用一到三句话说明本版本最重要的价值。

## 新功能

- 描述用户现在可以完成什么，不要只写内部类名或文件名。

## 修复

- 描述修复前的实际问题以及修复后的行为。

## 兼容性与升级

- 是否需要重新组装 Agent、迁移配置、升级 Profile 或协议。
- 没有迁移要求时明确写“无需迁移”。

## 已知限制

- 只列与本版本使用直接相关的限制。

## 发布产物

- wheel
- SHA256SUMS.txt

## 完整变更

- 链接上一个 Tag 到当前 Tag 的比较页面。
```

写作要求：

- `feat:` 通常进入“新功能”，`fix:` 通常进入“修复”。
- `refactor:`、`test:`、`docs:` 和 `chore:` 只有影响用户使用、兼容性或发布过程时才需要展开。
- 不把“修改了某文件”当成用户价值，要说明行为变化。
- 不声称未验证的性能、安全、兼容性或生产可用性结论。
- 安全修正必须说明用户需要采取的动作，但不要泄露可直接利用的敏感细节。
- 明确区分“当前已经提供”和“未来规划”。

Release Workflow 创建基础说明后，可以使用 `gh release edit` 更新为审核后的文案。修改文案不会改变 Tag 或附件：

```powershell
gh release edit v0.2.2 --notes-file <发布说明文件>
```

发布说明临时文件不得提交，除非用户明确要求维护 Changelog。

## 创建 Tag 与自动发布

确认 `main` 已推送、Tests Workflow 成功且工作树干净后：

```powershell
git tag -a v0.2.2 -m "Remote Agent Gateway v0.2.2"
git push origin v0.2.2
```

Tag 推送会自动触发 Release Workflow。不得在 Workflow 完成前手工重复创建同名 Release。

使用 GitHub CLI 查找并等待运行：

```powershell
gh run list --workflow release.yml --limit 5
gh run watch <run-id> --exit-status
gh release view v0.2.2
```

Release Workflow 会：

1. 在 Windows 与 Ubuntu 上重新安装项目并运行完整验证。
2. 校验 Tag、Python 包版本和 Agent 版本一致。
3. 在干净的 Ubuntu Runner 上构建 wheel。
4. 检查 wheel 中的前端、Manifest 和 LICENSE。
5. 生成 `SHA256SUMS.txt`。
6. 创建 GitHub Pre-release，或者在安全重跑时覆盖同名附件。

## 失败与恢复

- **Tag 前 Tests 失败**：修复后提交并重新等待，不得推 Tag。
- **Release Workflow 临时失败**：优先重跑失败 Job 或整次 Workflow，不要移动 Tag。
- **Release 已创建但附件上传失败**：重跑 Workflow；发布步骤会使用 `--clobber` 覆盖同名附件。
- **Release Notes 有误**：使用 `gh release edit` 修正文案，不需要重建产物。
- **Tag 指向了错误提交或版本内容错误**：公开 Tag 不得重写，修复后发布新的 PATCH 版本。
- **发现实现 Bug**：评估影响，修复并发布新版本；不要替换旧 Release 中已经公开的源码含义。

网络错误、GitHub `5xx` 或 Runner 临时故障不等于代码失败。读取具体 Job 日志，确认根因后再决定重跑或修改代码。

## 发布后验证

发布完成后必须检查：

1. Release 为非 Draft，Pre-release/Stable 状态符合计划。
2. Tag 指向预期提交，远端 Tag 与本地一致。
3. wheel 和 `SHA256SUMS.txt` 均已上传。
4. 从 GitHub 重新下载 wheel，计算 SHA-256 并与校验文件比较。
5. GitHub Release 中的版本、文案、兼容性和升级要求正确。
6. `main` 与 `origin/main` 同步，工作树没有 Secret、构建产物或未提交修改。

完成时向用户报告：

- Release URL
- Tag 与提交 ID
- 发布类型和版本组成
- Tests 与 Release Workflow 结果
- 附件名称和 SHA-256 验证结果
- 任何未完成验证或仍需用户处理的事项

未拿到上述证据时，不得声称发布完成。
