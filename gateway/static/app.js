const state = {
  user: null, agents: [], keys: [], tokens: [], catalog: [], bundles: [], tasks: [], alerts: [],
  controls: { stops: [], operation_groups: {}, runtime_ids: [] },
  mcpUrl: "", wsUrl: "", authMode: "login", view: "overview",
};
let planStream = null;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatTime(epoch) {
  if (!epoch) return "从未";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(epoch * 1000));
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = error ? "show error" : "show";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = ""; }, 2600);
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401) {
    showAuth();
    throw new Error("登录已过期");
  }
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) { /* no body */ }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function showAuth() {
  state.user = null;
  planStream?.close();
  const detail = $("#profile-dialog");
  if (detail.open && detail.operationTask) detail.close();
  $("#app-view").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
}

function showApp() {
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#user-email").textContent = state.user.email;
  $("#mcp-endpoint").textContent = state.mcpUrl;
}

async function loadAll(silent = false) {
  try {
    const session = await api("/session");
    if (!session.authenticated) { showAuth(); return; }
    state.user = session.user;
    state.mcpUrl = session.mcp_url;
    state.wsUrl = session.agent_websocket_url;
    const [agents, keys, tokens, catalog, bundles, tasks, controls, alerts, monitors] = await Promise.all([
      api("/agents"), api("/mcp-keys"), api("/enrollment-tokens"), api("/profile-catalog"),
      api("/agent-bundles"), api("/tasks"), api("/execution-controls"), api("/alerts"), api("/health-monitors"),
    ]);
    state.agents = agents.agents;
    state.keys = keys.keys;
    state.tokens = tokens.tokens;
    state.catalog = catalog.profiles;
    state.bundles = bundles.bundles;
    state.tasks = tasks.tasks;
    state.controls = controls;
    state.alerts = alerts.alerts;
    state.monitors = monitors.monitors;
    showApp();
    render();
    if (!silent) toast("数据已刷新");
  } catch (error) {
    if (state.user && !silent) toast(error.message, true);
  }
}

function emptyState(title, detail) {
  return `<div class="empty"><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div></div>`;
}

function schemaField(field, required = false) {
  const facts = [`类型：${field.type}`, required ? "必填" : "可选"];
  if (Object.hasOwn(field, "default")) facts.push(`默认：${field.default}`);
  if (Object.hasOwn(field, "minimum")) facts.push(`最小：${field.minimum}`);
  return `<article class="schema-field"><header><code>${escapeHtml(field.name)}</code><span>${escapeHtml(facts.join(" · "))}</span></header><p>${escapeHtml(field.description || "按协议返回该字段。")}</p></article>`;
}

function mcpToolSignature(action) {
  if (!action.mcp_tool) return "";
  const parameters = (action.parameters || []).map((field) => `${field.name}${field.required ? "" : "?"}`);
  return `${action.mcp_tool}(${["agent_id", ...parameters].join(", ")})`;
}

function formatBytes(value) {
  if (Number.isInteger(value) && value % (1024 * 1024) === 0) return `${value / (1024 * 1024)} MiB`;
  return `${value} bytes`;
}

function openProfileDialog(profileId, returnAgentId = "") {
  const profile = state.catalog.find((item) => item.id === profileId);
  if (!profile) return;
  const runtime = profile.runtime_contract || {};
  const limits = profile.limits || {};
  const runtimeFields = (runtime.fields || []).map((field) => schemaField(field, true)).join("");
  const dialects = (runtime.dialects || []).map((dialect) => `<article class="dialect-card">
    <header><strong>${escapeHtml(dialect.label)}</strong><code>${escapeHtml(dialect.id)}</code></header>
    <p>${escapeHtml((dialect.systems || []).join(" / "))}</p>
    <span>检测：${escapeHtml((dialect.detection || []).join(" → "))}</span>
    <span>示例：<code>${escapeHtml(dialect.example)}</code></span>
  </article>`).join("");
  const actions = (profile.actions || []).map((action) => `<section class="protocol-section">
    <div class="protocol-section-head"><div><span>Action</span><h3>${escapeHtml(action.name)}</h3></div><p>${escapeHtml(action.description)}</p></div>
    ${action.mcp_tool ? `<div class="mcp-invocation"><span>MCP 工具</span><code>${escapeHtml(action.mcp_tool)}</code><small>${escapeHtml(mcpToolSignature(action))}</small></div>` : ""}
    <h4>输入参数</h4><div class="schema-list">${(action.parameters || []).map((field) => schemaField(field, field.required)).join("")}</div>
    <h4>返回字段</h4><div class="schema-list">${(action.returns || []).map((field) => schemaField(field, true)).join("")}</div>
    <h4>错误语义</h4><div class="error-list">${(action.errors || []).map((error) => `<div><code>${escapeHtml(error.name)}</code><span>${escapeHtml(error.description)}</span></div>`).join("")}</div>
  </section>`).join("");
  const limitItems = [
    limits.capture_bytes_per_stream ? `stdout / stderr 各最多 ${formatBytes(limits.capture_bytes_per_stream)}` : "",
    limits.truncation_marker ? `截断标记：${limits.truncation_marker}` : "",
    limits.timeout_terminates_process_tree ? "超时、取消或断线会终止本次命令的整个进程树" : "",
    limits.max_concurrent_requests_per_agent ? `同一 Agent 最多同时执行 ${limits.max_concurrent_requests_per_agent} 个请求` : "",
    limits.max_file_bytes ? `单文件最多 ${formatBytes(limits.max_file_bytes)}` : "",
    limits.max_read_bytes ? `单次读取输出最多 ${formatBytes(limits.max_read_bytes)}` : "",
    limits.max_write_bytes ? `单次写入最多 ${formatBytes(limits.max_write_bytes)}` : "",
    limits.atomic_write ? "写入使用原子替换" : "",
    limits.optimistic_concurrency ? "编辑使用版本冲突检测" : "",
  ].filter(Boolean);
  const runtimeTitle = profile.id === "shell.v1" ? "Shell 方言声明" : "运行时声明";
  $("#profile-dialog-eyebrow").textContent = `${profile.id} · ${profile.status === "stable" ? "稳定" : profile.status}`;
  $("#profile-dialog-title").textContent = `${profile.name} 协议`;
  $("#profile-dialog").dataset.returnAgentId = returnAgentId;
  $("#profile-dialog-back").classList.toggle("hidden", !returnAgentId);
  $("#profile-dialog-body").innerHTML = `
    <section class="protocol-lead"><p>${escapeHtml(profile.description)}</p><div>${(profile.runtimes || []).map((item) => `<span>${escapeHtml(item)} 实现</span>`).join("")}${profile.minimum_agent_version ? `<span>Agent ≥ ${escapeHtml(profile.minimum_agent_version)}</span>` : ""}</div></section>
    <section class="protocol-section"><div class="protocol-section-head"><div><span>Runtime</span><h3>${runtimeTitle}</h3></div><p>${escapeHtml(runtime.selection || "Agent 必须声明具体运行时。")}</p></div><div class="schema-list">${runtimeFields}</div><div class="dialect-grid">${dialects}</div></section>
    ${actions}
    <section class="protocol-section"><div class="protocol-section-head"><div><span>Limits</span><h3>执行限制</h3></div></div><ul class="limit-list">${limitItems.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
  $("#profile-dialog").showModal();
}

function profileRuntimeLabel(declaration) {
  if (!declaration?.runtime) return "";
  const dialectLabels = { powershell: "PowerShell", cmd: "CMD", "posix-sh": "/bin/sh" };
  const runtime = declaration.runtime;
  const labels = [
    runtime.os || runtime.platform ? platformLabel(runtime.os || runtime.platform) : "",
    dialectLabels[runtime.dialect] || runtime.dialect || runtime.engine || runtime.language || runtime.backend,
    runtime.scope === "workdir" ? "工作目录" : runtime.scope,
  ].filter(Boolean);
  return [...new Set(labels)].join(" · ");
}

function agentProfileIds(agent) {
  const declarations = agent.profile_declarations || [];
  return (agent.profiles || []).length
    ? agent.profiles
    : declarations.map((declaration) => declaration.id);
}

function agentCapabilitiesButton(agent) {
  const count = agentProfileIds(agent).length;
  return `<button class="capability-summary" data-agent-capabilities="${escapeHtml(agent.id)}" type="button" aria-label="查看 ${escapeHtml(agent.name)} 的能力">${count} 个能力</button>`;
}

function openShellPolicyForm(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  const policy = agent?.shell_policy;
  if (!agent || !policy) return;
  $("#agent-capabilities-dialog").close();
  const options = (policy.modes || []).map((option) => `<label class="profile-option policy-option"><input type="radio" name="mode" value="${escapeHtml(option.id)}" ${option.id === policy.mode ? "checked" : ""}><span><strong>${escapeHtml(option.name)}</strong><span>${escapeHtml(option.description)}</span></span></label>`).join("");
  const rules = (policy.rules || []).map((rule) => `<li><strong>${escapeHtml(rule.name)}</strong><span>${escapeHtml(rule.description)}</span></li>`).join("");
  openForm({
    eyebrow: `${agent.name} · shell.v1`,
    title: "配置命令拦截",
    submit: "保存策略",
    fields: `<p class="policy-form-note">标准拦截不限制普通文件删除、项目修改、依赖安装、构建或测试。</p><div class="policy-options">${options}</div><section class="policy-rules"><h3>标准拦截范围</h3><ul>${rules}</ul></section>`,
    onSubmit: async (data) => {
      await api(`/agents/${agentId}/shell-policy`, { method: "PATCH", body: JSON.stringify({ mode: data.get("mode") }) });
      await loadAll(true);
      toast("命令策略已更新");
    },
  });
}

function openPlanPolicyForm(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  if (!agent) return;
  $("#agent-capabilities-dialog").close();
  openForm({
    eyebrow: `${agent.name} · workspace.v1`,
    title: "配置 L1 免审批路径",
    submit: "保存路径",
    fields: `<label>工作区相对路径（每行一个）<textarea name="l1_paths" rows="5" spellcheck="false" maxlength="3856" placeholder="notes/demo.txt">${escapeHtml((agent.plan_policy?.l1_paths || []).join("\n"))}</textarea></label>
      <p class="policy-form-note">仅对该设备精确匹配的 .txt 或 .md 文件生效；最多 16 条。不支持通配符。留空撤销所有 L1 免审批授权。修改后，已预览的旧计划需重新提交。L2 始终需要人工审批。</p>`,
    onSubmit: async (data) => {
      const l1_paths = data.get("l1_paths").split(/\r?\n/).map((path) => path.trim()).filter(Boolean);
      await api(`/agents/${encodeURIComponent(agentId)}/plan-policy`, { method: "PATCH", body: JSON.stringify({ l1_paths }) });
      await loadAll(true);
      toast("设备计划策略已更新");
    },
  });
}

function openAgentCapabilitiesDialog(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  if (!agent) return;
  const declarations = agent.profile_declarations || [];
  const declarationsById = new Map(declarations.map((declaration) => [declaration.id, declaration]));
  const items = agentProfileIds(agent).map((profileId) => {
    const profile = state.catalog.find((item) => item.id === profileId);
    const runtime = profileRuntimeLabel(declarationsById.get(profileId));
    const actions = (profile?.actions || []).map((action) => `<span><code>${escapeHtml(action.plan_operation || action.name)}</code>${action.mcp_tool ? ` <small>→</small> <code>${escapeHtml(action.mcp_tool)}</code>` : ""}</span>`).join("");
    const serviceIds = declarationsById.get(profileId)?.runtime?.service_ids;
    const policy = profileId === "shell.v1" && agent.shell_policy
      ? `<section class="shell-policy"><div><strong>命令拦截</strong><span>${escapeHtml(agent.shell_policy.mode === "standard" ? "标准拦截" : "关闭")}</span></div><button class="secondary" data-shell-policy="${escapeHtml(agent.id)}" type="button">配置</button></section>`
      : "";
    const l1PathCount = agent.plan_policy?.l1_paths.length || 0;
    const planPolicy = profileId === "workspace.v1" && (agent.features?.planned_l1_policy || l1PathCount)
      ? `<section class="shell-policy"><div><strong>L1 计划策略</strong><span>${l1PathCount ? `授权 ${l1PathCount} 个文件` : "需要人工审批"}</span></div><button class="secondary" data-plan-policy="${escapeHtml(agent.id)}" type="button">配置 L1 路径</button></section>`
      : "";
    return `<article class="agent-capability-item">
      <header><div><h3>${escapeHtml(profile?.name || profileId)}</h3><code>${escapeHtml(profileId)}</code></div>${profile ? `<button class="secondary" data-profile-details="${escapeHtml(profileId)}" type="button">查看协议</button>` : ""}</header>
      <p>${escapeHtml(runtime || "未声明运行时详情")}</p>
      ${serviceIds?.length ? `<p>已登记：${escapeHtml(serviceIds.join("、"))}</p>` : ""}
      <div class="agent-capability-actions">${actions || "<span>暂无 Action 映射</span>"}</div>
      ${policy}${planPolicy}
    </article>`;
  }).join("");
  const device = agent.device || {};
  const deviceTypes = { computer: "电脑", desktop: "台式机", server: "服务器" };
  const runtimes = (agent.runtimes || []).map((runtime) => {
    const available = agent.health_runtimes?.includes(runtime.provider_id);
    const status = { available: "已声明", unavailable: "不可用", error: "错误" }[runtime.status];
    return `<p>${escapeHtml(runtimeLabel(runtime.provider_id))} ${escapeHtml(runtime.runtime_version)} · Adapter ${escapeHtml(runtime.adapter_version)} · ${escapeHtml(available ? "可用于健康检查" : status || "未知")} · ${escapeHtml(runtime.mode || "未声明模式")}</p>`;
  }).join("");
  const identity = `<article class="agent-capability-item device-identity">
    <h3>设备资料</h3>
    <p>${escapeHtml(deviceTypes[device.device_type] || device.device_type || "类型未声明")} · ${escapeHtml(devicePlatformText(agent))} · 由当前账号注册</p>
    <p>设备 ID <code>${escapeHtml(agent.id)}</code></p>
    <p>环境：${escapeHtml(agent.environment || "未设置")} · 标签：${escapeHtml((agent.tags || []).join("、") || "未设置")}</p>
    <h3>本地 Runtime</h3>${runtimes || "<p>未声明模型 Runtime</p>"}
  </article>`;
  $("#agent-capabilities-title").textContent = agent.name;
  $("#agent-capabilities-body").innerHTML = identity + (items || emptyState("没有可用能力", "该 Agent 尚未声明标准 Profile"));
  $("#agent-capabilities-dialog").dataset.agentId = agentId;
  $("#agent-capabilities-dialog").showModal();
}

function agentVersionView(agent) {
  const version = agent.version || {};
  if (!version.agent_version) return { text: "版本未知", status: "unknown", label: "未知版本" };
  const build = String(version.build_id || "");
  const buildLabel = build ? ` · Build ${build.slice(0, 8)}` : "";
  const labels = {
    current: "当前版本",
    update_available: "需要升级",
    incompatible: "协议不兼容",
    newer: "高于网关版本",
    unknown: "未知版本",
  };
  return {
    text: `Agent ${version.agent_version} · Core ${version.core_version} · Protocol ${version.protocol_version}${buildLabel}`,
    status: agent.version_status || "unknown",
    label: labels[agent.version_status] || labels.unknown,
  };
}

function platformLabel(platform) {
  const labels = { windows: "Windows", linux: "Linux", macos: "macOS", posix: "POSIX", unknown: "平台未声明" };
  return labels[platform] || labels.unknown;
}

function devicePlatformText(agent) {
  return [platformLabel(agent.device?.platform), agent.device?.platform_version].filter(Boolean).join(" ");
}

function deviceFilterValues(container) {
  return Object.fromEntries([...container.querySelectorAll("select")].map((field) => [field.name, field.value]));
}

function deviceFilterFields(agents, chosen = {}) {
  const values = (items) => [...new Set(items.filter(Boolean))].sort();
  const select = (name, label, options, title = (value) => value) => `
    <label>${label}<select name="${name}"><option value="">全部</option>
      ${options.map((value) => `<option value="${escapeHtml(value)}" ${chosen[name] === value ? "selected" : ""}>${escapeHtml(title(value))}</option>`).join("")}
    </select></label>`;
  return [
    select("platform", "平台", values(agents.map((agent) => agent.device?.platform)), platformLabel),
    select("environment", "环境", values(agents.map((agent) => agent.environment))),
    select("tag", "标签", values(agents.flatMap((agent) => agent.tags || []))),
    select("capability", "能力", values(agents.flatMap((agent) => agent.capability_ids || []))),
  ].join("");
}

function matchesDeviceFilters(agent, filters) {
  return (!filters.platform || agent.device?.platform === filters.platform)
    && (!filters.environment || agent.environment === filters.environment)
    && (!filters.tag || agent.tags?.includes(filters.tag))
    && (!filters.capability || agent.capability_ids?.includes(filters.capability));
}

function agentCard(agent) {
  const capabilities = agentCapabilitiesButton(agent);
  const upgrade = agent.requires_reenrollment ? '<span class="upgrade-badge">需重新注册</span>' : "";
  const version = agentVersionView(agent);
  return `<article class="agent-card">
    <div class="agent-title"><h3>${escapeHtml(agent.name)}</h3><p>${escapeHtml(agent.description || "暂无用途描述")}</p>
      <p>${escapeHtml([devicePlatformText(agent), agent.environment, ...(agent.tags || [])].filter(Boolean).join(" · "))}</p><small>${escapeHtml(version.text)}</small></div>
    <div class="agent-meta"><span class="status ${agent.online ? "online" : ""}">${agent.online ? "在线" : "离线"}</span><span class="version-badge ${escapeHtml(version.status)}">${escapeHtml(version.label)}</span>${upgrade}${capabilities}<span>${formatTime(agent.last_seen_at)}</span></div>
    <div class="row-actions"><button class="secondary" data-edit-agent="${escapeHtml(agent.id)}">编辑</button><button class="danger" data-revoke-agent="${escapeHtml(agent.id)}">撤销</button></div>
  </article>`;
}

function renderAgentDirectory() {
  const filters = deviceFilterValues($("#agent-directory-filters"));
  const matches = state.agents.filter((agent) => matchesDeviceFilters(agent, filters));
  $("#agents-list").innerHTML = matches.length ? matches.map(agentCard).join("")
    : state.agents.length ? emptyState("没有匹配的设备", "调整平台、环境、标签或能力筛选")
    : emptyState("还没有 Agent", "前往 Agent 工作台组装并注册第一台远程设备");
}

function renderAgents() {
  const filters = $("#agent-directory-filters");
  filters.innerHTML = deviceFilterFields(state.agents, deviceFilterValues(filters));
  renderAgentDirectory();
  $("#overview-agents").innerHTML = state.agents.length ? state.agents.slice(0, 4).map(agentCard).join("")
    : emptyState("还没有 Agent", "前往 Agent 工作台组装并注册第一台远程设备");
}

function isExecutionStopped(scope, agentId = "", subject = "") {
  return state.controls.stops.some((stop) => stop.scope === scope && stop.agent_id === agentId && stop.subject === subject);
}

function renderExecutionControls() {
  const row = (scope, label, note, agentId = "", subject = "") => {
    const stopped = isExecutionStopped(scope, agentId, subject);
    return `<article class="data-row execution-control-row"><div><h3>${escapeHtml(label)}</h3><p>${escapeHtml(note)}</p></div>
      <span class="health-status ${stopped ? "cancelled" : ""}">${stopped ? "拒绝新执行" : "未暂停"}</span>
      <button type="button" class="${stopped ? "secondary" : "danger"}" data-execution-stop="${scope}" data-execution-agent="${escapeHtml(agentId)}" data-execution-subject="${escapeHtml(subject)}" data-execution-value="${!stopped}">${stopped ? "恢复" : "暂停"}</button></article>`;
  };
  const groups = Object.entries(state.controls.operation_groups).map(([id, label]) =>
    row("operation", label, "暂停这一类动作，不替代设备或账号停机", "", id));
  const devices = state.agents.flatMap((agent) => {
    const runtimeIds = state.controls.runtime_ids.filter((id) =>
      agent.runtimes?.some((runtime) => runtime.provider_id === id) || isExecutionStopped("runtime", agent.id, id));
    return [
      row("device", agent.name, "阻止这台设备的所有计划、健康检查和直连请求，保留心跳", agent.id),
      ...runtimeIds.map((id) => row("runtime", `${agent.name} · ${runtimeLabel(id)}`,
        "只停止此模型 Runtime，不关闭设备的原生运行能力", agent.id, id)),
    ];
  });
  $("#execution-controls").innerHTML = `
    <div class="health-notice"><strong>网关实例：${isExecutionStopped("global") ? "全局暂停" : "未全局暂停"}</strong><span>实例开关只由 Gateway 主机的运维人员设置。下方状态仅表示该范围自身，不覆盖更外层的暂停。</span></div>
    ${row("account", "当前账号全部设备", "阻止当前账号的计划、健康检查和旧版直连动作")}
    ${groups.join("")}${devices.join("")}`;
}

function alertStatusLabel(status) {
  return { open: "待处理", acknowledged: "已确认", approved: "已批准", blocked: "已拦截", resolved: "已解决", ignored: "已忽略" }[status] || status;
}

function alertDeviceLabel(alert) {
  return state.agents.find((agent) => agent.id === alert.agent_id)?.name || alert.agent_id || "未指定设备";
}

function renderRiskAlerts() {
  const severity = { info: "提示", warning: "警告", critical: "严重" };
  $("#risk-alerts").innerHTML = state.alerts.length ? state.alerts.map((alert) => `
    <article class="data-row risk-alert-row"><div><h3>${escapeHtml(alert.trigger)}</h3>
    <p>${escapeHtml(severity[alert.severity] || alert.severity)} · ${escapeHtml(alertDeviceLabel(alert))} · ${formatTime(alert.updated_at)}</p></div>
    <span class="health-status ${alert.status === "blocked" ? "failed" : ""}">${escapeHtml(alertStatusLabel(alert.status))}</span>
    <button type="button" class="secondary" data-open-risk-alert="${escapeHtml(alert.id)}">查看告警</button></article>`
  ).join("") : emptyState("暂无风险告警", "设备与任务的风险检测结果会显示在这里");
}

async function openRiskAlert(alertId, refreshed = null) {
  const alert = refreshed || (await api(`/alerts/${encodeURIComponent(alertId)}`)).alert;
  const dialog = $("#profile-dialog");
  dialog.dataset.returnAgentId = "";
  $("#profile-dialog-back").classList.add("hidden");
  $("#profile-dialog-eyebrow").textContent = `${alert.category} · ${alertStatusLabel(alert.status)}`;
  $("#profile-dialog-title").textContent = alert.trigger;
  const actions = ["resolved", "ignored"].includes(alert.status) ? [] : [
    ...(alert.status === "acknowledged" ? [] : [["acknowledged", "确认告警"]]),
    ["resolved", "标记已解决"], ["ignored", "忽略告警"],
  ];
  const sources = { detected: "检测记录", owner: "账号所有者", "agent.reconnected": "设备重新上线" };
  const history = alert.history.map((event) => `<li>${formatTime(event.created_at)} · ${escapeHtml(sources[event.source] || event.source)} · ${escapeHtml(alertStatusLabel(event.status))}</li>`);
  $("#profile-dialog-body").innerHTML = `
    <section class="protocol-section"><h3>触发依据</h3>
      <p>设备：${escapeHtml(alertDeviceLabel(alert))} · 告警 ID <code>${escapeHtml(alert.id)}</code></p>
      ${alert.task_id ? `<p>任务 <code>${escapeHtml(alert.task_id)}</code></p>` : ""}
      ${alert.step_id ? `<p>步骤 <code>${escapeHtml(alert.step_id)}</code></p>` : ""}
      ${alert.action ? `<p>动作 <code>${escapeHtml(alert.action)}</code></p>` : ""}
      <pre class="plan-preview">${escapeHtml(JSON.stringify(alert.basis, null, 2))}</pre>
      <p>${escapeHtml(alert.recommendation)}</p>
    </section>
    <section class="protocol-section"><h3>处理记录</h3><ul class="limit-list">${history.join("") || "<li>没有历史迁移记录</li>"}</ul>
      <div class="plan-controls">${actions.map(([status, label]) => `<button type="button" class="secondary" data-risk-status="${status}" data-risk-previous="${escapeHtml(alert.status)}" data-risk-id="${escapeHtml(alert.id)}">${label}</button>`).join("")}</div>
    </section>`;
  if (!dialog.open) dialog.showModal();
}

function renderMetrics() {
  const online = state.agents.filter((agent) => agent.online).length;
  const activeKeys = state.keys.filter((key) => !key.revoked_at).length;
  $("#metrics").innerHTML = [
    ["Agent 总数", state.agents.length, `${online} 台在线`],
    ["在线 Agent", online, online ? "连接状态正常" : "等待远程设备连接"],
    ["有效 MCP Keys", activeKeys, "可连接 AI 客户端"],
  ].map(([label, value, note]) => `<div class="metric"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");
}

function renderKeys() {
  const active = state.keys.filter((key) => !key.revoked_at);
  $("#keys-list").innerHTML = active.length ? active.map((key) => `<article class="data-row">
    <div><h3>${escapeHtml(key.name)}</h3><p>${key.scope === "plan" ? "仅受控计划" : "旧版直连权限"} · 创建于 ${formatTime(key.created_at)}</p></div>
    <code>${escapeHtml(key.prefix)}…</code>
    <time>最近使用：${formatTime(key.last_used_at)}</time>
    <div class="row-actions"><button class="danger" data-revoke-key="${key.id}">撤销</button></div>
  </article>`).join("") : emptyState("没有有效的 MCP Key", "为每个主控 Agent 或直连客户端分别创建一个 Key");
}

function tokenStatus(token) {
  if (token.revoked_at) return "已撤销";
  if (token.used_at) return "已使用";
  if (token.expires_at * 1000 < Date.now()) return "已过期";
  return "等待使用";
}

function renderTokens() {
  $("#tokens-list").innerHTML = state.tokens.length ? state.tokens.map((token) => {
    const active = tokenStatus(token) === "等待使用";
    return `<article class="data-row">
      <div><h3>${escapeHtml(token.name)}</h3><p>${escapeHtml(tokenStatus(token))}</p></div>
      <code>${escapeHtml(token.prefix)}…</code>
      <time>有效期至 ${formatTime(token.expires_at)}</time>
      <div class="row-actions">${active ? `<button class="danger" data-revoke-token="${token.id}">撤销</button>` : ""}</div>
    </article>`;
  }).join("") : emptyState("还没有手动接入 Token", "普通用户无需在这里操作，请优先使用 Agent 工作台");
}

function renderBuilder() {
  $("#profile-catalog").innerHTML = state.catalog.length ? state.catalog.map((profile) => `<article class="catalog-item">
    <header><h4>${escapeHtml(profile.name)}</h4><span class="profile">${escapeHtml(profile.id)}</span></header>
    <p>${escapeHtml(profile.description)}</p>
    <div class="profile-contract">${(profile.actions || []).map((action) => {
      const parameters = (action.parameters || []).map((parameter) => `${parameter.name}${parameter.required ? "" : "?"}: ${parameter.type}`).join(" · ");
      const returns = (action.returns || []).map((field) => `${field.name}: ${field.type}`).join(" · ");
      return `<strong>${escapeHtml(action.name)}</strong><code>${escapeHtml(parameters)}</code><span>返回 ${escapeHtml(returns)}</span>${action.mcp_tool ? `<span class="profile-mcp">MCP · <code>${escapeHtml(action.mcp_tool)}</code></span>` : ""}`;
    }).join("")}</div>
    <footer class="catalog-footer"><div class="catalog-meta"><span class="catalog-status">${profile.status === "stable" ? "稳定" : escapeHtml(profile.status)}</span>${profile.runtimes.map((runtime) => `<span>${escapeHtml(runtime)}</span>`).join("")}</div><button class="secondary" data-profile-details="${escapeHtml(profile.id)}" type="button">查看协议</button></footer>
  </article>`).join("") : emptyState("暂无可组装 Profile", "网关管理员尚未发布标准能力");

  $("#bundles-list").innerHTML = state.bundles.length ? state.bundles.map((bundle) => {
    const expired = bundle.expires_at * 1000 <= Date.now();
    return `<article class="data-row">
      <div><h3>${escapeHtml(bundle.name)}</h3><p>${escapeHtml(bundle.description || "暂无用途描述")}</p></div>
      <code>${escapeHtml(bundle.runtime)} · ${escapeHtml(bundle.profiles.join(", "))}</code>
      <time>${expired ? "已过期" : `有效期至 ${formatTime(bundle.expires_at)}`}</time>
      <div class="row-actions">${expired ? "" : `<button class="secondary" data-download-bundle="${bundle.id}">下载 ZIP</button>`}</div>
    </article>`;
  }).join("") : emptyState("还没有组装记录", "选择标准 Profile，生成可独立部署的远程 Agent 包");
}

function taskStatusLabel(status) {
  return {
    created: "已创建",
    planned: "已规划",
    waiting_approval: "待审批",
    approved: "待执行",
    queued: "排队中",
    running: "执行中",
    completed: "已完成",
    partially_completed: "部分完成",
    failed: "失败",
    cancelled: "已取消",
    verification_failed: "验证失败",
    skipped: "依赖未完成",
  }[status] || status;
}

function renderHealth() {
  const tasks = state.tasks.filter((task) => task.risk_policy?.mode !== "operation_plan");
  $("#health-tasks").innerHTML = tasks.length ? tasks.map((task) => {
    const runtime = task.requested_runtime ? `${escapeHtml(runtimeLabel(task.requested_runtime))} · L0` : "原生 · L0";
    const summary = task.result?.summary;
    const summaryText = summary
      ? `健康 ${summary.healthy || 0} · 警告 ${summary.warning || 0} · 失败 ${summary.failed || 0}`
      : "等待设备事件和观察结果";
    const canCancel = !["completed", "failed", "partially_completed", "cancelled"].includes(task.status);
    return `<article class="data-row health-task-row">
      <div><h3>${escapeHtml(task.objective)}</h3><p>${runtime} · ${escapeHtml(summaryText)}</p></div>
      <span class="health-status ${escapeHtml(task.status)}">${escapeHtml(taskStatusLabel(task.status))}</span>
      <time>${formatTime(task.updated_at)}</time>
      <div class="row-actions"><button class="secondary" data-health-detail="${escapeHtml(task.id)}">事件与证据</button>${canCancel ? `<button class="danger" data-health-cancel="${escapeHtml(task.id)}">取消</button>` : ""}</div>
    </article>`;
  }).join("") : emptyState("还没有健康检查", "选择在线 PC Agent，开始一次只读跨设备检查");
}

function renderOperationPlans() {
  const tasks = state.tasks.filter((task) => task.risk_policy?.mode === "operation_plan");
  $("#operation-plans").innerHTML = tasks.length ? tasks.map((task) => `
    <article class="data-row health-task-row">
      <div><h3>${escapeHtml(task.objective)}</h3><p>${escapeHtml(task.risk_policy.max_level)} · ${escapeHtml(task.resource_budget.max_actions)} 步</p></div>
      <span class="health-status ${escapeHtml(task.status)}">${escapeHtml(taskStatusLabel(task.status))}</span>
      <time>${formatTime(task.updated_at)}</time>
      <button class="secondary" type="button" data-operation-detail="${escapeHtml(task.id)}">查看计划</button>
    </article>`).join("") : emptyState("还没有任务计划", "为在线 Agent 准备文件操作或固定版本检查");
}

function render() {
  renderMetrics();
  renderAgents();
  renderKeys();
  renderTokens();
  renderBuilder();
  renderHealth();
  renderOperationPlans();
  renderExecutionControls();
  renderRiskAlerts();
  renderHealthMonitors();
}

function switchView(view) {
  const titles = { overview: "概览", health: "健康检查", plans: "任务计划", builder: "Agent 工作台", agents: "远程 Agent", keys: "MCP Keys", tokens: "手动接入", controls: "安全控制", alerts: "风险告警" };
  state.view = view;
  $$("#main-nav button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
  $("#page-title").textContent = titles[view];
  $(".sidebar").classList.remove("open");
}

function runtimeLabel(provider) {
  return { codex: "Codex", claude: "Claude Code" }[provider] || provider;
}

function renderHealthMonitors() {
  const statuses = { enabled: "已授权", suspended: "停机后需重新授权", expired: "已到期" };
  $("#health-monitors").innerHTML = state.monitors.length ? state.monitors.map((monitor) => {
    const name = state.agents.find((agent) => agent.id === monitor.agent_id)?.name || monitor.agent_id;
    return `<article class="data-row"><div><h3>${escapeHtml(name)}</h3><p>${escapeHtml(statuses[monitor.status])} · 每 ${monitor.interval_minutes} 分钟 · 截止 ${escapeHtml(formatTime(monitor.expires_at))}</p></div>
      <div class="row-actions">${monitor.last_task_id ? `<button type="button" class="secondary" data-health-detail="${escapeHtml(monitor.last_task_id)}">最近巡检</button>` : ""}
      <button type="button" class="secondary" data-monitor-disable="${escapeHtml(monitor.agent_id)}">关闭巡检</button></div></article>`;
  }).join("") : emptyState("未授权定时巡检", "只在所有者明确选择设备后周期性读取健康摘要");
}

function openMonitorForm() {
  const candidates = state.agents.filter((agent) => agent.features?.task_protocol === 1
    && agent.profiles.includes("system_observe.v1"));
  openForm({
    eyebrow: "L0 Read Only",
    title: "授权定时巡检",
    submit: "授权 24 小时",
    fields: candidates.length ? `<p class="policy-form-note">仅对所选设备运行原生 L0 健康检查。重新授权会替换原巡检并清空比较基线。停机后不会自动续跑。</p>
      <label>目标设备<select name="agent_id">${candidates.map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name)} · ${escapeHtml(devicePlatformText(agent))}</option>`).join("")}</select></label>
      <label>巡检间隔<select name="interval_minutes"><option value="15">15 分钟</option><option value="30" selected>30 分钟</option><option value="60">60 分钟</option></select></label>`
      : emptyState("没有支持巡检的设备", "先注册声明 system_observe.v1 和 Task Protocol v1 的 Agent"),
    onSubmit: async (data) => {
      const agentId = data.get("agent_id");
      if (!agentId) throw new Error("请选择一台设备");
      await api(`/health-monitors/${encodeURIComponent(agentId)}`, {
        method: "PUT", body: JSON.stringify({ interval_minutes: Number(data.get("interval_minutes")) }),
      });
      await loadAll(true);
      toast("定时巡检已授权");
    },
  });
}

function openHealthForm() {
  const accountStopped = isExecutionStopped("global") || isExecutionStopped("account");
  const candidates = state.agents.filter((agent) => !accountStopped && !isExecutionStopped("device", agent.id) && agent.online
    && agent.features?.task_protocol >= 1
    && (agent.profiles || []).includes("system_observe.v1"));
  const providers = [...new Set(candidates.flatMap((agent) => agent.health_runtimes || []))].sort(
    (left, right) => Number(right === "codex") - Number(left === "codex") || left.localeCompare(right),
  );
  const runtimeOptions = providers.map((provider) => `<option value="${escapeHtml(provider)}">${escapeHtml(runtimeLabel(provider))}（受限只读）</option>`).join("");
  const unchecked = new Set();
  openForm({
    eyebrow: "L0 Read Only",
    title: "开始跨设备健康检查",
    submit: "开始检查",
    fields: candidates.length ? `<p class="policy-form-note">仅执行四个固定只读观察。Codex 会把系统类型、资源百分比和计数发给模型；Claude Code 只向模型提交固定观察名称，不回传采集数据。</p>
      <label>运行时<select id="health-runtime" name="runtime">${runtimeOptions}<option value="">原生检查（无需模型）</option></select></label>
      ${providers.length ? "" : `<p class="policy-form-note">当前无兼容的模型 Runtime，请在设备上检查版本或启用 Codex。</p>`}
      <div class="device-filters" id="health-device-filters">${deviceFilterFields(candidates)}</div>
      <div class="profile-options" id="health-options"></div>` : emptyState("没有可用设备", "检查停机控制、设备连接与 system_observe.v1 声明"),
    onSubmit: async (data) => {
      const agentIds = data.getAll("agent_ids");
      if (!agentIds.length) throw new Error("至少选择一台在线设备");
      if (agentIds.length > 64) throw new Error("单次健康检查最多选择 64 台设备");
      const requestedRuntime = data.get("runtime") || null;
      const result = await api("/tasks", { method: "POST", body: JSON.stringify({ kind: "health_check", agent_ids: agentIds, requested_runtime: requestedRuntime }) });
      switchView("health");
      await loadAll(true);
      watchHealthTask(result.task.id);
    },
  });
  const runtimeSelector = $("#health-runtime");
  if (runtimeSelector) {
    const filterControls = $("#health-device-filters");
    const options = $("#health-options");
    const renderChoices = () => {
      const provider = runtimeSelector.value;
      const filters = deviceFilterValues(filterControls);
      const matches = candidates.filter((agent) => matchesDeviceFilters(agent, filters)
        && (!provider || agent.health_runtimes?.includes(provider)));
      options.innerHTML = matches.length ? matches.map((agent) => `<label class="profile-option">
        <input type="checkbox" name="agent_ids" value="${escapeHtml(agent.id)}" ${unchecked.has(agent.id) ? "" : "checked"}>
        <span><strong>${escapeHtml(agent.name)}</strong><span>${escapeHtml(devicePlatformText(agent))} · ${escapeHtml(agent.environment || agent.description || "系统只读观察")}</span></span>
      </label>`).join("") : emptyState("没有匹配的在线设备", "调整筛选条件或选择其他运行时");
    };
    options.onchange = (event) => {
      const checkbox = event.target;
      if (checkbox.checked) unchecked.delete(checkbox.value);
      else unchecked.add(checkbox.value);
    };
    runtimeSelector.onchange = renderChoices;
    filterControls.onchange = renderChoices;
    renderChoices();
  }
}

async function openHealthDetail(taskId) {
  const path = `/tasks/${encodeURIComponent(taskId)}`;
  const [{ task }, { events }, { evidence }, { alerts }] = await Promise.all([
    api(path), api(`${path}/events`), api(`${path}/evidence`), api(`${path}/alerts`),
  ]);
  const observations = evidence.map(({ summary, content_hash }) => `<li>${escapeHtml(summary.agent_id)} · ${escapeHtml(summary.status)}
      ${summary.runtime ? `· ${escapeHtml(summary.runtime.provider_id)} ${escapeHtml(summary.runtime.runtime_version)}` : ""}
      <br><code>${escapeHtml(content_hash)}</code></li>`).join("");
  const timeline = events.map((event) => `<li>${escapeHtml(formatTime(event.created_at))} · ${escapeHtml(event.event_type)}
      ${event.payload?.policy ? `· ${escapeHtml(event.payload.policy.decision)}` : ""}
      ${event.payload?.error ? `· ${escapeHtml(event.payload.error)}` : ""}</li>`).join("");
  const risks = alerts.map((alert) => `<li>${escapeHtml(alertStatusLabel(alert.status))} · ${escapeHtml(alert.trigger)} · ${escapeHtml(alert.recommendation)}</li>`).join("");
  const steps = task.steps.map((step) => {
    const agent = state.agents.find((item) => item.id === step.target_agent_id);
    const cancellable = task.status === "running" && agent?.features?.health_step_cancel
      && ["created", "running"].includes(step.status);
    return `<article class="schema-field"><header><code>${escapeHtml(step.action_type)}</code><span>${escapeHtml(taskStatusLabel(step.status))}</span></header>
      <p>${escapeHtml(agent?.name || step.target_agent_id)}</p>
      ${cancellable ? `<button class="secondary" data-health-step-cancel="${escapeHtml(step.id)}" data-health-task="${escapeHtml(task.id)}" type="button">取消步骤</button>` : ""}</article>`;
  }).join("");
  const dialog = $("#profile-dialog");
  dialog.dataset.returnAgentId = "";
  $("#profile-dialog-back").classList.add("hidden");
  $("#profile-dialog-eyebrow").textContent = `L0 · ${taskStatusLabel(task.status)}`;
  $("#profile-dialog-title").textContent = task.objective;
  $("#profile-dialog-body").innerHTML = `
    <section class="protocol-section"><h3>证据 · ${evidence.length}</h3><ul class="limit-list">${observations || "<li>暂无已验证证据</li>"}</ul></section>
    <section class="protocol-section"><h3>观察步骤</h3>${steps}</section>
    <section class="protocol-section"><h3>告警 · ${alerts.length}</h3><ul class="limit-list">${risks || "<li>暂无告警</li>"}</ul></section>
    <section class="protocol-section"><h3>执行事件</h3><ul class="limit-list">${timeline || "<li>暂无事件</li>"}</ul></section>`;
  if (!dialog.open) dialog.showModal();
}

async function watchHealthTask(taskId) {
  for (let index = 0; index < 44; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    await loadAll(true);
    const task = state.tasks.find((item) => item.id === taskId);
    if (task && ["completed", "failed", "partially_completed", "cancelled"].includes(task.status)) {
      toast(task.status === "completed" ? "健康检查已完成" : `健康检查${taskStatusLabel(task.status)}`, task.status === "failed");
      return;
    }
  }
}

function planWindowInput(epoch) {
  if (!epoch) return "";
  const date = new Date(epoch * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
}

function openPlanForm(existing = null) {
  const agents = state.agents.filter((agent) => agent.online && agent.features?.planned_actions);
  const candidate = agents.find((agent) => agentProfileIds(agent).includes("workspace.v1")) || agents[0];
  const workspace = candidate && agentProfileIds(candidate).includes("workspace.v1");
  const serviceId = candidate?.profile_declarations?.find((profile) => profile.id === "service_control.v1")?.runtime?.service_ids?.[0];
  const build = candidate && agentProfileIds(candidate).includes("build_verify.v1");
  const operation = workspace ? "workspace.hash" : serviceId ? "service.inspect" : build ? "build.source" : "shell.version";
  const params = workspace ? { path: "example.txt" } : serviceId ? { service_id: serviceId } : build ? {} : { tool: "git" };
  const steps = existing
    ? existing.plan.steps.map(({ key, agent_id, operation, params, depends_on }) => ({ key, agent_id, operation, params, depends_on }))
    : [{
      key: "observe", agent_id: candidate?.id || "",
      operation, params,
    }];
  const targets = agents.map((agent) => `<li>${escapeHtml(agent.name)} · <code>${escapeHtml(agent.id)}</code></li>`).join("");
  const maintenance = existing?.plan.maintenance;
  openForm({
    eyebrow: "受控原生任务",
    title: existing ? "修订计划（旧审批失效）" : "新建任务计划",
    submit: "保存并预览",
    fields: `<label>目标<input name="objective" required maxlength="240" value="${escapeHtml(existing?.objective || "")}"></label>
      <label>执行时限（秒）<input name="timeout" type="number" min="10" max="300" required value="${existing?.plan.timeout_seconds || 120}"></label>
      <label>维护窗口开始（可选，本地时间）<input name="maintenance_start" type="datetime-local" step="1" value="${planWindowInput(maintenance?.starts_at)}"></label>
      <label>维护窗口结束（可选，本地时间）<input name="maintenance_end" type="datetime-local" step="1" value="${planWindowInput(maintenance?.ends_at)}"></label>
      <label>窗口内每批最多设备数<input name="maintenance_concurrency" type="number" min="1" max="5" required value="${maintenance?.max_concurrency || 1}"></label>
      <label>步骤（JSON 数组）<textarea name="steps" required rows="9" spellcheck="false">${escapeHtml(JSON.stringify(steps, null, 2))}</textarea></label>
      <div class="policy-form-note">设置窗口后须人工批准，并在窗口内手动启动。某批未验证通过，跳过剩余批次。支持文件和版本检查、登记应用恢复与预置源码验证；不要在计划中粘贴凭据。<ul class="limit-list">${targets || "<li>没有兼容的在线 Agent，请重新组装并连接。</li>"}</ul></div>`,
    onSubmit: async (data) => {
      let parsed;
      try { parsed = JSON.parse(data.get("steps")); }
      catch (_) { throw new Error("步骤不是有效的 JSON 数组"); }
      if (!Array.isArray(parsed)) throw new Error("请提交步骤数组");
      const draft = { objective: data.get("objective"), timeout_seconds: Number(data.get("timeout")), steps: parsed };
      const start = data.get("maintenance_start");
      const end = data.get("maintenance_end");
      if (start || end) {
        const startsAt = Date.parse(start) / 1000;
        const endsAt = Date.parse(end) / 1000;
        if (!Number.isInteger(startsAt) || !Number.isInteger(endsAt) || startsAt >= endsAt) {
          throw new Error("请提供有效的维护窗口起止时间");
        }
        draft.maintenance = { starts_at: startsAt, ends_at: endsAt, max_concurrency: Number(data.get("maintenance_concurrency")) };
      }
      const response = existing
        ? await api(`/tasks/${encodeURIComponent(existing.id)}/plan`, { method: "PUT", body: JSON.stringify({ plan_hash: existing.plan.hash, plan: draft }) })
        : await api("/task-plans", { method: "POST", body: JSON.stringify(draft) });
      switchView("plans");
      await loadAll(true);
      await openOperationDetail(response.task.id);
    },
  });
}

function openAgentMetadataForm(agent) {
  openForm({
    eyebrow: "Agent Metadata",
    title: "编辑 Agent",
    submit: "保存",
    fields: `<label>名称<input name="name" value="${escapeHtml(agent.name)}" required maxlength="80"></label>
      <label>用途描述<textarea name="description" maxlength="500" rows="4">${escapeHtml(agent.description)}</textarea></label>
      <label>环境<input name="environment" value="${escapeHtml(agent.environment)}" maxlength="32" placeholder="例如 staging"></label>
      <label>标签<input name="tags" value="${escapeHtml((agent.tags || []).join(", "))}" maxlength="419" placeholder="例如 canary, west"></label>
      <p class="policy-form-note">环境和标签只用于筛选。每项最多 32 字符，首字符为字母或数字，其余可含点、下划线、连字符。标签用逗号分隔，最多 12 个。</p>`,
    onSubmit: async (data) => {
      const environment = data.get("environment").trim().toLowerCase() || null;
      const tags = [...new Set(data.get("tags").toLowerCase().split(/[,，]/).map((tag) => tag.trim()).filter(Boolean))];
      const label = /^[a-z0-9][a-z0-9._-]{0,31}$/;
      if ((environment !== null && !label.test(environment)) || tags.some((tag) => !label.test(tag)) || tags.length > 12) {
        throw new Error("环境或标签格式不正确；请输入 1–32 位英文字母、数字或允许的符号");
      }
      await api(`/agents/${encodeURIComponent(agent.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ name: data.get("name"), description: data.get("description"), environment, tags }),
      });
      await loadAll(true);
    },
  });
}

function operationEventLabel(event) {
  const policy = event.payload?.policy;
  const batch = event.payload?.batch;
  return `${formatTime(event.created_at)} · ${event.event_type}${policy ? ` · ${policy.risk_level} ${policy.decision}` : ""}${batch ? ` · 批次 ${batch}` : ""}${event.agent_id ? ` · ${event.agent_id}` : ""}`;
}

async function openOperationDetail(taskId) {
  const path = `/tasks/${encodeURIComponent(taskId)}`;
  const [{ task }, { events }, { evidence }, { alerts }] = await Promise.all([
    api(path), api(`${path}/events`), api(`${path}/evidence`), api(`${path}/alerts`),
  ]);
  planStream?.close();
  planStream = null;
  const plan = task.plan;
  const active = ["queued", "running"].includes(task.status);
  const review = task.status === "waiting_approval";
  const approved = task.status === "approved";
  const dialog = $("#profile-dialog");
  dialog.dataset.returnAgentId = "";
  dialog.operationTask = task;
  $("#profile-dialog-back").classList.add("hidden");
  $("#profile-dialog-eyebrow").textContent = `修订 ${plan.revision} · ${plan.max_level} · ${taskStatusLabel(task.status)}`;
  $("#profile-dialog-title").textContent = task.objective;
  const control = (command, label, style = "secondary") => `<button type="button" class="${style}" data-plan-command="${command}">${label}</button>`;
  const states = Object.fromEntries(task.steps.map((step) => [step.id, step]));
  const steps = plan.steps.map((step) => {
    const stepState = states[step.id];
    const input = {
      "shell.version": { command: `${step.params.tool} --version` },
      "service.restart": { ...step.params, recovery: "本机登记接口 → 新代际就绪检查" },
      "build.verify": { ...step.params, recipe: "Python 编译检查 → unittest" },
    }[step.operation] || step.params;
    const cancel = active && ["created", "running"].includes(stepState?.status)
      ? `<button type="button" class="secondary" data-plan-step-cancel="${escapeHtml(step.id)}">取消步骤</button>` : "";
    const approvalLabel = (step.approval_required ?? step.risk_level !== "L0") ? "需人工审批" : "策略允许";
    return `<article class="schema-field"><header><strong>${escapeHtml(step.key)} · ${escapeHtml(step.operation)}</strong><span>${escapeHtml(step.risk_level)} · ${approvalLabel} · ${escapeHtml(taskStatusLabel(stepState?.status))}</span></header>
      <p>${escapeHtml(plan.targets[step.agent_id].name)} · <code>${escapeHtml(step.agent_id)}</code>${step.depends_on.length ? ` · 依赖 ${escapeHtml(step.depends_on.join("、"))}` : ""}</p>
      <pre class="plan-preview">${escapeHtml(JSON.stringify(input, null, 2))}</pre>${cancel}</article>`;
  }).join("");
  const verifiedCount = evidence.filter((entry) => entry.kind !== "operation_plan_attempt").length;
  const maintenanceSection = plan.maintenance ? `<section class="protocol-section"><h3>维护窗口与批次</h3>
    <p>${escapeHtml(formatTime(plan.maintenance.starts_at))} — ${escapeHtml(formatTime(plan.maintenance.ends_at))}
    · 每批最多 ${plan.maintenance.max_concurrency} 台设备 · 须在窗口内手动启动，失败即停止后续批次</p>
    <ol class="limit-list">${plan.maintenance.batches.map((batch) => `<li>${escapeHtml(batch.join("、"))}</li>`).join("")}</ol></section>` : "";
  const results = evidence.map((entry) => `<article class="schema-field"><header><strong>${escapeHtml(entry.summary.operation)}</strong>${entry.kind === "operation_plan_attempt" ? "<span>未通过验证</span>" : ""}</header><code>${escapeHtml(entry.content_hash)}</code>
      <pre class="plan-preview">${escapeHtml(entry.summary.diff || JSON.stringify(entry.summary, null, 2))}</pre></article>`).join("");
  $("#profile-dialog-body").innerHTML = `
    <section class="protocol-section"><h3>执行与授权</h3><p>${plan.submitted_by_mcp ? "计划型 MCP 提交 · " : ""}${plan.workdir_scope === "local_registration" ? "设备预置资源" : "设备绑定的工作目录"} · ${plan.timeout_seconds} 秒预算 · 不自动重试或回滚</p>
      ${plan.external_network === null ? "<p>测试代码继承本机权限；网络仅能通过设备隔离策略限制。</p>" : ""}
      <p>计划有效至 ${escapeHtml(formatTime(plan.valid_until))}${task.approval ? ` · 审批有效至 ${escapeHtml(formatTime(task.approval.expires_at))}` : ""}</p>
      ${task.approval ? `<p>${task.approval.decision === "allow" ? "批准" : "拒绝"} · ${escapeHtml(task.approval.approver_user_id === state.user?.id ? state.user.email : task.approval.approver_user_id)} · ${escapeHtml(formatTime(task.approval.created_at))}</p>` : ""}
      <code>${escapeHtml(plan.hash)}</code><div class="plan-controls">
      ${review ? control("approve", "批准此版本", "primary") + control("deny", "拒绝") : ""}
      ${approved ? control("execute", "执行此版本", "primary") : ""}
      ${review || approved ? control("revise", "修订计划") : ""}${review || approved || active ? control("cancel", "取消任务") : ""}
      ${control("refresh", "刷新详情")}</div></section>
    ${maintenanceSection}<section class="protocol-section"><h3>设备与步骤</h3><div class="schema-list">${steps}</div></section>
    <section class="protocol-section"><h3>已验证证据 · ${verifiedCount}</h3><div class="schema-list">${results || "<p>尚无证据</p>"}</div></section>
    <section class="protocol-section"><h3>风险告警 · ${alerts.length}</h3><ul class="limit-list">${alerts.map((alert) => `<li>${escapeHtml(alertStatusLabel(alert.status))} · ${escapeHtml(alert.trigger)}</li>`).join("") || "<li>无告警</li>"}</ul></section>
    <section class="protocol-section"><h3>执行事件</h3><ul id="plan-timeline" class="limit-list" aria-live="polite">${events.map((event) => `<li>${escapeHtml(operationEventLabel(event))}</li>`).join("")}</ul></section>`;
  if (!dialog.open) dialog.showModal();
  if (active && typeof EventSource !== "undefined") {
    const cursor = events.at(-1)?.sequence || 0;
    const stream = new EventSource(`/api${path}/events/live?after_sequence=${cursor}`);
    planStream = stream;
    stream.addEventListener("task_event", (event) => {
      const data = JSON.parse(event.data);
      if (planStream !== stream || data.sequence <= cursor) return;
      const entry = document.createElement("li");
      entry.textContent = operationEventLabel(data);
      $("#plan-timeline")?.append(entry);
    });
    stream.addEventListener("task_finished", () => {
      if (planStream !== stream) return;
      stream.close();
      loadAll(true);
      if (dialog.open && dialog.operationTask?.id === taskId) openOperationDetail(taskId).catch((error) => toast(error.message, true));
    });
  }
}

async function handlePlanControl(button) {
  const dialog = $("#profile-dialog");
  const task = dialog.operationTask;
  if (!task) return;
  const command = button.dataset.planCommand;
  if (command === "revise") { dialog.close(); openPlanForm(task); return; }
  if (command === "refresh") { await openOperationDetail(task.id); return; }
  const path = `/tasks/${encodeURIComponent(task.id)}`;
  const stepToCancel = button.dataset.planStepCancel;
  const endpoint = stepToCancel ? `${path}/steps/${encodeURIComponent(stepToCancel)}/cancel`
    : `${path}/${["approve", "deny"].includes(command) ? "approval" : command}`;
  const prompt = {
    approve: "确认批准已预览的设备、动作、参数和计划版本？",
    deny: "拒绝并关闭此计划？",
    execute: "确认立即执行此版本？已经执行的操作不会自动回滚。",
    cancel: "确认取消任务？已经开始的操作可能需要人工核对。",
  }[command] || "确认取消此步骤？";
  if (!confirm(prompt)) return;
  button.disabled = true;
  try {
    await api(endpoint, { method: "POST", body: JSON.stringify({ plan_hash: task.plan.hash, decision: ["approve", "deny"].includes(command) ? command : undefined }) });
    await loadAll(true);
    await openOperationDetail(task.id);
  } finally { button.disabled = false; }
}

function openForm({ eyebrow, title, fields, submit, onSubmit }) {
  const dialog = $("#form-dialog");
  $("#dialog-eyebrow").textContent = eyebrow;
  $("#dialog-title").textContent = title;
  $("#dialog-submit").textContent = submit;
  $("#dialog-error").textContent = "";
  $("#dialog-fields").innerHTML = fields;
  dialog.onSubmitAction = onSubmit;
  dialog.showModal();
}

function showSecret(title, secret, command = "") {
  $("#secret-title").textContent = title;
  $("#secret-value").value = secret;
  $("#command-value").value = command;
  $("#command-block").classList.toggle("hidden", !command);
  $("#copy-command").classList.toggle("hidden", !command);
  $("#secret-dialog").showModal();
}

async function createKey(name, scope) {
  const result = await api("/mcp-keys", { method: "POST", body: JSON.stringify({ name, scope }) });
  showSecret("MCP Key 已创建", result.secret);
  await loadAll(true);
}

async function createToken(name) {
  const result = await api("/enrollment-tokens", { method: "POST", body: JSON.stringify({ name }) });
  showSecret("手动接入 Token 已创建", result.secret, result.command);
  await loadAll(true);
}

async function createBundle(name, description, profiles) {
  const result = await api("/agent-bundles", {
    method: "POST",
    body: JSON.stringify({ name, description, runtime: "python", profiles }),
  });
  const link = document.createElement("a");
  link.href = result.bundle.download_url;
  link.download = result.bundle.filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  showSecret("Agent 已组装", result.secret, result.command);
  await loadAll(true);
}

function openBundleForm() {
  const options = state.catalog.map((profile) => `<label class="profile-option"><input type="checkbox" name="profiles" value="${escapeHtml(profile.id)}" ${profile.id === "shell.v1" ? "checked" : ""}><span><strong>${escapeHtml(profile.name)} · ${escapeHtml(profile.id)}</strong><span>${escapeHtml(profile.description)}</span></span></label>`).join("");
  openForm({
    eyebrow: "Python Agent Builder",
    title: "组装远程 Agent",
    submit: "组装并下载",
    fields: `<label>Agent 名称<input name="name" required maxlength="80" placeholder="例如 GPU Notebook"></label><label>用途描述<textarea name="description" maxlength="500" rows="3" placeholder="告诉 AI 这台设备适合做什么"></textarea></label><div class="profile-options">${options}</div><p class="policy-form-note">登记服务和源码验证需要设备端本地配置；请选择这些能力时取消旧版直连 Shell。</p>`,
    onSubmit: (data) => {
      const profiles = data.getAll("profiles");
      if (!profiles.length) throw new Error("至少选择一个 Profile");
      return createBundle(data.get("name"), data.get("description"), profiles);
    },
  });
}

function bindEvents() {
  $("#login-tab").onclick = () => setAuthMode("login");
  $("#register-tab").onclick = () => setAuthMode("register");
  $("#auth-form").onsubmit = async (event) => {
    event.preventDefault();
    $("#auth-error").textContent = "";
    const button = $("#auth-submit");
    button.disabled = true;
    try {
      await api(`/auth/${state.authMode}`, { method: "POST", body: JSON.stringify({ email: $("#auth-email").value, password: $("#auth-password").value }) });
      await loadAll(true);
    } catch (error) { $("#auth-error").textContent = error.message; }
    finally { button.disabled = false; }
  };
  $("#logout-button").onclick = async () => { try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* session may already be gone */ } showAuth(); };
  $("#refresh-button").onclick = () => loadAll();
  $("#agent-directory-filters").onchange = renderAgentDirectory;
  $("#mobile-menu").onclick = () => $(".sidebar").classList.toggle("open");
  $$("#main-nav button").forEach((button) => button.onclick = () => switchView(button.dataset.view));
  $("#new-health-button").onclick = openHealthForm;
  $("#new-monitor-button").onclick = openMonitorForm;
  $("#new-plan-button").onclick = () => openPlanForm();
  $("#new-key-button").onclick = () => openForm({
    eyebrow: "MCP Access",
    title: "创建 MCP Key",
    submit: "创建 Key",
    fields: `<label>名称<input name="name" required maxlength="80" placeholder="例如 运维主控 Agent"></label>
      <label>权限范围<select name="scope"><option value="plan">仅受控计划（推荐）</option><option value="legacy">旧版直连工具</option></select></label>
      <p class="policy-form-note">计划型 Key 不能调用直连 Shell 或文件工具，也不能审批。旧版直连只应发给受信客户端，不经过计划审批。</p>`,
    onSubmit: (data) => createKey(data.get("name"), data.get("scope")),
  });
  $$('[data-action="new-bundle"]').forEach((button) => button.onclick = openBundleForm);
  $$('[data-action="new-token"]').forEach((button) => button.onclick = () => openForm({ eyebrow: "Advanced Enrollment", title: "创建手动接入 Token", submit: "创建 Token", fields: '<label>令牌名称或备注<input name="name" required maxlength="80" placeholder="例如 CI 临时环境"></label>', onSubmit: (data) => createToken(data.get("name")) }));
  $$('[data-action="go-builder"]').forEach((button) => button.onclick = () => switchView("builder"));
  $$('[data-action="close-form-dialog"]').forEach((button) => button.onclick = () => $("#form-dialog").close());
  $("#dialog-form").onsubmit = async (event) => {
    event.preventDefault();
    const dialog = $("#form-dialog");
    const button = $("#dialog-submit");
    button.disabled = true;
    try { await dialog.onSubmitAction(new FormData(event.currentTarget)); dialog.close(); }
    catch (error) { $("#dialog-error").textContent = error.message; }
    finally { button.disabled = false; }
  };
  $("#secret-close").onclick = () => $("#secret-dialog").close();
  $("#agent-capabilities-close").onclick = () => $("#agent-capabilities-dialog").close();
  $("#agent-capabilities-done").onclick = () => $("#agent-capabilities-dialog").close();
  $("#profile-dialog-back").onclick = () => {
    const dialog = $("#profile-dialog");
    const agentId = dialog.dataset.returnAgentId;
    dialog.close();
    if (agentId) openAgentCapabilitiesDialog(agentId);
  };
  $("#profile-dialog-close").onclick = () => $("#profile-dialog").close();
  $("#profile-dialog-done").onclick = () => $("#profile-dialog").close();
  $("#profile-dialog").addEventListener("close", () => {
    if ($("#profile-dialog").open) return;
    planStream?.close();
    planStream = null;
    $("#profile-dialog").operationTask = null;
  });
  $("#copy-secret").onclick = () => copyText($("#secret-value").value, "Secret 已复制");
  $("#copy-command").onclick = () => copyText($("#command-value").value, "启动命令已复制");
  $(".copy-endpoint").onclick = () => copyText(state.mcpUrl, "MCP Endpoint 已复制");
  document.body.onclick = async (event) => {
    const target = event.target;
    const healthStep = target.closest("[data-health-step-cancel]");
    if (healthStep) {
      healthStep.disabled = true;
      const { healthTask, healthStepCancel } = healthStep.dataset;
      try {
        const { cancelled } = await api(`/tasks/${encodeURIComponent(healthTask)}/steps/${encodeURIComponent(healthStepCancel)}/cancel`, { method: "POST" });
        toast(cancelled ? "已请求取消该观察步骤" : "该步骤已结束或不支持单步取消");
        await loadAll(true);
        if ($("#profile-dialog").open) await openHealthDetail(healthTask);
      } catch (error) { toast(error.message, true); }
      finally { healthStep.disabled = false; }
      return;
    }
    const monitorButton = target.closest("[data-monitor-disable]");
    if (monitorButton) {
      if (!confirm("关闭定时巡检并取消本次尚在运行的巡检？")) return;
      monitorButton.disabled = true;
      try {
        await api(`/health-monitors/${encodeURIComponent(monitorButton.dataset.monitorDisable)}`, {
          method: "PUT", body: JSON.stringify({ interval_minutes: null }),
        });
        await loadAll(true);
        toast("已关闭定时巡检");
      } catch (error) { toast(error.message, true); }
      finally { monitorButton.disabled = false; }
      return;
    }
    const alertButton = target.closest("[data-open-risk-alert], [data-risk-status]");
    if (alertButton) {
      try {
        if (alertButton.dataset.openRiskAlert) {
          await openRiskAlert(alertButton.dataset.openRiskAlert);
        } else {
          const { riskStatus, riskPrevious, riskId } = alertButton.dataset;
          alertButton.disabled = true;
          const { alert } = await api(`/alerts/${encodeURIComponent(riskId)}`, {
            method: "PATCH", body: JSON.stringify({ previous_status: riskPrevious, status: riskStatus }),
          });
          await loadAll(true);
          if ($("#profile-dialog").open) await openRiskAlert(riskId, alert);
        }
      } catch (error) { toast(error.message, true); }
      finally { alertButton.disabled = false; }
      return;
    }
    const stopButton = target.closest("[data-execution-stop]");
    if (stopButton) {
      const { executionStop, executionAgent, executionSubject, executionValue } = stopButton.dataset;
      const stopped = executionValue === "true";
      if (stopped && !confirm("暂停此范围的新执行，并请求取消命中的在途操作？已产生的副作用不会回滚。")) return;
      stopButton.disabled = true;
      try {
        const { interrupted } = await api("/execution-controls", {
          method: "PATCH",
          body: JSON.stringify({ scope: executionStop, agent_id: executionAgent, subject: executionSubject, stopped }),
        });
        await loadAll(true);
        toast(stopped ? `已暂停指定范围；${interrupted} 个在途请求被拦截或请求取消` : "已恢复接收新任务；旧任务不会续跑");
      } catch (error) { toast(error.message, true); }
      finally { stopButton.disabled = false; }
      return;
    }
    const planButton = target.closest("[data-operation-detail], [data-plan-command], [data-plan-step-cancel]");
    if (planButton) {
      try {
        if (planButton.dataset.operationDetail) await openOperationDetail(planButton.dataset.operationDetail);
        else await handlePlanControl(planButton);
      } catch (error) { toast(error.message, true); }
      return;
    }
    const capabilitiesButton = target.closest("[data-agent-capabilities]");
    if (capabilitiesButton) openAgentCapabilitiesDialog(capabilitiesButton.dataset.agentCapabilities);
    const shellPolicyButton = target.closest("[data-shell-policy]");
    if (shellPolicyButton) {
      openShellPolicyForm(shellPolicyButton.dataset.shellPolicy);
      return;
    }
    const planPolicyButton = target.closest("[data-plan-policy]");
    if (planPolicyButton) {
      openPlanPolicyForm(planPolicyButton.dataset.planPolicy);
      return;
    }
    const profileButton = target.closest("[data-profile-details]");
    if (profileButton) {
      const capabilitiesDialog = $("#agent-capabilities-dialog");
      const returnAgentId = capabilitiesDialog.open ? capabilitiesDialog.dataset.agentId : "";
      if (capabilitiesDialog.open) capabilitiesDialog.close();
      openProfileDialog(profileButton.dataset.profileDetails, returnAgentId);
    }
    const agentId = target.dataset.editAgent;
    if (agentId) openAgentMetadataForm(state.agents.find((item) => item.id === agentId));
    if (target.dataset.revokeAgent && confirm("撤销后该 Agent 的设备凭据将永久失效。继续吗？")) { await api(`/agents/${target.dataset.revokeAgent}`, { method: "DELETE" }); await loadAll(true); toast("Agent 已撤销"); }
    if (target.dataset.revokeKey && confirm("撤销后使用该 Key 的 MCP 客户端将立即失去访问权限。继续吗？")) { await api(`/mcp-keys/${target.dataset.revokeKey}`, { method: "DELETE" }); await loadAll(true); toast("MCP Key 已撤销"); }
    if (target.dataset.revokeToken && confirm("确定撤销这个注册令牌吗？")) { await api(`/enrollment-tokens/${target.dataset.revokeToken}`, { method: "DELETE" }); await loadAll(true); toast("注册令牌已撤销"); }
    if (target.dataset.downloadBundle) { window.location.assign(`/api/agent-bundles/${target.dataset.downloadBundle}/download`); }
    if (target.dataset.healthCancel && confirm("确定取消这次健康检查吗？")) {
      try {
        const { cancelled } = await api(`/tasks/${target.dataset.healthCancel}/cancel`, { method: "POST" });
        await loadAll(true);
        toast(cancelled ? "已请求取消健康检查" : "任务已经结束");
      } catch (error) { toast(error.message, true); }
    }
    if (target.dataset.healthDetail) {
      try { await openHealthDetail(target.dataset.healthDetail); }
      catch (error) { toast(error.message, true); }
    }
  };
}

function setAuthMode(mode) {
  state.authMode = mode;
  $("#login-tab").classList.toggle("active", mode === "login");
  $("#register-tab").classList.toggle("active", mode === "register");
  $("#auth-title").textContent = mode === "login" ? "登录网关" : "创建账号";
  $("#auth-submit").textContent = mode === "login" ? "登录" : "注册";
  $("#auth-password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("#auth-error").textContent = "";
}

async function copyText(value, message) {
  await navigator.clipboard.writeText(value);
  toast(message);
}

bindEvents();
loadAll(true);
setInterval(() => { if (state.user) loadAll(true); }, 15000);
