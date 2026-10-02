/* 云资源视图：实例、开机抢机、硬盘、网络/DNS、用户管理、对象存储 */
(function () {
  const instAccount = () => Number($("#inst-account").value || 0);
  const acctById = id => state.accounts.find(a => a.id === Number(id));

  /* ================= 实例 ================= */

  window.loadInstances = async function (silent = false) {
    if (!state.instAccount) return;
    if (!silent) $("#inst-summary").textContent = "加载中…";
    try {
      const acct = acctById(instAccount());
      if (!acct) return;
      const platform = acct.platform;
      const url = platform === "oci"
        ? `/api/cloud/oci/instances?account_id=${acct.id}`
        : `/api/cloud/${platform}/instances?account_id=${acct.id}`;
      const r = await api(url);
      renderInstances(platform, acct, r.data);
      if (!silent) $("#inst-summary").textContent = "";
    } catch (e) {
      if (!silent) { $("#inst-summary").textContent = ""; toast(e.message, false); }
    }
  };

  function stateBadge(s) {
    const map = { RUNNING: ["RUNNING", "b-ok"], STOPPED: ["STOPPED", "b-mid"],
      STARTING: ["STARTING", "b-mid"], STOPPING: ["STOPPING", "b-mid"],
      PROVISIONING: ["创建中", "b-warn"], PENDING: ["PENDING", "b-warn"],
      STOPPED_BY_USER: ["已停止", "b-mid"], ACTIVE: ["ACTIVE", "b-ok"] };
    const m = map[s] || [s, "b-err"];
    return `<span class="badge ${m[1]}">${esc(m[0])}</span>`;
  }

  function renderInstances(platform, acct, list) {
    const isOci = platform === "oci";
    $("#inst-table tbody").innerHTML = list.map(i => {
      const d = esc;
      const hasErr = i.error !== undefined;
      if (hasErr) {
        return `<tr><td>${esc(PLATFORM_LABEL[platform] || platform)}</td><td colspan="7" class="err-cell" style="max-width:none">账号 ${esc(i.account_name)}: ${esc(i.error)}</td></tr>`;
      }
      const kind = i.kind || "";
      let ops = `
        <button data-act="start" data-id="${d(i.id)}" data-kind="${kind}" ${i.state === "RUNNING" ? "disabled" : ""}>启动</button>
        <button data-act="STOP" data-id="${d(i.id)}" data-kind="${kind}" ${i.state !== "RUNNING" ? "disabled" : ""}>关机</button>
        <button data-act="REBOOT" data-id="${d(i.id)}" data-kind="${kind}" ${i.state !== "RUNNING" ? "disabled" : ""}>重启</button>
        <button data-act="ip" data-id="${d(i.id)}" ${i.public_ip || i.state === "RUNNING" ? "" : "disabled"}>换IP</button>
        <button data-act="TERMINATE" class="danger" data-id="${d(i.id)}" data-name="${d(i.name)}" data-kind="${kind}">终止</button>`;
      if (isOci) {
        ops += `
        <button data-act="resize" data-id="${d(i.id)}" data-name="${d(i.name)}">升降配</button>
        <button data-act="reinstall" data-id="${d(i.id)}" data-name="${d(i.name)}">重建</button>
        <button data-act="ipv6" data-id="${d(i.id)}">IPv6</button>
        <button data-act="console" data-id="${d(i.id)}" data-name="${d(i.name)}">串口日志</button>`;
      }
      return `<tr>
        <td><span class="chip">${esc(PLATFORM_LABEL[platform] || platform)}</span></td>
        <td>${d(i.name)}</td><td>${stateBadge(i.state)}</td>
        <td>${d(i.spec || i.shape || "")}</td>
        <td>${d(i.public_ip)}</td><td>${d(i.private_ip)}</td><td style="font-size:12px">${d(i.ad)}</td>
        <td class="ops">${ops}</td></tr>`;
    }).join("") || `<tr><td colspan="8" class="muted">该账号下没有实例</td></tr>`;
    $("#inst-summary").textContent = list.length ? `共 ${list.length} 台` : "";

  }

  async function instAction(action, id, name, kind) {
    const acct = acctById(instAccount());
    const body = { account_id: acct.id, instance_id: id, action, kind, rg: acct.params?.resource_group || "", zone: "" };
    await api(`/api/cloud/${acct.platform}/action`, { method: "POST", body });
    toast("指令已提交：" + action);
    loadInstances(true);
  }

  $("#inst-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn || btn.disabled) return;
    const { act, id, name, kind } = btn.dataset;
    const acct = acctById(instAccount());
    const isOci = acct.platform === "oci";
    try {
      if (act === "start") { if (!confirm(`确认启动 ${name || id}？`)) return; await instAction("START", id, name, kind); }
      else if (act === "STOP") { if (!confirm(`确认关机 ${name || id}？`)) return; await instAction("STOP", id, name, kind); }
      else if (act === "REBOOT") { if (!confirm(`确认重启 ${name || id}？`)) return; await instAction("SOFTRESET", id, name, kind); }
      else if (act === "TERMINATE") {
        const input = prompt(`高危操作！将终止并释放实例 ${name || id}，数据不可恢复！\n如确认请输入实例名称：`);
        if (input === null) return;
        if (input.trim() !== (name || id)) { toast("名称不一致，已取消", false); return; }
        await instAction("TERMINATE", id, name, kind);
      } else if (act === "ip") {
        if (!confirm("换公网IP？临时IP会解绑后分配新的。")) return;
        const r = await api(`/api/cloud/${acct.platform}/change-ip`, { method: "POST", body: {
          account_id: acct.id, instance_id: id, rg: acct.params?.resource_group || "" } });
        toast(`换IP完成：${r.old_ip || "无"} → ${r.new_ip || "分配中"}`);
        loadInstances(true);
      } else if (act === "resize") {
        const inp = prompt("升降配（仅 A1 Flex）\n输入 OCPU,内存（如 2,12 或 4,24）：", "2,12");
        if (!inp) return;
        const [o, m] = inp.split(",").map(x => Number(x.trim()));
        if (!o || !m) { toast("格式不对", false); return; }
        await api("/api/oci/resize", { method: "POST", body: { account_id: acct.id, instance_id: id, ocpus: o, memory_gbs: m } });
        toast("升降配已提交"); loadInstances(true);
      } else if (act === "reinstall") {
        if (!confirm(`按原镜像重建 ${name}？将新建实例并保留旧实例及数据，可能需要额外配额。确认新实例可用后再手动释放旧资源。`)) return;
        const r = await api("/api/oci/reinstall", { method: "POST", body: { account_id: acct.id, instance_id: id } });
        toast(r.message);
        loadInstances(true);
      } else if (act === "ipv6") {
        const r = await api("/api/oci/ipv6", { method: "POST", body: { account_id: acct.id, instance_id: id } });
        toast("IPv6 已附加: " + (r.ipv6 || "(刷新查看)")); loadInstances(true);
      } else if (act === "console") {
        toast("抓取串口日志中（最多45秒）…");
        const r = await api("/api/oci/console", { method: "POST", body: { account_id: acct.id, instance_id: id } });
        showLogModal(`${name} 串口日志`, r.content || "(空)");
      }
    } catch (err) { toast(err.message, false); }
  });

  function showLogModal(title, text) {
    const w = window.open("", "_blank");
    if (!w) { toast("弹窗被拦截，日志：" + (text || "").slice(0, 200), false); return; }
    w.document.write(`<pre style="white-space:pre-wrap;background:#111;color:#ddd;padding:16px;font-size:13px"><b>${esc(title)}</b>\n\n${esc(text)}</pre>`);
  }
  window.showLogModal = showLogModal;

  $("#btn-refresh-inst").addEventListener("click", () => loadInstances());
  $("#inst-account").addEventListener("change", e => { state.instAccount = e.target.value; loadInstances(); });

  /* ================= 开机抢机 ================= */

  $("#l-preset").addEventListener("change", () => {
    const v = $("#l-preset").value;
    if (v === "a1x2") { $("#l-shape").value = "VM.Standard.A1.Flex"; $("#l-ocpus").value = 2; $("#l-mem").value = 12; }
    if (v === "a1x1") { $("#l-shape").value = "VM.Standard.A1.Flex"; $("#l-ocpus").value = 1; $("#l-mem").value = 6; }
    if (v === "a1x4") { $("#l-shape").value = "VM.Standard.A1.Flex"; $("#l-ocpus").value = 4; $("#l-mem").value = 24; }
    if (v === "amd") { $("#l-shape").value = "VM.Standard.E2.1.Micro"; $("#l-ocpus").value = 1; $("#l-mem").value = 1; }
  });
  $("#l-os-sel").addEventListener("change", () => {
    const [os, ver] = $("#l-os-sel").value.split("|");
    $("#l-os-name").value = os; $("#l-os-version").value = ver;
  });

  window.fillLaunchMeta = async function () {
    const acct = acctById($("#l-account").value);
    if (!acct || acct.platform !== "oci") return;
    try {
      const r = await api(`/api/oc-info?account_id=${acct.id}`);
      $("#l-ad").innerHTML = `<option value="">自动轮换</option>` +
        r.ads.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join("");
      $("#l-subnet").innerHTML = `<option value="">自动选择（优先公网子网）</option>` +
        r.subnets.map(s => `<option value="${s.id}">${esc(s.name)} ${s.public ? "（公网）" : "（私网）"}</option>`).join("");
    } catch (e) { toast("加载开机配置失败：" + e.message, false); }
  };
  $("#l-account").addEventListener("change", fillLaunchMeta);

  $("#btn-create-network").addEventListener("click", async () => {
    const aid = Number($("#l-account").value);
    if (!aid) { toast("请先添加并选择 OCI 账号", false); return; }
    if (!confirm("创建面板专用 VCN、公网子网和互联网网关，并开放 SSH 22 端口？已有的其他网络不会修改。")) return;
    const button = $("#btn-create-network");
    button.disabled = true;
    try {
      const r = await api("/api/oci/network/create", {method: "POST", body: {account_id: aid}});
      await fillLaunchMeta();
      $("#l-subnet").value = r.subnet_id;
      toast(r.reused ? "已选择面板公网子网" : "公网网络已创建");
    } catch (e) { toast(e.message, false); }
    finally { button.disabled = false; }
  });

  $("#launch-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/launch-tasks", { method: "POST", body: {
        account_id: Number($("#l-account").value),
        display_name: $("#l-name").value.trim() || "auto-boot",
        shape: $("#l-shape").value.trim(),
        ocpus: Number($("#l-ocpus").value) || 0,
        memory_gbs: Number($("#l-mem").value) || 0,
        os_name: $("#l-os-name").value.trim(), os_version: $("#l-os-version").value.trim(),
        boot_gb: Number($("#l-boot").value) || 50,
        ssh_key: $("#l-ssh").value.trim(), subnet_id: $("#l-subnet").value,
        boot_volume_id: $("#l-bootvol").value.trim(), ad_name: $("#l-ad").value,
      }});
      toast("任务已创建，每 60 秒自动尝试"); loadTasks();
    } catch (err) { toast(err.message, false); }
  });

  window.loadTasks = async function (silent = false) {
    try {
      const r = await api("/api/launch-tasks");
      const stMap = { running: ["运行中", "b-run"], success: ["成功", "b-ok"],
        failed: ["失败", "b-err"], stopped: ["已停止", "b-mid"] };
      $("#task-table tbody").innerHTML = r.data.map(t => {
        const [st, cls] = stMap[t.status] || [t.status, ""];
        const cfg = t.shape.includes("Flex") ? `${t.ocpus}C/${t.memory_gbs}G` : "微型";
        return `<tr><td>${esc(t.display_name)}</td><td>${esc(t.account_name || "（已删）")}</td>
          <td>${esc(t.shape)}<br><span class="muted" style="font-size:12px">${cfg} · ${esc(t.os_name)} ${esc(t.os_version)}</span></td>
          <td><span class="badge ${cls}">${st}</span></td><td>${t.attempts}</td>
          <td class="err-cell" title="${esc(t.last_error)}">${esc((t.last_error || "").slice(0, 60)) || "-"}</td>
          <td class="ops">${t.status === "running"
            ? `<button data-op="stop" data-id="${t.id}">暂停</button>`
            : t.status === "success" ? "" : `<button data-op="start" data-id="${t.id}">继续</button>`}
          <button data-op="delete" class="danger" data-id="${t.id}">删除</button></td></tr>`;
      }).join("") || `<tr><td colspan="7" class="muted">还没有任务</td></tr>`;
    } catch (e) { if (!silent) toast(e.message, false); }
  };
  $("#task-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-op]");
    if (!btn) return;
    try { await api(`/api/launch-tasks/${btn.dataset.id}/${btn.dataset.op}`, { method: "POST" }); loadTasks(true); }
    catch (err) { toast(err.message, false); }
  });

  $("#btn-a1").addEventListener("click", async () => {
    try {
      const r = await api(`/api/oci/a1-checkup?account_id=${$("#a1-account").value}`);
      $("#a1-result").textContent =
        `A1 总量 ${r.total_ocpus}C/${r.total_mem}G（当前区域参考上限 ${r.limit_ocpus}C/${r.limit_mem}G）` +
        (r.over_ocpus || r.over_mem ? " ⚠️ 超额！" : " ✅ 未超额");
      $("#a1-table tbody").innerHTML = r.instances.map(i => `<tr>
        <td>${esc(i.name)}</td><td>${i.ocpus}C/${i.memory_gbs}G</td>
        <td class="ops"><button data-a1id="${i.id}" data-o="${i.ocpus}" data-m="${i.memory_gbs}">降配</button></td></tr>`).join("")
        || `<tr><td colspan="3" class="muted">没有 A1 实例</td></tr>`;
    } catch (e) { toast(e.message, false); }
  });
  $("#a1-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-a1id]");
    if (!btn) return;
    const val = prompt("降配到 OCPU,内存（如 1,6）：", "1,6");
    if (!val) return;
    const [o, m] = val.split(",").map(x => Number(x.trim()));
    try {
      await api("/api/oci/a1-downsize", { method: "POST", body: {
        account_id: Number($("#a1-account").value), instance_id: btn.dataset.a1id, ocpus: o, memory_gbs: m } });
      toast("降配已提交"); $("#btn-a1").click();
    } catch (err) { toast(err.message, false); }
  });

  /* ================= 硬盘 ================= */

  window.loadVolumes = async function (silent = false) {
    const aid = $("#vol-account").value;
    if (!aid) return;
    try {
      const [b, k] = await Promise.all([
        api(`/api/oci/boot-volumes?account_id=${aid}`),
        api(`/api/oci/block-volumes?account_id=${aid}`),
      ]);
      const gb = v => v == null ? "-" : v;
      $("#vol-table tbody").innerHTML = b.data.map(v => `<tr>
        <td>${esc(v.name)}</td><td>${gb(v.size_gbs)}</td><td>${gb(v.size_used_gbs)}</td><td>${gb(v.vpus)}</td>
        <td>${esc(v.instance || "-")}</td><td>${esc(v.state)}</td>
        <td class="ops"><button data-vact="resize" data-id="${v.id}" data-size="${v.size_gbs}">扩容</button>
          <button data-vact="perf" data-id="${v.id}" data-vpus="${v.vpus}">改性能</button></td></tr>`).join("")
        || `<tr><td colspan="7" class="muted">没有引导卷</td></tr>`;
      $("#bvol-table tbody").innerHTML = k.data.map(v => `<tr>
        <td>${esc(v.name)}</td><td>${gb(v.size_gbs)}</td><td>${gb(v.vpus)}</td>
        <td>${esc(v.instance || "未挂载")}</td><td>${esc(v.state)}</td>
        <td class="ops"><button data-bact="resize" data-id="${v.id}" data-size="${v.size_gbs}">扩容</button>
          ${v.instance ? `<button data-bact="detach" data-id="${v.id}">卸载</button>`
            : `<button data-bact="attach" data-id="${v.id}">挂载</button>`}
          <button data-bact="delete" class="danger" data-id="${v.id}">删除</button></td></tr>`).join("")
        || `<tr><td colspan="6" class="muted">没有块卷</td></tr>`;
    } catch (e) { if (!silent) toast(e.message, false); }
  };
  $("#vol-account").addEventListener("change", () => loadVolumes());
  $("#btn-refresh-vol").addEventListener("click", () => loadVolumes());

  $("#btn-newvol").addEventListener("click", async () => {
    $("#newvol-form").classList.toggle("hide");
    const acct = acctById($("#vol-account").value);
    if (!acct) return;
    try {
      const r = await api(`/api/oc-info?account_id=${acct.id}`);
      if (r.ads[0]) $("#nv-ad").value = r.ads[0];
    } catch {}
  });
  $("#btn-newvol-go").addEventListener("click", async () => {
    try {
      await api("/api/oci/block-volumes", { method: "POST", body: {
        account_id: Number($("#vol-account").value), ad: $("#nv-ad").value.trim(),
        name: $("#nv-name").value.trim(), size_gbs: Number($("#nv-size").value) || 50,
        vpus: Number($("#nv-vpu").value) || 10 }});
      toast("块卷创建中"); $("#newvol-form").classList.add("hide"); loadVolumes(true);
    } catch (e) { toast(e.message, false); }
  });

  $("#vol-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-vact]");
    if (!btn) return;
    const aid = Number($("#vol-account").value);
    try {
      if (btn.dataset.vact === "resize") {
        const n = prompt("扩容到多少 GB？（只能增大）", btn.dataset.size);
        if (!n) return;
        await api(`/api/oci/boot-volumes/${btn.dataset.id}/update`, { method: "POST", body: { account_id: aid, size_gbs: Number(n) } });
      } else {
        const n = prompt("性能 VPU/GB：10=平衡 20=更高 30~120=极高", btn.dataset.vpus);
        if (!n) return;
        await api(`/api/oci/boot-volumes/${btn.dataset.id}/update`, { method: "POST", body: { account_id: aid, vpus: Number(n) } });
      }
      toast("已提交"); setTimeout(() => loadVolumes(true), 1500);
    } catch (err) { toast(err.message, false); }
  });

  $("#bvol-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-bact]");
    if (!btn) return;
    const aid = Number($("#vol-account").value);
    try {
      if (btn.dataset.bact === "resize") {
        const n = prompt("扩容到多少 GB？", btn.dataset.size);
        if (!n) return;
        await api(`/api/oci/block-volumes/${btn.dataset.id}/update`, { method: "POST", body: { account_id: aid, size_gbs: Number(n) } });
      } else if (btn.dataset.bact === "attach") {
        const iid = prompt("挂载到实例 OCID：");
        if (!iid) return;
        await api(`/api/oci/block-volumes/${btn.dataset.id}/attach`, { method: "POST", body: { account_id: aid, instance_id: iid } });
      } else if (btn.dataset.bact === "detach") {
        await api(`/api/oci/block-volumes/${btn.dataset.id}/detach`, { method: "POST", body: { account_id: aid } });
      } else {
        if (!confirm("删除该块卷？")) return;
        await api(`/api/oci/block-volumes/${btn.dataset.id}?account_id=${aid}`, { method: "DELETE" });
      }
      toast("已提交"); setTimeout(() => loadVolumes(true), 1500);
    } catch (err) { toast(err.message, false); }
  });

  /* ================= 网络 / DNS ================= */

  window.loadNetwork = async function () {
    await fillNetInstances();
    loadReserved();
    loadCfZones();
  };

  async function fillNetInstances() {
    const aid = $("#net-account").value;
    if (!aid) return;
    try {
      const r = await api(`/api/cloud/oci/instances?account_id=${aid}`);
      $("#net-instance").innerHTML = r.data.map(i =>
        `<option value="${i.id}">${esc(i.name)}（${esc(i.public_ip || i.state)}）</option>`).join("")
        || `<option value="">（没有实例）</option>`;
    } catch (e) { $("#net-instance").innerHTML = `<option value="">加载失败</option>`; }
  }
  $("#net-account").addEventListener("change", fillNetInstances);

  $("#btn-net-changeip").addEventListener("click", async () => {
    try {
      const r = await api("/api/cloud/oci/change-ip", { method: "POST", body: {
        account_id: Number($("#net-account").value), instance_id: $("#net-instance").value }});
      $("#net-result").textContent = `换IP：${r.old_ip || "无"} → ${r.new_ip || "分配中"}`;
    } catch (e) { toast(e.message, false); }
  });
  $("#btn-net-ipv6").addEventListener("click", async () => {
    try {
      const r = await api("/api/oci/ipv6", { method: "POST", body: {
        account_id: Number($("#net-account").value), instance_id: $("#net-instance").value }});
      $("#net-result").textContent = "IPv6: " + (r.ipv6 || "已附加");
    } catch (e) { toast(e.message, false); }
  });

  async function loadReserved() {
    const aid = $("#net-account").value;
    if (!aid) return;
    try {
      const r = await api(`/api/oci/reserved-ips?account_id=${aid}`);
      $("#rip-table tbody").innerHTML = r.data.map(p => `<tr>
        <td>${esc(p.ip)}</td><td>${esc(p.name)}</td><td>${p.assigned ? "✅" : "—"}</td>
        <td class="ops">${p.assigned ? "" : `<button data-ripa="${p.id}">绑定到实例</button>`}
          <button data-ripd="${p.id}" class="danger">删除</button></td></tr>`).join("")
        || `<tr><td colspan="4" class="muted">没有保留 IP</td></tr>`;
    } catch (e) { $("#rip-table tbody").innerHTML = `<tr><td colspan="4" class="err-cell">${esc(e.message)}</td></tr>`; }
  }
  $("#btn-rip-load").addEventListener("click", loadReserved);
  $("#btn-rip-new").addEventListener("click", async () => {
    const name = prompt("保留IP备注名（可选）：") || "";
    try {
      await api("/api/oci/reserved-ips", { method: "POST", body: { account_id: Number($("#net-account").value), name } });
      loadReserved();
    } catch (e) { toast(e.message, false); }
  });
  $("#rip-table tbody").addEventListener("click", async (e) => {
    const aid = Number($("#net-account").value);
    const a = e.target.closest("button[data-ripa]"), d = e.target.closest("button[data-ripd]");
    try {
      if (a) {
        const iid = prompt("绑定到实例 OCID：");
        if (!iid) return;
        await api(`/api/oci/reserved-ips/${a.dataset.ripa}/assign`, { method: "POST", body: { account_id: aid, instance_id: iid } });
      } else if (d) {
        if (!confirm("删除该保留 IP？")) return;
        await api(`/api/oci/reserved-ips/${d.dataset.ripd}?account_id=${aid}`, { method: "DELETE" });
      }
      loadReserved();
    } catch (err) { toast(err.message, false); }
  });

  /* ---- Cloudflare ---- */
  async function loadCfZones() {
    try {
      const r = await api("/api/cf/zones");
      $("#cf-zone").innerHTML = r.data.map(z =>
        `<option value="${z.id}">${esc(z.name)}</option>`).join("") || `<option value="">（未配置 CF 或没有域名）</option>`;
      if (r.data.length) loadCfRecords();
    } catch (e) {
      $("#cf-zone").innerHTML = `<option value="">CF 未配置或出错：${esc(e.message).slice(0, 40)}</option>`;
    }
  }
  async function loadCfRecords() {
    const zid = $("#cf-zone").value;
    if (!zid) return;
    try {
      const r = await api(`/api/cf/records?zone=${zid}`);
      $("#cf-table tbody").innerHTML = r.data.map(x => `<tr>
        <td>${esc(x.type)}</td><td>${esc(x.name)}</td><td style="max-width:280px;overflow:hidden;text-overflow:ellipsis">${esc(x.content)}</td>
        <td>${x.proxied ? "🟠" : "—"}</td>
        <td class="ops"><button data-cfdel="${x.id}">删除</button></td></tr>`).join("")
        || `<tr><td colspan="5" class="muted">没有解析记录</td></tr>`;
    } catch (e) { toast(e.message, false); }
  }
  $("#btn-cf-load").addEventListener("click", loadCfRecords);
  $("#cf-zone").addEventListener("change", loadCfRecords);
  $("#btn-cf-add").addEventListener("click", () => $("#cf-add-form").classList.toggle("hide"));
  $("#btn-cf-save").addEventListener("click", async () => {
    try {
      await api("/api/cf/records", { method: "POST", body: {
        zone: $("#cf-zone").value, type: $("#cfr-type").value, name: $("#cfr-name").value.trim(),
        content: $("#cfr-content").value.trim(), proxied: $("#cfr-proxied").value === "true" }});
      toast("解析已添加"); $("#cf-add-form").classList.add("hide"); loadCfRecords();
    } catch (e) { toast(e.message, false); }
  });
  $("#cf-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-cfdel]");
    if (!btn) return;
    try {
      await api(`/api/cf/records/${btn.dataset.cfdel}?zone=${$("#cf-zone").value}`, { method: "DELETE" });
      loadCfRecords();
    } catch (err) { toast(err.message, false); }
  });

  /* ================= 用户管理（OCI） ================= */

  $("#btn-usr-load").addEventListener("click", loadUsers);
  $("#btn-usr-new").addEventListener("click", () => $("#usr-new-form").classList.toggle("hide"));
  $("#btn-usr-create").addEventListener("click", async () => {
    try {
      await api("/api/oci/users", { method: "POST", body: {
        account_id: Number($("#usr-account").value), name: $("#un-name").value.trim(),
        email: $("#un-email").value.trim() }});
      toast("用户已创建"); $("#usr-new-form").classList.add("hide"); loadUsers();
    } catch (e) { toast(e.message, false); }
  });

  async function loadUsers() {
    const aid = $("#usr-account").value;
    if (!aid) return;
    try {
      const r = await api(`/api/oci/users?account_id=${aid}`);
      $("#usr-table tbody").innerHTML = r.data.map(u => `<tr>
        <td>${esc(u.name)}</td><td>${esc(u.email)}</td><td>${u.mfa ? "🔒 开启" : "—"}</td>
        <td class="ops">
          <button data-uact="resetpw" data-id="${u.id}" data-name="${esc(u.name)}">重置密码</button>
          <button data-uact="keys" data-id="${u.id}" data-name="${esc(u.name)}">API密钥</button>
          <button data-uact="email" data-id="${u.id}" data-name="${esc(u.name)}">修改邮箱</button>
          <button data-uact="mfa" data-id="${u.id}" data-name="${esc(u.name)}">清除2FA</button>
          <button data-uact="del" class="danger" data-id="${u.id}" data-name="${esc(u.name)}">删除</button>
        </td></tr>`).join("") || `<tr><td colspan="4" class="muted">没有用户</td></tr>`;
    } catch (e) { toast(e.message, false); }
  }

  $("#usr-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-uact]");
    if (!btn) return;
    const aid = Number($("#usr-account").value);
    const uid = btn.dataset.id;
    try {
      if (btn.dataset.uact === "resetpw") {
        if (!confirm(`重置 ${btn.dataset.name} 的控制台密码？`)) return;
        const r = await api(`/api/oci/users/${uid}/reset-password`, { method: "POST", body: { account_id: aid } });
        showLogModal(`一次性密码（只显示这一次）：${btn.dataset.name}`, r.password);
      } else if (btn.dataset.uact === "keys") {
        $("#usr-keys").classList.remove("hide");
        $("#uk-name").textContent = btn.dataset.name;
        $("#uk-name").dataset.uid = uid;
        const r = await api(`/api/oci/users/${uid}/keys?account_id=${aid}`);
        $("#uk-table tbody").innerHTML = r.data.map(k => `<tr>
          <td>${esc(k.fingerprint)}</td><td>${esc(k.time_added)}</td>
          <td class="ops"><button data-ukdel="${esc(k.fingerprint)}">删除</button></td></tr>`).join("")
          || `<tr><td colspan="3" class="muted">没有 API 密钥</td></tr>`;
      } else if (btn.dataset.uact === "email") {
        const email = prompt("新的邮箱地址：");
        if (!email) return;
        await api(`/api/oci/users/${uid}/email`, {method: "POST", body: {account_id: aid, email}});
        toast("邮箱已更新"); loadUsers();
      } else if (btn.dataset.uact === "mfa") {
        if (!confirm(`清除 ${btn.dataset.name} 的 2FA？`)) return;
        const r = await api(`/api/oci/users/${uid}/clear-mfa`, { method: "POST", body: { account_id: aid } });
        toast(`已清除 ${r.deleted || 0} 个 TOTP 设备`); loadUsers();
      } else {
        if (!confirm(`删除用户 ${btn.dataset.name}？`)) return;
        await api(`/api/oci/users/${uid}?account_id=${aid}`, { method: "DELETE" });
        loadUsers();
      }
    } catch (err) { toast(err.message, false); }
  });
  $("#uk-table tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-ukdel]");
    if (!btn) return;
    const aid = Number($("#usr-account").value);
    const uid = $("#uk-name").dataset.uid;
    try {
      await api(`/api/oci/users/${uid}/keys/${encodeURIComponent(btn.dataset.ukdel)}?account_id=${aid}`, { method: "DELETE" });
      toast("已删除");
      $(`#usr-table tbody button[data-uact="keys"][data-id="${uid}"]`).click();
    } catch (err) { toast(err.message, false); }
  });
  $("#btn-uk-add").addEventListener("click", async () => {
    const aid = Number($("#usr-account").value);
    const uid = $("#uk-name").dataset.uid;
    try {
      await api(`/api/oci/users/${uid}/keys`, { method: "POST", body: { account_id: aid, key_pem: $("#uk-pem").value } });
      toast("公钥已上传");
      $(`#usr-table tbody button[data-uact="keys"][data-id="${uid}"]`).click();
    } catch (e) { toast(e.message, false); }
  });
  $("#btn-uk-smtp").addEventListener("click", async () => {
    const aid = Number($("#usr-account").value);
    const uid = $("#uk-name").dataset.uid;
    try {
      const r = await api(`/api/oci/users/${uid}/smtp-credential`, { method: "POST", body: { account_id: aid } });
      showLogModal("SMTP 发信凭据（只显示这一次）", `用户: ${r.user}\n密码: ${r.password}\n主机: smtp.email.ap-singapore-1.oci.oraclecloud.com（按区域）\n端口: 587`);
    } catch (e) { toast(e.message, false); }
  });

  /* ================= 对象存储 ================= */

  $("#btn-os-load").addEventListener("click", loadBuckets);
  $("#btn-os-new").addEventListener("click", async () => {
    const name = prompt("新建 Bucket 名称：");
    if (!name) return;
    try {
      await api("/api/oci/buckets", { method: "POST", body: { account_id: Number($("#os-account").value), name } });
      toast("Bucket 创建中"); loadBuckets();
    } catch (e) { toast(e.message, false); }
  });

  async function loadBuckets() {
    const aid = $("#os-account").value;
    if (!aid) return;
    try {
      const r = await api(`/api/oci/buckets?account_id=${aid}`);
      $("#os-buckets tbody").innerHTML = r.data.map(b => `<tr>
        <td>${esc(b.name)}</td><td>${esc(b.created)}</td>
        <td class="ops"><button data-bopen="${esc(b.name)}">打开</button>
          <button data-bdel="${esc(b.name)}" class="danger">删除</button></td></tr>`).join("")
        || `<tr><td colspan="3" class="muted">没有 Bucket</td></tr>`;
    } catch (e) { toast(e.message, false); }
  }
  $("#os-buckets tbody").addEventListener("click", async (e) => {
    const open = e.target.closest("button[data-bopen]");
    const del = e.target.closest("button[data-bdel]");
    const aid = Number($("#os-account").value);
    try {
      if (open) { $("#os-files").classList.remove("hide"); $("#os-cur").textContent = "Bucket: " + open.dataset.bopen; loadObjects(open.dataset.bopen, ""); }
      else if (del) {
        if (!confirm(`删除 Bucket ${del.dataset.bdel}？（需为空）`)) return;
        await api(`/api/oci/buckets/${encodeURIComponent(del.dataset.bdel)}?account_id=${aid}`, { method: "DELETE" });
        loadBuckets();
      }
    } catch (err) { toast(err.message, false); }
  });

  let curBucket = "";
  async function loadObjects(bucket, prefix) {
    curBucket = bucket || curBucket;
    const aid = $("#os-account").value;
    try {
      const r = await api(`/api/oci/objects?account_id=${aid}&bucket=${encodeURIComponent(curBucket)}&prefix=${encodeURIComponent(prefix || "")}`);
      $("#os-table tbody").innerHTML = (r.prefixes || []).map(p => `<tr>
        <td colspan="4"><a href="javascript:void(0)" data-oprefix="${esc(p)}">📁 ${esc(p)}</a></td></tr>`).join("")
        + r.objects.map(o => `<tr>
          <td>${esc(o.name)}</td><td>${(o.size / 1024).toFixed(1)}KB</td><td>${esc(o.modified)}</td>
          <td class="ops"><button data-oview="${esc(o.name)}">查看</button>
            <button data-odel="${esc(o.name)}" class="danger">删除</button></td></tr>`).join("")
        || `<tr><td colspan="4" class="muted">空</td></tr>`;
      $("#os-objname").value = prefix || "";
    } catch (e) { toast(e.message, false); }
  }
  $("#os-table tbody").addEventListener("click", async (e) => {
    const aid = Number($("#os-account").value);
    const dir = e.target.closest("a[data-oprefix]");
    const view = e.target.closest("button[data-oview]");
    const del = e.target.closest("button[data-odel]");
    try {
      if (dir) loadObjects(curBucket, dir.dataset.oprefix);
      else if (view) {
        const r = await api(`/api/oci/objects/content?account_id=${aid}&bucket=${encodeURIComponent(curBucket)}&name=${encodeURIComponent(view.dataset.oview)}`);
        showLogModal(view.dataset.oview + (r.truncated ? "（已截断）" : ""), r.content);
      } else if (del) {
        if (!confirm(`删除 ${del.dataset.odel}？`)) return;
        await api(`/api/oci/objects?account_id=${aid}&bucket=${encodeURIComponent(curBucket)}&name=${encodeURIComponent(del.dataset.odel)}`, { method: "DELETE" });
        loadObjects(curBucket, $("#os-objname").value);
      }
    } catch (err) { toast(err.message, false); }
  });
  $("#btn-os-up").addEventListener("click", () => {
    const cur = $("#os-objname").value;
    const idx = cur.lastIndexOf("/", cur.length - 2);
    loadObjects(curBucket, idx > 0 ? cur.slice(0, idx + 1) : "");
  });
  $("#btn-os-open").addEventListener("click", () => loadObjects(curBucket, $("#os-objname").value.trim()));
  $("#btn-os-upload").addEventListener("click", () => $("#os-file").click());
  $("#os-file").addEventListener("change", async () => {
    const f = $("#os-file").files[0];
    if (!f) return;
    const buf = await f.arrayBuffer();
    let binary = "";
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    const name = ($("#os-objname").value.trim() || "") + f.name;
    try {
      await api("/api/oci/objects/put", { method: "POST", body: {
        account_id: Number($("#os-account").value), bucket: curBucket, name, content_base64: btoa(binary) }});
      toast("上传完成"); loadObjects(curBucket, $("#os-objname").value);
    } catch (e) { toast(e.message, false); }
    $("#os-file").value = "";
  });
})();
