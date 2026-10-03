/* Local-only assets and authenticated metrics; no analytics or external fonts. */
(function () {
  const bytes = (n, rate = false) => {
    if (n == null || !Number.isFinite(Number(n))) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}${rate ? "/s" : ""}`;
  };
  let metricPending = false;
  async function pollMetrics() {
    if (document.hidden || $("#app").classList.contains("hide") || metricPending) {
      if ($("#app").classList.contains("hide")) $("#host-metrics").classList.add("hide");
      return;
    }
    metricPending = true;
    try {
      const r = await api("/api/panel/metrics");
      $("#host-metrics").classList.remove("hide");
      $("#host-metrics").title = r.available ? `${r.scope === "container" ? "容器 / LXC 限额" : "部署服务器"} · CPU ${r.cores} 核 · 磁盘 ${bytes(r.disk_used)} / ${bytes(r.disk_total)}` : r.message;
      $("#host-cpu").textContent = r.cpu == null ? "—" : `${r.cpu}%`;
      $("#host-cpu-meter").style.width = `${r.cpu || 0}%`;
      $("#host-mem").textContent = r.available ? `${bytes(r.memory_used)} / ${bytes(r.memory_total)}` : "不可用";
      $("#host-mem-meter").style.width = `${Math.min(100, r.memory_percent || 0)}%`;
      $("#host-rx").textContent = `↓ ${bytes(r.network_rx, true)}`;
      $("#host-tx").textContent = `↑ ${bytes(r.network_tx, true)}`;
      $("#host-app").textContent = bytes(r.app_memory);
    } catch {
      $("#host-cpu").textContent = "—";
      $("#host-mem").textContent = "不可用";
      for (const id of ["#host-rx", "#host-tx", "#host-app"]) $(id).textContent = "—";
      $("#host-metrics").title = "监控连接暂不可用";
    } finally { metricPending = false; }
  }
  let chosenAccount = "";
  let requestGeneration = 0;
  const sections = ["usage", "traffic", "regions", "stats"];
  function resetCards() {
    requestGeneration++;
    sections.forEach(k => $("#summary-" + k).textContent = "点击读取实时云数据");
  }
  window.syncDashboardAccounts = function () {
    for (const id of ["#dashboard-account", "#check-account", "#monitor-account"]) {
      const el = $(id), prev = el.value;
      el.innerHTML = state.accounts.map(a => `<option value="${a.id}">${esc(a.name)} · ${esc(a.region)}</option>`).join("") || '<option value="">先添加 OCI 账号</option>';
      if (state.accounts.some(a => String(a.id) === prev)) el.value = prev;
    }
    if (chosenAccount !== $("#dashboard-account").value) { chosenAccount = $("#dashboard-account").value; resetCards(); }
    $("#overview-profiles").innerHTML = state.accounts.length ? state.accounts.map(a => `<div class="profile-row"><span class="profile-avatar">${uiIcon("globe")}</span><div><b>${esc(a.name)}</b><small>${esc(a.region)}</small></div><span class="profile-status" data-profile-status="${a.id}">未检测</span><button class="ghost" data-profile-check="${a.id}">测活</button></div>`).join("") : '<div class="empty-state">还没有云账号。到「云账号」添加 OCI 配置，开始管理资源。</div>';
  };
  $("#dashboard-account").addEventListener("change", () => { chosenAccount = $("#dashboard-account").value; resetCards(); });

  async function readCard(kind) {
    const account = $("#dashboard-account").value;
    if (!account) { toast("请先添加 OCI 账号", false); return; }
    const generation = requestGeneration;
    const box = $("#summary-" + kind), button = $(`[data-cloud-metric="${kind}"]`);
    if (button.disabled) return;
    button.disabled = true;
    box.innerHTML = '<span class="loading-state">正在查询云端…</span>';
    try {
      const r = await api(`/api/accounts/${account}/${kind}`);
      if (generation !== requestGeneration) return;
      if (kind === "usage") {
        box.innerHTML = Object.entries(r.currencies || {}).map(([currency, amount]) => `<strong>${esc(amount.toFixed(2))}<small>${esc(currency)}</small></strong>`).join("") || '<strong>暂无费用记录</strong>';
        box.insertAdjacentHTML("beforeend", '<p>最近 30 个完整 UTC 日 · 云端账单可能延迟</p>');
      } else if (kind === "traffic") {
        box.innerHTML = `<strong>${r.total_gb.toFixed(2)}<small>GB</small></strong><p>${Object.keys(r.per_resource).length ? "已收到实例监控数据" : "未收到监控数据，不代表没有流量"}</p>`;
      } else if (kind === "regions") {
        box.innerHTML = `<strong>${r.data.length}<small>个区域</small></strong><div class="summary-details">${r.data.map(x => `<span class="chip">${esc(x.region)}${x.home ? " · 主区域" : ""} · ${esc(x.status)}</span>`).join("")}</div>`;
      } else {
        box.innerHTML = `<strong>${r.limits.length}<small>项配额</small></strong><div class="summary-details">${r.limits.map(x => `<p><b>${esc(x.name)}</b>${esc(x.used ?? "未知")} / ${esc(x.limit ?? "未知")}${x.ad ? " · " + esc(x.ad) : ""}</p>`).join("") || esc(r.cost_note || "没有匹配的配额")}</div>`;
      }
    } catch (e) { if (generation === requestGeneration) box.innerHTML = `<span class="query-error">${esc(e.message)}</span>`; }
    finally { button.disabled = false; }
  }
  $$('[data-cloud-metric]').forEach(b => b.addEventListener("click", () => readCard(b.dataset.cloudMetric)));
  $("#btn-refresh-overview").addEventListener("click", loadOverview);
  async function checkProfile(id) {
    const status = $(`[data-profile-status="${id}"]`);
    if (status) status.textContent = "检测中…";
    try {
      const r = await api(`/api/accounts/${id}/test`, {method:"POST"});
      if (status) { status.textContent = r.ok ? "连接正常" : r.error; status.classList.toggle("err", !r.ok); status.classList.toggle("success", r.ok); }
    } catch (e) { if (status) status.textContent = e.message; }
  }
  $("#overview-profiles").addEventListener("click", e => {
    const b = e.target.closest("[data-profile-check]"); if (b) checkProfile(b.dataset.profileCheck);
  });
  $("#btn-check-all").addEventListener("click", async e => {
    e.currentTarget.disabled = true;
    try { for (const a of state.accounts) await checkProfile(a.id); }
    finally { $("#btn-check-all").disabled = false; }
  });
  $("#btn-account-check").addEventListener("click", async () => {
    const id = $("#check-account").value; if (!id) { toast("请先添加 OCI 账号", false); return; }
    const box = $("#diagnostic-results"); box.textContent = "检查中…";
    $("#btn-account-check").disabled = true;
    try {
      const r = await api(`/api/accounts/${id}/check`, {method:"POST"});
      box.classList.remove("empty-state");
      box.innerHTML = `<div class="section-heading"><h3>${esc(r.account_name)}</h3><span class="chip">${esc(r.region)}</span></div>${r.checks.map(c => `<div class="check-row"><span class="check-indicator ${c.ok ? "success" : "err"}">${uiIcon(c.ok ? "check" : "close")}</span><div><b>${esc(c.name)}</b><p>${esc(c.message)}</p></div><span class="badge ${c.ok ? "b-ok" : "b-err"}">${c.ok ? "通过" : "失败"}</span></div>`).join("")}`;
    } catch (e) { box.textContent = e.message; }
    finally { $("#btn-account-check").disabled = false; }
  });
  $("#btn-cloud-monitor").addEventListener("click", async () => {
    const id = $("#monitor-account").value; if (!id) { toast("请先添加 OCI 账号", false); return; }
    const box = $("#cloud-monitor-results"); box.textContent = "读取中…";
    $("#btn-cloud-monitor").disabled = true;
    try {
      const [instances, traffic] = await Promise.all([api(`/api/cloud/oci/instances?account_id=${id}`), api(`/api/accounts/${id}/traffic`)]);
      box.classList.remove("empty-state");
      box.innerHTML = `<div class="section-heading"><h3>近 24 小时出站 ${traffic.total_gb.toFixed(2)} GB</h3><span class="muted">${new Date().toLocaleTimeString()} 更新</span></div><div class="table-wrap"><table><thead><tr><th>实例</th><th>状态</th><th>规格</th><th>出站流量</th></tr></thead><tbody>${instances.data.map(i => `<tr><td>${esc(i.name)}</td><td><span class="badge ${i.state === "RUNNING" ? "b-ok" : "b-mid"}">${esc(i.state)}</span></td><td>${esc(i.spec || i.shape)}</td><td>${Object.prototype.hasOwnProperty.call(traffic.per_resource, i.id) ? traffic.per_resource[i.id].toFixed(3) + " GB" : "无数据"}</td></tr>`).join("") || '<tr><td colspan="4">当前区域没有实例</td></tr>'}</tbody></table></div>`;
    } catch (e) { box.textContent = e.message; }
    finally { $("#btn-cloud-monitor").disabled = false; }
  });
  VIEW_LOADERS.diagnostics = () => {};
  VIEW_LOADERS.cloudmonitor = () => {};
  const icons = {overview:"home", instances:"server", diagnostics:"check", cloudmonitor:"activity", ssh:"terminal", network:"globe", domains:"globe", objects:"folder", volumes:"server", accounts:"globe"};
  $$('nav button[data-view]').forEach(b => { b.innerHTML = uiIcon(icons[b.dataset.view] || "server") + `<span>${esc(b.textContent)}</span>`; });
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $("#btn-theme").textContent = theme === "dark" ? "浅色模式" : "深色模式";
    try { localStorage.setItem("oci-theme", theme); } catch {}
  }
  let initialTheme = "light"; try { initialTheme = localStorage.getItem("oci-theme") || "light"; } catch {}
  setTheme(initialTheme);
  $("#btn-theme").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  setInterval(pollMetrics, 3000);
  window.addEventListener("focus", pollMetrics);
  window.addEventListener("DOMContentLoaded", pollMetrics);
})();
