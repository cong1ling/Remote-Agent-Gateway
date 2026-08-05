const state = { user: null, agents: [], keys: [], tokens: [], mcpUrl: "", wsUrl: "", authMode: "login", view: "overview" };
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
    const me = await api("/me");
    state.user = me.user;
    state.mcpUrl = me.mcp_url;
    state.wsUrl = me.agent_websocket_url;
    const [agents, keys, tokens] = await Promise.all([api("/agents"), api("/mcp-keys"), api("/enrollment-tokens")]);
    state.agents = agents.agents;
    state.keys = keys.keys;
    state.tokens = tokens.tokens;
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
  const content = state.agents.length ? state.agents.map(agentCard).join("") : emptyState("还没有 Agent", "创建一次性注册令牌并在远程环境运行启动命令");
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
  }).join("") : emptyState("还没有注册令牌", "创建令牌后只会展示一次完整启动命令");
}

function render() {
  renderMetrics();
  renderAgents();
  renderKeys();
  renderTokens();
}

function switchView(view) {
  const titles = { overview: "概览", agents: "远程 Agent", keys: "MCP Keys", tokens: "注册令牌" };
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
  showSecret("Agent 注册令牌已创建", result.secret, result.command);
  await loadAll(true);
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
  $$('[data-action="new-token"]').forEach((button) => button.onclick = () => openForm({ eyebrow: "Agent Enrollment", title: "创建注册令牌", submit: "创建令牌", fields: '<label>Agent 名称<input name="name" required maxlength="80" placeholder="例如 GPU Notebook"></label>', onSubmit: (data) => createToken(data.get("name")) }));
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
