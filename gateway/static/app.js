const state = { user: null, agents: [], keys: [], tokens: [], catalog: [], bundles: [], mcpUrl: "", wsUrl: "", authMode: "login", view: "overview" };
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
    const [agents, keys, tokens, catalog, bundles] = await Promise.all([api("/agents"), api("/mcp-keys"), api("/enrollment-tokens"), api("/profile-catalog"), api("/agent-bundles")]);
    state.agents = agents.agents;
    state.keys = keys.keys;
    state.tokens = tokens.tokens;
    state.catalog = catalog.profiles;
    state.bundles = bundles.bundles;
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

function agentCard(agent) {
  const profiles = (agent.profiles || []).map((profile) => `<span class="profile">${escapeHtml(profile)}</span>`).join("");
  return `<article class="agent-card">
    <div class="agent-title"><h3>${escapeHtml(agent.name)}</h3><p>${escapeHtml(agent.description || "暂无用途描述")}</p></div>
    <div class="agent-meta"><span class="status ${agent.online ? "online" : ""}">${agent.online ? "在线" : "离线"}</span>${profiles}<span>${formatTime(agent.last_seen_at)}</span></div>
    <div class="row-actions"><button class="secondary" data-edit-agent="${agent.id}">编辑</button><button class="danger" data-revoke-agent="${agent.id}">撤销</button></div>
  </article>`;
}

function renderAgents() {
  const content = state.agents.length ? state.agents.map(agentCard).join("") : emptyState("还没有 Agent", "前往 Agent 工作台组装并注册第一台远程设备");
  $("#agents-list").innerHTML = content;
  $("#overview-agents").innerHTML = state.agents.length ? state.agents.slice(0, 4).map(agentCard).join("") : content;
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
    <div><h3>${escapeHtml(key.name)}</h3><p>创建于 ${formatTime(key.created_at)}</p></div>
    <code>${escapeHtml(key.prefix)}…</code>
    <time>最近使用：${formatTime(key.last_used_at)}</time>
    <div class="row-actions"><button class="danger" data-revoke-key="${key.id}">撤销</button></div>
  </article>`).join("") : emptyState("没有有效的 MCP Key", "为每个 Claude 或 WorkBuddy 客户端分别创建一个 Key");
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
    <div class="catalog-meta"><span class="catalog-status">${profile.status === "stable" ? "稳定" : escapeHtml(profile.status)}</span>${profile.runtimes.map((runtime) => `<span>${escapeHtml(runtime)}</span>`).join("")}</div>
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

function render() {
  renderMetrics();
  renderAgents();
  renderKeys();
  renderTokens();
  renderBuilder();
}

function switchView(view) {
  const titles = { overview: "概览", builder: "Agent 工作台", agents: "远程 Agent", keys: "MCP Keys", tokens: "手动接入" };
  state.view = view;
  $$("#main-nav button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
  $("#page-title").textContent = titles[view];
  $(".sidebar").classList.remove("open");
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

async function createKey(name) {
  const result = await api("/mcp-keys", { method: "POST", body: JSON.stringify({ name }) });
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
    fields: `<label>Agent 名称<input name="name" required maxlength="80" placeholder="例如 GPU Notebook"></label><label>用途描述<textarea name="description" maxlength="500" rows="3" placeholder="告诉 AI 这台设备适合做什么"></textarea></label><div class="profile-options">${options}</div>`,
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
  $("#mobile-menu").onclick = () => $(".sidebar").classList.toggle("open");
  $$("#main-nav button").forEach((button) => button.onclick = () => switchView(button.dataset.view));
  $("#new-key-button").onclick = () => openForm({ eyebrow: "MCP Access", title: "创建 MCP Key", submit: "创建 Key", fields: '<label>名称<input name="name" required maxlength="80" placeholder="例如 Claude Desktop"></label>', onSubmit: (data) => createKey(data.get("name")) });
  $$('[data-action="new-bundle"]').forEach((button) => button.onclick = openBundleForm);
  $$('[data-action="new-token"]').forEach((button) => button.onclick = () => openForm({ eyebrow: "Advanced Enrollment", title: "创建手动接入 Token", submit: "创建 Token", fields: '<label>令牌名称或备注<input name="name" required maxlength="80" placeholder="例如 CI 临时环境"></label>', onSubmit: (data) => createToken(data.get("name")) }));
  $$('[data-action="go-builder"]').forEach((button) => button.onclick = () => switchView("builder"));
  $("#dialog-form").onsubmit = async (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") { $("#form-dialog").close(); return; }
    const dialog = $("#form-dialog");
    const button = $("#dialog-submit");
    button.disabled = true;
    try { await dialog.onSubmitAction(new FormData(event.currentTarget)); dialog.close(); }
    catch (error) { $("#dialog-error").textContent = error.message; }
    finally { button.disabled = false; }
  };
  $("#secret-close").onclick = () => $("#secret-dialog").close();
  $("#copy-secret").onclick = () => copyText($("#secret-value").value, "Secret 已复制");
  $("#copy-command").onclick = () => copyText($("#command-value").value, "启动命令已复制");
  $(".copy-endpoint").onclick = () => copyText(state.mcpUrl, "MCP Endpoint 已复制");
  document.body.onclick = async (event) => {
    const target = event.target;
    const agentId = target.dataset.editAgent;
    if (agentId) {
      const agent = state.agents.find((item) => item.id === agentId);
      openForm({ eyebrow: "Agent Metadata", title: "编辑 Agent", submit: "保存", fields: `<label>名称<input name="name" value="${escapeHtml(agent.name)}" required maxlength="80"></label><label>用途描述<textarea name="description" maxlength="500" rows="4">${escapeHtml(agent.description)}</textarea></label>`, onSubmit: async (data) => { await api(`/agents/${agentId}`, { method: "PATCH", body: JSON.stringify({ name: data.get("name"), description: data.get("description") }) }); await loadAll(true); } });
    }
    if (target.dataset.revokeAgent && confirm("撤销后该 Agent 的设备凭据将永久失效。继续吗？")) { await api(`/agents/${target.dataset.revokeAgent}`, { method: "DELETE" }); await loadAll(true); toast("Agent 已撤销"); }
    if (target.dataset.revokeKey && confirm("撤销后使用该 Key 的 MCP 客户端将立即失去访问权限。继续吗？")) { await api(`/mcp-keys/${target.dataset.revokeKey}`, { method: "DELETE" }); await loadAll(true); toast("MCP Key 已撤销"); }
    if (target.dataset.revokeToken && confirm("确定撤销这个注册令牌吗？")) { await api(`/enrollment-tokens/${target.dataset.revokeToken}`, { method: "DELETE" }); await loadAll(true); toast("注册令牌已撤销"); }
    if (target.dataset.downloadBundle) { window.location.assign(`/api/agent-bundles/${target.dataset.downloadBundle}/download`); }
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
