/* SSH 终端视图：会话管理、xterm 终端、SFTP、批量命令、端口转发、资源监控 */
(function () {
  let terminals = {};   // sid -> {term, ws, fit}
  let activeSid = null;
  let sftpSid = null;
  let sftpPath = "/";
  let editingSessionId = null;
  const connectionStates = new Map();
  $("#session-search").addEventListener("input", renderSessionCards);

  async function loadSessions() {
    try {
      const r = await api("/api/ssh/sessions");
      state.sessions = r.data;
      renderSessionCards();
      syncSshSessionSelects();
    } catch (e) { /* 静默 */ }
  }
  window.loadSessions = loadSessions;

  window.syncSshSessionSelects = function () {
    const opts = state.sessions.map(s =>
      `<option value="${s.id}">${esc(s.name)}（${esc(s.host)}）</option>`).join("");
    for (const sel of ["#fw-session", "#mon-session"]) {
      const el = $(sel);
      if (el) el.innerHTML = opts || `<option value="">（先添加 SSH 会话）</option>`;
    }
    renderBatchChips();
  };

  function renderSessionCards() {
    const query = $("#session-search").value.trim().toLowerCase();
    const sessions = state.sessions.filter(s => `${s.name} ${s.host} ${s.username} ${s.tags}`.toLowerCase().includes(query));
    $("#session-count").textContent = state.sessions.length;
    $("#ssh-session-list").innerHTML = sessions.map(s => {
      const m = s.metadata || {};
      const status = connectionStates.get(s.id) || "未检测";
      const action = (attr, label, icon, danger = false) => `<button class="icon-action ${danger ? "danger" : "ghost"}" ${attr}="${s.id}" title="${label}" aria-label="${label}">${uiIcon(icon)}</button>`;
      return `<article class="session-card">
        <div class="session-title"><span class="session-avatar">${uiIcon("terminal")}</span><h3 title="${esc(s.name)}">${esc(s.name)}</h3><span class="auth-pill">${s.auth_type === "key" ? "密钥" : "密码"}</span></div>
        <p class="session-spec" title="${esc(m.shape || "")}">${esc(m.shape || "手动保存的 SSH 会话")}</p>
        <div class="session-badges">${m.spec ? `<span class="chip blue">${esc(m.spec)}</span>` : ""}${m.memory_gbs ? `<span class="chip amber">${esc(m.memory_gbs)} GB</span>` : ""}${(s.tags || "").split(",").filter(Boolean).map(tag => `<span class="chip">${esc(tag.trim())}</span>`).join("")}</div>
        <div class="session-address">${uiIcon("server")}<code>${esc(s.host)}:${s.port}</code><button class="ghost tiny" data-saddress="${s.id}" aria-label="复制地址" title="复制地址">${uiIcon("copy")}</button></div>
        <div class="session-user"><span class="user-dot"></span>${esc(s.username)}<span class="muted">${s.proxy_command ? "通过服务器代理连接" : "面板服务器直连"}</span></div>
        <div class="session-footer"><span class="connection-status ${status === "已连接" || status === "检测成功" ? "online" : ""}"><i></i>${esc(status)}</span><div>
          ${action("data-sopen", "打开终端", "play")}${action("data-scopy", "复制会话", "copy")}${action("data-sedit", "编辑会话", "edit")}${action("data-sdel", "删除会话", "trash", true)}
        </div></div>
        <details class="session-more"><summary>文件与连接工具</summary><div class="bar">${action("data-ssftp", "打开 SFTP", "folder")}${action("data-stest", "测试连接", "check")}<button class="ghost tiny" data-skey="${s.id}">重置信任</button></div></details>
      </article>`;
    }).join("") || `<div class="empty-state card">${query ? "没有匹配的会话，试试其他关键词。" : "还没有保存会话。添加一台服务器，或从 OCI 实例同步。"}</div>`;
  }

  window.sshViewLoaded = function () { renderSessionCards(); loadSessions(); };

  $("#ssh-session-list").addEventListener("click", async (e) => {
    const open = e.target.closest("[data-sopen]");
    const sftp = e.target.closest("[data-ssftp]");
    const edit = e.target.closest("[data-sedit]");
    const test = e.target.closest("[data-stest]");
    const del = e.target.closest("[data-sdel]");
    const key = e.target.closest("[data-skey]");
    const copy = e.target.closest("[data-scopy]");
    const address = e.target.closest("[data-saddress]");
    if (copy) {
      try { await api(`/api/ssh/sessions/${copy.dataset.scopy}/copy`, {method: "POST"}); await loadSessions(); toast("会话已复制，凭据保存在服务器端"); }
      catch (err) { toast(err.message, false); }
      return;
    }
    if (address) {
      const s = state.sessions.find(s => s.id === Number(address.dataset.saddress));
      try { await navigator.clipboard.writeText(`${s.host}:${s.port}`); toast("地址已复制"); }
      catch { toast("浏览器不允许复制，请手动选择地址", false); }
      return;
    }
    if (open) openTerminal(Number(open.dataset.sopen));
    else if (sftp) openSftp(Number(sftp.dataset.ssftp));
    else if (edit) fillSessionForm(Number(edit.dataset.sedit));
    else if (test) {
      toast("测试连接中…");
      try {
        const r = await api(`/api/ssh/sessions/${test.dataset.stest}/test`, { method: "POST" });
        connectionStates.set(Number(test.dataset.stest), "检测成功"); renderSessionCards();
        toast("✅ " + (r.output || "ok").slice(0, 120));
      } catch (err) { connectionStates.set(Number(test.dataset.stest), "检测失败"); renderSessionCards(); toast("❌ " + err.message, false); }
    } else if (key) {
      if (!confirm("确认已核实服务器身份并需要重新记录 SSH 主机公钥？下次连接会记录新的主机公钥。")) return;
      try { await api(`/api/ssh/sessions/${key.dataset.skey}/forget-host-key`, {method: "POST"}); toast("已清除记录，下次连接将重新验证并记录"); }
      catch (err) { toast(err.message, false); }
    } else if (del) {
      if (!confirm("删除该 SSH 会话？")) return;
      try { await api(`/api/ssh/sessions/${del.dataset.sdel}`, { method: "DELETE" }); closeTerminal(Number(del.dataset.sdel)); loadSessions(); }
      catch (err) { toast(err.message, false); }
    }
  });

  /* ---- 会话表单 ---- */
  $("#btn-ssh-new").addEventListener("click", () => fillSessionForm(null));

  function fillSessionForm(id) {
    editingSessionId = id;
    const s = state.sessions.find(x => x.id === id);
    $("#ssh-form-title").textContent = id ? `编辑会话：${s ? s.name : id}` : "添加 SSH 会话";
    $("#ss-name").value = s ? s.name : "";
    $("#ss-host").value = s ? s.host : "";
    $("#ss-port").value = s ? s.port : 22;
    $("#ss-user").value = s ? s.username : "root";
    $("#ss-authtype").value = s ? s.auth_type : "password";
    $("#ss-secret").value = "";
    $("#ss-proxy").value = s ? s.proxy_command : "";
    $("#ss-tags").value = s ? s.tags : "";
    $("#ss-mcpu").checked = !!(s && s.monitor_cpu);
    $("#ss-mmem").checked = !!(s && s.monitor_mem);
    $("#ss-mdisk").checked = !!(s && s.monitor_disk);
    $("#ssh-form").classList.remove("hide");
  }
  $("#btn-ssh-cancel").addEventListener("click", () => { $("#ssh-form").classList.add("hide"); editingSessionId = null; });
  $("#btn-ssh-save").addEventListener("click", async () => {
    const body = {
      name: $("#ss-name").value.trim(), host: $("#ss-host").value.trim(),
      port: Number($("#ss-port").value) || 22, username: $("#ss-user").value.trim() || "root",
      auth_type: $("#ss-authtype").value, secret: $("#ss-secret").value,
      proxy_command: $("#ss-proxy").value.trim(), tags: $("#ss-tags").value.trim(),
      monitor_cpu: $("#ss-mcpu").checked, monitor_mem: $("#ss-mmem").checked,
      monitor_disk: $("#ss-mdisk").checked,
    };
    if (!body.name || !body.host) { toast("名称和主机必填", false); return; }
    try {
      if (editingSessionId) await api(`/api/ssh/sessions/${editingSessionId}`, { method: "PUT", body });
      else await api("/api/ssh/sessions", { method: "POST", body });
      toast("已保存");
      $("#ssh-form").classList.add("hide");
      editingSessionId = null;
      loadSessions();
    } catch (e) { toast(e.message, false); }
  });

  $("#btn-ssh-sync").addEventListener("click", async () => {
    const acct = $("#dashboard-account").value || state.instAccount;
    if (!acct) { toast("请先在云账号页面添加账号", false); return; }
    const name = state.accounts.find(a => String(a.id) === String(acct))?.name;
    if (!confirm(`同步账号「${name || acct}」的实例？已有会话会更新规格，新会话需要填写 SSH 凭据。`)) return;
    try {
      const r = await api("/api/ssh/sync-cloud", { method: "POST", body: { account_id: Number(acct), username: "root" } });
      toast(`发现 ${r.found} 台实例，新建 ${r.created} 个会话`);
      loadSessions();
    } catch (e) { toast(e.message, false); }
  });

  /* ---- xterm 终端 ---- */

  function openTerminal(sid) {
    const sess = state.sessions.find(x => x.id === sid);
    if (!sess) return;
    $("#term-area").classList.remove("hide");
    if (terminals[sid]) {
      if (terminals[sid].ws.readyState <= 1) { activateTab(sid); return; }
      closeTerminal(sid);
      $("#term-area").classList.remove("hide");
    }

    const tabBtn = document.createElement("button");
    tabBtn.textContent = sess.name;
    tabBtn.dataset.tsid = sid;
    tabBtn.addEventListener("click", () => activateTab(sid));
    $("#term-tabs").appendChild(tabBtn);
    const closeBtn = document.createElement("button");
    closeBtn.className = "ghost term-close";
    closeBtn.innerHTML = uiIcon("close");
    closeBtn.title = `关闭终端 ${sess.name}`;
    closeBtn.setAttribute("aria-label", closeBtn.title);
    closeBtn.addEventListener("click", () => closeTerminal(sid));
    $("#term-tabs").appendChild(closeBtn);

    const holder = document.createElement("div");
    holder.className = "term-holder hide";
    holder.id = "term-" + sid;
    holder.style.height = "480px";
    $("#term-stack").appendChild(holder);

    const term = new Terminal({ cursorBlink: true, fontSize: 13, theme: { background: "#0e1220" } });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(holder);
    fit.fit();

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/ssh?sid=${sid}&cols=${term.cols}&rows=${term.rows}`);
    ws.onopen = () => {
      fit.fit(); term.focus();
      ws.send(JSON.stringify({resize: {cols: term.cols, rows: term.rows}}));
      connectionStates.set(sid, "已连接"); renderSessionCards();
    };
    ws.onmessage = ev => term.write(ev.data);
    ws.onclose = ev => {
      connectionStates.set(sid, "已断开"); renderSessionCards();
      if (!terminals[sid] || terminals[sid].ws !== ws) return;
      term.write(ev.code === 4401 ? "\r\n\r\n[未登录]" : "\r\n\r\n[连接已断开，点击 × 关闭，或点击终端重新连接]");
    };
    term.onData(data => { if (ws.readyState === 1) ws.send(data); });
    term.onResize(({ cols, rows }) => {
      if (ws.readyState === 1) ws.send(JSON.stringify({ resize: { cols, rows } }));
    });
    holder.addEventListener("paste", async (ev) => {
      const item = [...(ev.clipboardData?.items || [])].find(i => i.type.startsWith("image/"));
      if (!item) return;
      const blob = item.getAsFile();
      const buf = new Uint8Array(await blob.arrayBuffer());
      let binary = "";
      for (let i = 0; i < buf.length; i++) binary += String.fromCharCode(buf[i]);
      const b64 = btoa(binary);
      const name = `/tmp/oci-panel-paste-${Date.now()}.png`;
      try {
        await api("/api/ssh/sftp/write", { method: "POST", body: {
          session_id: sid, path: name, content_base64: b64 } });
        term.write(`\r\n[贴图已上传] ${name}\r\n`);
      } catch (e) { term.write(`\r\n[贴图上传失败] ${e.message}\r\n`); }
    });
    const resize = () => { if (sid === activeSid) fit.fit(); };
    window.addEventListener("resize", resize);

    terminals[sid] = { term, ws, fit, tabBtn, holder, closeBtn, resize };
    activateTab(sid);
  }

  function activateTab(sid) {
    activeSid = sid;
    for (const [k, t] of Object.entries(terminals)) {
      t.holder.classList.toggle("hide", String(k) !== String(sid));
      t.tabBtn.classList.toggle("active", String(k) === String(sid));
    }
    const t = terminals[sid];
    if (t) { t.fit.fit(); t.term.focus(); }
  }

  function closeTerminal(sid) {
    const t = terminals[sid];
    if (!t) return;
    t.ws.onclose = null;
    try { t.ws.close(); t.term.dispose(); } catch {}
    window.removeEventListener("resize", t.resize);
    t.holder.remove();
    t.tabBtn.remove();
    t.closeBtn.remove();
    delete terminals[sid];
    connectionStates.set(sid, "已断开");
    if (activeSid === sid) {
      const next = Object.keys(terminals)[0];
      activeSid = next ? Number(next) : null;
      if (next) activateTab(Number(next));
    }
    if (!Object.keys(terminals).length) $("#term-area").classList.add("hide");
    renderSessionCards();
  }

  /* ---- SFTP ---- */
  function openSftp(sid) {
    sftpSid = sid;
    const sess = state.sessions.find(x => x.id === sid);
    $("#sftp-sess").textContent = sess ? `${sess.name} (${sess.host})` : sid;
    $("#sftp-panel").classList.remove("hide");
    sftpPath = "/";
    sftpList();
  }

  async function sftpList() {
    try {
      const r = await api(`/api/ssh/sftp/list?session_id=${sftpSid}&path=${encodeURIComponent(sftpPath)}`);
      $("#sftp-path").textContent = sftpPath;
      $("#sftp-table tbody").innerHTML = r.data.map(f => `<tr>
        <td>${f.dir ? "📁" : "📄"} <a href="javascript:void(0)" data-fopen="${esc(f.name)}" data-fdir="${f.dir}">${esc(f.name)}</a></td>
        <td>${f.dir ? "-" : (f.size / 1024).toFixed(1) + "KB"}</td>
        <td>${f.mtime ? new Date(f.mtime * 1000).toLocaleString() : "-"}</td>
        <td class="ops">${f.dir ? "" : `<button data-fedit="${esc(f.name)}">编辑</button>`}
          <button data-frename="${esc(f.name)}">改名</button>
          <button data-fdel="${esc(f.name)}" data-fisdir="${f.dir}" class="danger">删除</button></td></tr>`).join("")
        || `<tr><td colspan="4" class="muted">空目录</td></tr>`;
    } catch (e) { toast(e.message, false); }
  }

  $("#sftp-table tbody").addEventListener("click", async (e) => {
    const link = e.target.closest("[data-fopen]");
    const edit = e.target.closest("[data-fedit]");
    const rename = e.target.closest("[data-frename]");
    const del = e.target.closest("[data-fdel]");
    try {
      if (link) {
        if (link.dataset.fdir === "true") {
          sftpPath = sftpPath.replace(/\/$/, "") + "/" + link.dataset.fopen;
          sftpList();
        } else downloadFile(link.dataset.fopen);
      } else if (edit) {
        const full = sftpPath.replace(/\/$/, "") + "/" + edit.dataset.fedit;
        const r = await api(`/api/ssh/sftp/read?session_id=${sftpSid}&path=${encodeURIComponent(full)}`);
        $("#sftp-edit").textContent = r.content;
        $("#sftp-edit").classList.remove("hide");
        $("#sftp-editbar").classList.remove("hide");
        $("#sftp-edit").dataset.path = full;
      } else if (rename) {
        const nn = prompt("新名称：", rename.dataset.frename);
        if (!nn) return;
        await api("/api/ssh/sftp/rename", { method: "POST", body: {
          session_id: sftpSid, path: sftpPath.replace(/\/$/, "") + "/" + rename.dataset.frename,
          new_path: sftpPath.replace(/\/$/, "") + "/" + nn }});
        sftpList();
      } else if (del) {
        if (!confirm(`删除 ${del.dataset.frename || del.dataset.fdel}？`)) return;
        await api("/api/ssh/sftp/delete", { method: "POST", body: {
          session_id: sftpSid, path: sftpPath.replace(/\/$/, "") + "/" + del.dataset.fdel,
          is_dir: del.dataset.fisdir === "true" }});
        sftpList();
      }
    } catch (err) { toast(err.message, false); }
  });

  async function downloadFile(name) {
    try {
      const full = sftpPath.replace(/\/$/, "") + "/" + name;
      const r = await fetch(`/api/ssh/sftp/download?session_id=${sftpSid}&path=${encodeURIComponent(full)}`);
      if (!r.ok) { const data = await r.json(); throw new Error(data.detail || "下载失败"); }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) { toast(e.message, false); }
  }

  $("#btn-sftp-mkdir").addEventListener("click", async () => {
    const n = $("#sftp-newname").value.trim();
    if (!n) return;
    try {
      await api("/api/ssh/sftp/mkdir", { method: "POST", body: { session_id: sftpSid, path: sftpPath.replace(/\/$/, "") + "/" + n } });
      $("#sftp-newname").value = "";
      sftpList();
    } catch (e) { toast(e.message, false); }
  });
  $("#btn-sftp-save").addEventListener("click", async () => {
    try {
      await api("/api/ssh/sftp/write", { method: "POST", body: {
        session_id: sftpSid, path: $("#sftp-edit").dataset.path, content: $("#sftp-edit").textContent }});
      toast("已保存到远程");
    } catch (e) { toast(e.message, false); }
  });

  /* ---- 批量命令 ---- */
  const batchSel = new Set();
  function renderBatchChips() {
    const el = $("#batch-sessions");
    if (!el) return;
    el.innerHTML = state.sessions.map(s =>
      `<span class="chip selchip ${batchSel.has(s.id) ? "on" : ""}" data-bsid="${s.id}">${esc(s.name)}</span>`).join("")
      || `<span class="muted">没有会话</span>`;
  }
  $("#batch-sessions").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-bsid]");
    if (!chip) return;
    const sid = Number(chip.dataset.bsid);
    batchSel.has(sid) ? batchSel.delete(sid) : batchSel.add(sid);
    chip.classList.toggle("on");
  });
  $("#btn-ssh-batch").addEventListener("click", () => { renderBatchChips(); $("#ssh-batch").classList.toggle("hide"); });
  $("#btn-batch-run").addEventListener("click", async () => {
    if (!batchSel.size || !$("#batch-cmd").value.trim()) { toast("选择会话并输入命令", false); return; }
    $("#batch-out").textContent = "执行中…";
    try {
      const r = await api("/api/ssh/exec", { method: "POST", body: { ids: [...batchSel], cmd: $("#batch-cmd").value } });
      $("#batch-out").textContent = r.results.map(x =>
        `===== ${x.name} =====\n${x.error ? "错误: " + x.error : (x.output || "(无输出)")}`).join("\n\n");
    } catch (e) { $("#batch-out").textContent = "失败: " + e.message; }
  });

  /* ---- 端口转发 ---- */
  $("#btn-ssh-fwd").addEventListener("click", async () => {
    $("#ssh-fwd-panel").classList.toggle("hide");
    if (!$("#ssh-fwd-panel").classList.contains("hide")) loadForwards();
  });
  async function loadForwards() {
    try {
      const r = await api("/api/ssh/forwards");
      $("#fw-table tbody").innerHTML = r.data.map(f => `<tr>
        <td>${esc(f.session)}</td><td>${f.type === "local" ? "本地" : "远程"}</td>
        <td>${f.local_port}</td><td>${esc(f.remote_host)}:${f.remote_port}</td>
        <td class="ops"><button data-fstop="${f.id}" class="danger">停止</button></td></tr>`).join("")
        || `<tr><td colspan="5" class="muted">没有进行中的转发</td></tr>`;
    } catch (e) { toast(e.message, false); }
  }
  $("#btn-fw-add").addEventListener("click", async () => {
    try {
      await api("/api/ssh/forwards", { method: "POST", body: {
        session_id: Number($("#fw-session").value), type: $("#fw-type").value,
        local_port: Number($("#fw-lport").value), remote_host: $("#fw-rhost").value.trim() || "127.0.0.1",
        remote_port: Number($("#fw-rport").value) }});
      toast("转发已开启"); loadForwards();
    } catch (e) { toast(e.message, false); }
  });
  $("#fw-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-fstop]");
    if (!btn) return;
    try { await api(`/api/ssh/forwards/${btn.dataset.fstop}/stop`, { method: "POST" }); toast("已停止"); loadForwards(); }
    catch (err) { toast(err.message, false); }
  });

  /* ---- 资源监控 ---- */
  $("#btn-ssh-monitor").addEventListener("click", () => { $("#ssh-monitor-panel").classList.toggle("hide"); });
  $("#btn-mon-load").addEventListener("click", async () => {
    const sid = Number($("#mon-session").value);
    if (!sid) return;
    $("#mon-bars").innerHTML = `<span class="muted">采集中…</span>`;
    try {
      const r = await api("/api/ssh/monitor", { method: "POST", body: { session_id: sid } });
      const bar = (label, v) => v == null ? "" :
        `<div class="mon"><span>${label}</span><div class="meter"><i style="width:${Math.min(v, 100)}%;background:${v > 90 ? "var(--err)" : v > 70 ? "var(--warn)" : "var(--ok)"}"></i></div><b>${v}%</b></div>`;
      $("#mon-bars").innerHTML =
        bar("CPU", r.cpu) + bar("内存", r.mem) + bar("磁盘/", r.disk) +
        `<div class="mon"><span>网卡累计</span><b>↓${r.net_rx_mb}MB ↑${r.net_tx_mb}MB</b></div>`;
    } catch (e) { $("#mon-bars").innerHTML = `<span class="err-cell">${esc(e.message)}</span>`; }
  });
})();
