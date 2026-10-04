/* OCI Panel 前端核心：API、登录、页签、概览、云账号、域名监控、邮件、设置 */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const PANEL_PREFIX = "/" + location.pathname.split("/")[1];
const panelPath = path => PANEL_PREFIX + path;

const state = {
  accounts: [],
  sessions: [],
  instAccount: "",
  editingAccountId: null,
  editingSessionId: null,
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function uiIcon(name) {
  const paths = {
    home: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6h.01M7 17h.01M11 6h6M11 17h6"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    play: '<path d="m8 4 13 8-13 8Z"/>',
    copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v12h5"/>',
    edit: '<path d="m15 4 5 5M4 20l4-1L21 6l-5-5L3 14Z"/>',
    trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    folder: '<path d="M3 6V4h7l2 3h9v13H3Z"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c6 5 6 13 0 18-6-5-6-13 0-18Z"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.server}</svg>`;
}

async function api(path, opts = {}) {
  if (path.startsWith('/api/ssh/sftp/') || path === '/api/ssh/monitor' || path === '/api/ssh/forwards') {
    const url = new URL(path, location.origin);
    const sid = opts.body?.session_id ?? Number(url.searchParams.get('session_id'));
    const revision = state.sessions.find(s => s.id === sid)?.connection_revision;
    if (revision && opts.body && opts.body.expected_revision === undefined) opts = {...opts, body:{...opts.body, expected_revision:revision}};
    if (revision && !opts.body) { url.searchParams.set('expected_revision', revision); path = url.pathname + url.search; }
  }
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(panelPath(path), init);
  if (res.status === 401) { showLogin(); throw new Error("未登录"); }
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const d = data?.detail;
    const message = typeof d === "string" ? d.trim() : d ? JSON.stringify(d) : "";
    throw new Error(message || `请求失败（HTTP ${res.status}），请检查服务状态后重试`);
  }
  return data;
}

let toastTimer;
function toast(msg, ok = true) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (ok ? "" : " err");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 7000);
}

const PLATFORM_LABEL = { oci: "甲骨文" };

/* ---------- 登录 / 启动 ---------- */

function showLogin() {
  $("#app").classList.add("hide");
  $("#hdr-user").classList.add("hide");
  $("#view-login").classList.remove("hide");
}

async function boot() {
  try { await api("/api/me"); } catch { return; }
  switchView("ssh");
  $("#view-login").classList.add("hide");
  $("#app").classList.remove("hide");
  $("#hdr-user").classList.remove("hide");
  await Promise.all([reloadAccounts(), loadSessions()]);
  api("/api/panel/version").then(r => $("#ver").textContent = "v" + r.version).catch(() => {});
  startTimers();
}

async function doLogin() {
  $("#login-err").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: {
      username: $("#login-user").value, password: $("#login-pass").value } });
    $("#login-pass").value = "";
    await boot();
  } catch (e) { $("#login-err").textContent = e.message; }
}

/* ---------- 页签 ---------- */

const VIEW_LOADERS = {
  overview: () => loadOverview(),
  instances: () => { if (state.instAccount) window.loadInstances(); },
  launch: () => { window.loadTasks(); window.fillLaunchMeta(); },
  volumes: () => window.loadVolumes(),
  network: () => window.loadNetwork(),
  users: () => {},
  objects: () => {},
  domains: () => loadDomains(),
  ssh: () => window.sshViewLoaded(),
  mail: () => loadMail(),
  accounts: () => renderAccounts(),
  settings: () => loadSettings(),
};

let curView = "overview";
function switchView(name) {
  curView = name;
  $$("nav button").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach(v => v.classList.toggle("hide", v.id !== "view-" + name));
  const fn = VIEW_LOADERS[name];
  if (fn) Promise.resolve(fn()).catch(e => toast(e.message, false));
}

/* ---------- 账号基础 ---------- */

async function reloadAccounts() {
  const r = await api("/api/accounts");
  state.accounts = r.data;
  const opts = state.accounts.map(a =>
    `<option value="${a.id}">${esc(a.name)}（${esc(PLATFORM_LABEL[a.platform] || a.platform)}${a.region ? " · " + esc(a.region) : ""}）</option>`).join("");
  for (const [selId, key] of [["#inst-account", "instAccount"]]) {
    const sel = $(selId);
    if (!sel) continue;
    const prev = sel.value;
    sel.innerHTML = opts || `<option value="">（先添加账号）</option>`;
    if (prev && state.accounts.some(a => String(a.id) === prev)) sel.value = prev;
  }
  for (const selId of ["#l-account", "#a1-account", "#vol-account", "#net-account", "#usr-account", "#os-account", "#mo-account"]) {
    const sel = $(selId);
    if (!sel) continue;
    const ociOnly = state.accounts.filter(a => a.platform === "oci");
    const prev = sel.value;
    sel.innerHTML = (ociOnly.length ? ociOnly : state.accounts).map(a =>
      `<option value="${a.id}">${esc(a.name)}（${esc(a.region)}）</option>`).join("")
      || `<option value="">（先添加 OCI 账号）</option>`;
    if (prev && state.accounts.some(a => String(a.id) === prev)) sel.value = prev;
  }
  state.instAccount = $("#inst-account").value;
  window.syncSshSessionSelects && window.syncSshSessionSelects();
  renderAccounts();
  window.syncDashboardAccounts && window.syncDashboardAccounts();
}

/* ---------- 概览 ---------- */

async function loadOverview() {
  try {
    const r = await api("/api/overview");
    $("#ov-cards").innerHTML = `
      <div class="stat"><b>${r.accounts}</b><span>云账号</span></div>
      <div class="stat"><b>${r.instances}</b><span>实例</span></div>
      <div class="stat"><b>${r.running_tasks}</b><span>抢机任务</span></div>
      <div class="stat"><b>${r.ssh_sessions}</b><span>SSH 会话</span></div>
      <div class="stat"><b>${r.domains}</b><span>监控域名</span></div>`;
    $("#ov-states").innerHTML = Object.entries(r.states).map(([k, v]) =>
      `<span class="chip">${esc(k)}: ${v}</span>`).join("") || `<span class="muted">还没有实例</span>`;
    $("#ov-errors").innerHTML = r.errors.map(e => `<div>⚠️ ${esc(e)}</div>`).join("")
      || `<span>全部账号连接正常</span>`;
  } catch (e) { toast(e.message, false); }
}

$("#btn-panel-log").addEventListener("click", async () => {
  const box = $("#panel-log");
  if (!box.classList.contains("hide")) { box.classList.add("hide"); return; }
  try {
    const r = await api("/api/panel/log");
    box.textContent = r.log || "(空)";
    box.classList.remove("hide");
  } catch (e) { toast(e.message, false); }
});
$("#btn-panel-restart").addEventListener("click", async () => {
  if (!confirm("重启面板服务？")) return;
  try { await api("/api/panel/restart", { method: "POST" }); toast("面板重启中，稍后自动恢复"); }
  catch (e) { toast(e.message, false); }
});
$("#btn-panel-upgrade").addEventListener("click", async () => {
  try { const r = await api("/api/panel/upgrade", { method: "POST" }); toast(r.message || JSON.stringify(r), r.ok); }
  catch (e) { toast(e.message, false); }
});

/* ---------- 云账号 ---------- */

function showPlatformFields() {}

function accountParamsFromForm() {
  return {
    user_ocid: $("#ao-user").value.trim(), tenancy_ocid: $("#ao-tenancy").value.trim(),
    fingerprint: $("#ao-fp").value.trim(), compartment_id: $("#ao-comp").value.trim(),
    private_key: $("#ao-pk").value, proxy_url: $("#a-proxy").value.trim(),
  };
}

function fillAccountForm(a) {
  const p = a.params || {};
  const set = (id, val) => { $(id).value = val || ""; };
  set("#a-name", a.name); set("#a-region", a.region);
  set("#ao-user", p.user_ocid); set("#ao-tenancy", p.tenancy_ocid);
  set("#ao-fp", p.fingerprint); set("#ao-comp", p.compartment_id); set("#ao-pk", "");
  set("#a-proxy", p.proxy_url); set("#a-traffic", a.traffic_limit_gb);
  $("#a-trafficact").value = a.traffic_action || "notify";
  $("#a-auto").checked = !!a.auto_restart;
}

$("#a-platform").addEventListener("change", () => showPlatformFields($("#a-platform").value));
$("#btn-new-account").addEventListener("click", () => {
  state.editingAccountId = null;
  $("#a-form-title").textContent = "添加账号";
  $$("#account-form input, #account-form textarea").forEach(el => {
    if (el.type !== "checkbox") el.value = "";
  });
  $("#a-auto").checked = false;
  $("#a-platform").value = "oci";
  showPlatformFields("oci");
  $("#account-form").classList.remove("hide");
});
$("#btn-cancel-account").addEventListener("click", () => {
  $("#account-form").classList.add("hide");
  state.editingAccountId = null;
});

$("#btn-save-account").addEventListener("click", async () => {
  const platform = $("#a-platform").value;
  const body = {
    platform, name: $("#a-name").value.trim(), region: $("#a-region").value.trim(),
    params: accountParamsFromForm(platform),
    auto_restart: $("#a-auto").checked,
    traffic_limit_gb: Number($("#a-traffic").value) || 0,
    traffic_action: $("#a-trafficact").value,
  };
  if (!body.name) { toast("请填写名称", false); return; }
  const previousAccount = state.accounts.find(a => a.id === state.editingAccountId);
  if (previousAccount?.params?.proxy_url && !body.params.proxy_url) {
    if (!confirm("移除这个账号的代理？保存后该账号的 OCI 请求将通过面板服务器直连。")) return;
    body.remove_proxy = true;
  }
  if (platform === "oci" && !state.editingAccountId &&
      !body.params.private_key.trim().startsWith("-----BEGIN")) {
    toast("OCI 私钥请粘贴 PEM 全文（-----BEGIN 开头）", false); return;
  }
  try {
    if (state.editingAccountId) {
      await api(`/api/accounts/${state.editingAccountId}`, { method: "PUT", body });
    } else {
      await api("/api/accounts", { method: "POST", body });
    }
    toast("已保存");
    state.editingAccountId = null;
    $("#account-form").classList.add("hide");
    await reloadAccounts();
  } catch (e) { toast(e.message, false); }
});

function renderAccounts() {
  const tb = $("#acct-table tbody");
  tb.innerHTML = state.accounts.map(a => `<tr>
    <td><span class="chip">${esc(PLATFORM_LABEL[a.platform] || a.platform)}</span></td>
    <td>${esc(a.name)}</td><td>${esc(a.region)}</td>
    <td>${a.auto_restart ? "✅" : "—"}</td>
    <td>${a.traffic_limit_gb ? esc(a.traffic_limit_gb) + "GB/" + (a.traffic_action === "stop" ? "关停" : "通知") : "—"}</td>
    <td class="ops">
      <button data-aact="test" data-id="${a.id}">测试</button>
      <button data-aact="stats" data-id="${a.id}">配额</button>
      <button data-aact="cost" data-id="${a.id}">费用</button>
      <button data-aact="edit" data-id="${a.id}">编辑</button>
      <button data-aact="copy" data-id="${a.id}" data-region="${esc(a.region)}">复制到新区域</button>
      <button data-aact="delete" class="danger" data-id="${a.id}" data-name="${esc(a.name)}">删除</button>
    </td></tr>`).join("") || `<tr><td colspan="6" class="muted">还没有账号</td></tr>`;
}

$("#acct-table tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-aact]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const acct = state.accounts.find(a => a.id === id);
  try {
    if (btn.dataset.aact === "test") {
      toast("测试连接中…");
      const r = await api(`/api/accounts/${id}/test`, { method: "POST" });
      r.ok ? toast("✅ 连接正常: " + (r.ads || []).join(" / "))
           : toast("❌ " + r.error, false);
    } else if (btn.dataset.aact === "stats") {
      const r = await api(`/api/accounts/${id}/stats`);
      showLogModal(`${acct.name} 配额`, `租户：${r.tenancy_name}\n主区域：${r.home_region}\n` +
        r.limits.map(l => `${l.name}: 已用 ${l.used ?? "未知"} / 上限 ${l.limit} ${l.ad}`).join("\n") +
        (r.cost_note ? "\n" + r.cost_note : ""));
    } else if (btn.dataset.aact === "cost") {
      const r = await api(`/api/accounts/${id}/usage?days=30`);
      showLogModal(`${acct.name} 近30天费用`, `合计：${r.total}\n` +
        Object.entries(r.daily).map(([date, value]) => `${date}: ${value}`).join("\n"));
    } else if (btn.dataset.aact === "edit") {
      state.editingAccountId = id;
      $("#a-form-title").textContent = `编辑账号：${acct.name}`;
      $("#a-platform").value = acct.platform;
      showPlatformFields(acct.platform);
      fillAccountForm(acct);
      $("#account-form").classList.remove("hide");
      window.scrollTo({ top: 0, behavior: "smooth" });
    } else if (btn.dataset.aact === "copy") {
      const region = prompt(`把「${acct.name}」复制到新区域（如 ap-tokyo-1）：`, acct.region);
      if (!region) return;
      await api(`/api/accounts/${id}/copy-region`, { method: "POST", body: { region } });
      toast("已复制，去列表查看");
      await reloadAccounts();
    } else if (btn.dataset.aact === "delete") {
      if (!confirm(`删除账号「${acct.name}」？只删除面板配置，不影响云端。`)) return;
      await api(`/api/accounts/${id}`, { method: "DELETE" });
      toast("已删除");
      await reloadAccounts();
    }
  } catch (err) { toast(err.message, false); }
});

/* ---------- 域名监控 ---------- */

async function loadDomains() {
  try {
    const r = await api("/api/domains");
    $("#dom-table tbody").innerHTML = r.data.map(d => {
      const st = (() => { try { return JSON.parse(d.last_state || "{}"); } catch { return {}; } })();
      const fmt = v => v === null || v === undefined ? "—" : (v + " 天");
      return `<tr>
        <td>${esc(d.name)}</td>
        <td>${fmt(st.ssl_days)}</td><td>${fmt(st.domain_days)}</td>
        <td class="muted" style="font-size:12px">${esc(st.error || "正常")}</td>
        <td class="ops"><button data-dact="check" data-id="${d.id}">立即检查</button>
          <button data-dact="del" class="danger" data-id="${d.id}">删除</button></td></tr>`;
    }).join("") || `<tr><td colspan="5" class="muted">还没有监控域名</td></tr>`;
  } catch (e) { toast(e.message, false); }
}

$("#btn-dom-add").addEventListener("click", async () => {
  try {
    await api("/api/domains", { method: "POST", body: { name: $("#dom-name").value } });
    $("#dom-name").value = "";
    toast("已添加，每天自动检查");
    loadDomains();
  } catch (e) { toast(e.message, false); }
});
$("#btn-dom-import").addEventListener("click", async () => {
  try { const r = await api("/api/cf/import-domains", { method: "POST" }); toast(`导入完成：CF ${r.zones} 个域名，新增 ${r.added}`); loadDomains(); }
  catch (e) { toast(e.message, false); }
});
$("#btn-dom-checkall").addEventListener("click", async () => {
  toast("检查中…");
  const r = await api("/api/domains");
  for (const d of r.data) {
    try { await api(`/api/domains/${d.id}/check`, { method: "POST" }); } catch {}
  }
  toast("检查完成");
  loadDomains();
});
$("#dom-table tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-dact]");
  if (!btn) return;
  const id = btn.dataset.id;
  try {
    if (btn.dataset.dact === "check") {
      toast("检查中…");
      const r = await api(`/api/domains/${id}/check`, { method: "POST" });
      toast(`SSL 剩 ${r.ssl_days ?? "?"} 天，域名注册剩 ${r.domain_days ?? "?"} 天 ${r.error ? "（" + r.error + "）" : ""}`);
      loadDomains();
    } else {
      await api(`/api/domains/${id}`, { method: "DELETE" });
      loadDomains();
    }
  } catch (err) { toast(err.message, false); }
});

/* ---------- 邮件服务 ---------- */

async function loadMail() {
  try {
    const r = await api("/api/mail/smtp");
    $("#m-host").value = r.email_host || ""; $("#m-port").value = r.email_port || "465";
    $("#m-ssl").value = r.email_ssl || "1"; $("#m-user").value = r.email_user || "";
    $("#m-pass").value = r.email_pass || ""; $("#m-from").value = r.email_from || "";
    $("#m-to").value = r.email_to || "";
  } catch (e) { toast(e.message, false); }
}
$("#btn-mail-save").addEventListener("click", async () => {
  try {
    await api("/api/mail/smtp", { method: "POST", body: {
      email_host: $("#m-host").value, email_port: $("#m-port").value,
      email_ssl: $("#m-ssl").value, email_user: $("#m-user").value,
      email_pass: $("#m-pass").value, email_from: $("#m-from").value,
      email_to: $("#m-to").value,
    }});
    toast("SMTP 配置已保存");
  } catch (e) { toast(e.message, false); }
});
$("#btn-mail-test").addEventListener("click", async () => {
  $("#mail-result").textContent = "发送中…";
  try {
    await api("/api/mail/smtp", { method: "POST", body: {
      email_host: $("#m-host").value, email_port: $("#m-port").value,
      email_ssl: $("#m-ssl").value, email_user: $("#m-user").value,
      email_pass: $("#m-pass").value, email_from: $("#m-from").value,
      email_to: $("#m-to").value,
    }});
    await api("/api/mail/test", { method: "POST", body: { to: $("#m-to").value } });
    $("#mail-result").textContent = "✅ 已发送，查收邮箱";
  } catch (e) { $("#mail-result").textContent = "❌ " + e.message; }
});
$("#btn-mo-setup").addEventListener("click", async () => {
  $("#mo-result").textContent = "创建中…";
  try {
    const r = await api("/api/mail/oci-setup", { method: "POST", body: {
      account_id: Number($("#mo-account").value), domain: $("#mo-domain").value.trim(),
      selector: $("#mo-selector").value.trim() || "ocipanel",
      push_dns: $("#mo-push").checked, from_addr: $("#mo-from").value.trim(),
    }});
    $("#mo-result").textContent = r.push_error ? ("DNS推送失败: " + r.push_error) :
      r.sender_error ? ("域名已创建，发件人仍需处理：" + r.sender_error) : "域名已创建，请添加 DNS 并等待验证";
    const lines = (r.dns || []).map(d => `${d.type} ${d.name} -> ${d.content}`).join("\n");
    if (lines) toast("请添加这些 DNS 记录：\n" + lines + (r.pushed ? "\n已自动推送到: " + r.pushed.join(", ") : ""));
  } catch (e) { $("#mo-result").textContent = "❌ " + e.message; }
});
$("#btn-mo-list").addEventListener("click", async () => {
  try {
    const r = await api(`/api/mail/oci-domains?account_id=${$("#mo-account").value}`);
    $("#mo-table tbody").innerHTML = r.data.map(d => `<tr>
      <td>${esc(d.name)}</td><td>${esc(d.state)}</td>
      <td class="ops"><button data-mod="${d.id}" class="danger">删除</button></td></tr>`).join("")
      || `<tr><td colspan="3" class="muted">没有发信域名</td></tr>`;
  } catch (e) { toast(e.message, false); }
});
$("#mo-table tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-mod]");
  if (!btn) return;
  try {
    await api(`/api/mail/oci-domains/${btn.dataset.mod}?account_id=${$("#mo-account").value}`, { method: "DELETE" });
    $("#btn-mo-list").click();
  } catch (err) { toast(err.message, false); }
});

/* ---------- 设置 ---------- */

async function loadSettings() {
  try {
    $("#mcp-endpoint").textContent = location.origin + panelPath("/mcp");
    await loadCfSettings();
    const r = await api("/api/settings/notify");
    $("#s-bark").value = r.notify_bark_url || ""; $("#s-hook").value = r.notify_webhook || "";
    const a = await api("/api/settings/alerts");
    $("#al-cpu").value = a.alert_cpu; $("#al-mem").value = a.alert_mem; $("#al-disk").value = a.alert_disk;
    const m = await api("/api/settings/mcp-token");
    $("#mcp-token").textContent = m.token;
  } catch (e) { toast(e.message, false); }
}

async function loadCfSettings() {
  const r = await api("/api/cf/settings");
  $("#cf-token").value = "";
  $("#cf-key").value = "";
  $("#cf-email").value = r.email || "";
  $("#cf-clear-token").checked = false;
  $("#cf-clear-key").checked = false;
  $("#cf-settings-state").textContent = `Token：${r.has_token ? "已保存" : "未配置"} · Global Key：${r.has_key ? "已保存" : "未配置"}`;
}

$("#btn-cf-settings-save").addEventListener("click", async () => {
  try {
    const clearToken = $("#cf-clear-token").checked, clearKey = $("#cf-clear-key").checked;
    if ((clearToken || clearKey) && !confirm("确认删除勾选的 Cloudflare 凭据？")) return;
    await api("/api/cf/settings", { method: "POST", body: {
      api_token: $("#cf-token").value, email: $("#cf-email").value,
      global_key: $("#cf-key").value, clear_token: clearToken, clear_global_key: clearKey,
    }});
    await loadCfSettings(); toast("Cloudflare 配置已保存");
  } catch (e) { toast(e.message, false); }
});
$("#btn-cf-test").addEventListener("click", async () => {
  try {
    $("#cf-settings-state").textContent = "正在测试…";
    const r = await api("/api/cf/test", { method: "POST" });
    $("#cf-settings-state").textContent = `连接成功，可读取 ${r.zones} 个区域`;
  } catch (e) { $("#cf-settings-state").textContent = e.message; }
});

$("#btn-save-notify").addEventListener("click", async () => {
  try {
    await api("/api/settings/notify", { method: "POST", body: {
      notify_bark_url: $("#s-bark").value, notify_webhook: $("#s-hook").value,
    }});
    toast("通知设置已保存");
  } catch (e) { toast(e.message, false); }
});
$("#btn-test-notify").addEventListener("click", async () => {
  $("#notify-result").textContent = "发送中…";
  try {
    await api("/api/settings/notify", { method: "POST", body: {
      notify_bark_url: $("#s-bark").value, notify_webhook: $("#s-hook").value,
    }});
    const r = await api("/api/settings/notify/test", { method: "POST" });
    $("#notify-result").textContent = r.ok ? "结果: " + JSON.stringify(r.results) : (r.message || "未配置");
  } catch (e) { $("#notify-result").textContent = "❌ " + e.message; }
});
$("#btn-save-alerts").addEventListener("click", async () => {
  try {
    await api("/api/settings/alerts", { method: "POST", body: {
      alert_cpu: $("#al-cpu").value, alert_mem: $("#al-mem").value, alert_disk: $("#al-disk").value }});
    toast("阈值已保存");
  } catch (e) { toast(e.message, false); }
});
$("#btn-mcp-regen").addEventListener("click", async () => {
  try { const r = await api("/api/settings/mcp-token/regenerate", { method: "POST" }); $("#mcp-token").textContent = r.token; }
  catch (e) { toast(e.message, false); }
});
$("#btn-save-pass").addEventListener("click", async () => {
  try {
    await api("/api/settings/password", { method: "POST", body: {
      old_password: $("#p-old").value, new_password: $("#p-new").value }});
    showLogin(); toast("密码已修改，请重新登录"); $("#p-old").value = ""; $("#p-new").value = "";
  } catch (e) { toast(e.message, false); }
});

/* ---------- 全局 ---------- */

$$("nav button").forEach(b => b.addEventListener("click", () => switchView(b.dataset.view)));
$("#btn-login").addEventListener("click", doLogin);
$("#login-user").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("#login-pass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("#btn-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.reload();
});

let timersStarted = false;
function startTimers() {
  if (timersStarted) return;
  timersStarted = true;
  setInterval(async () => {
    if (document.hidden || $("#app").classList.contains("hide")) return;
    if (curView === "instances" && $("#inst-auto").checked && state.instAccount) window.loadInstances(true);
    if (curView === "launch") window.loadTasks(true);
  }, 15000);
}

window.addEventListener("DOMContentLoaded", () => boot().catch(e => { showLogin(); toast(e.message, false); }));
