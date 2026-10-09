/* GCP has its own state: never add its accounts to the OCI account selector. */
(() => {
  let accounts = [], selected = '', editing = null, generation = 0, options = null;
  let busy = false;
  const el = id => document.getElementById('gcp-' + id);
  const current = () => accounts.find(a => String(a.id) === selected);
  const message = text => { el('message').textContent = text; };
  const hide = name => el(name).classList.add('hide');
  const show = name => { el(name).classList.remove('hide'); el(name).scrollIntoView({behavior:'smooth',block:'nearest'}); };
  const bytes = n => (Number(n || 0) / 1024 / 1024 / 1024).toFixed(3) + ' GiB';
  function capture() {
    const a = current();
    if (!a) throw new Error('请先添加或选择 GCP 账号');
    return {...a, generation};
  }
  const valid = a => a.generation === generation && String(a.id) === selected && a.revision === current()?.revision;
  function path(a, suffix) { return `/api/gcp/accounts/${a.id}/${suffix}`; }
  const query = (a, values = {}) => '?' + new URLSearchParams({revision:a.revision,...values});
  async function guarded(fn) {
    if (busy) { toast('请等待当前 GCP 请求完成', false); return; }
    busy = true;
    try { await fn(); } catch (error) { message(error.message); toast(error.message,false); }
    finally { busy = false; }
  }
  function choose() {
    selected = el('account').value;
    generation++;
    const a = current();
    el('route').textContent = a?.route || '';
    el('identity').textContent = a ? `${a.name} · ${a.project_id} · ${a.client_email}` : '尚未配置 GCP 账号';
    el('instances').replaceChildren(); el('summary').replaceChildren();
    for (const name of ['create-form','sync-form','traffic-panel','account-form']) hide(name);
    options = null;
    message(a ? '点击刷新实例 / 测试连接，读取当前项目。' : '请添加 GCP 账号。');
  }
  async function loadAccounts(preferred = selected) {
    accounts = await api('/api/gcp/accounts');
    el('account').innerHTML = accounts.length ? accounts.map(a => `<option value="${a.id}">${esc(a.name)} · ${esc(a.project_id)}</option>`).join('') : '<option value="">请添加账号</option>';
    el('account').value = accounts.some(a => String(a.id) === String(preferred)) ? String(preferred) : String(accounts[0]?.id || '');
    choose();
  }
  VIEW_LOADERS.gcp = () => guarded(() => loadAccounts());
  el('account').addEventListener('change',choose);
  function accountForm(a = null) {
    editing = a ? {...a} : null;
    el('account-form').reset();
    el('form-title').textContent = a ? '编辑 GCP 账号' : '添加 GCP 账号';
    el('name').value = a?.name || ''; el('project').value = a?.project_id || '';
    el('proxy').placeholder = a?.proxied ? '留空保留已保存代理；或填写新代理' : 'socks5h://地址:1080 或 http://地址:端口';
    show('account-form');
  }
  el('new').onclick = () => accountForm();
  el('edit').onclick = () => { if (current()) accountForm(current()); };
  el('cancel-account').onclick = () => { el('account-form').reset(); editing = null; hide('account-form'); };
  el('json-file').onchange = async () => {
    const file = el('json-file').files[0];
    if (!file) return;
    if (file.size > 65536) { toast('密钥文件过大',false); return; }
    try { const raw = await file.text(); const data = JSON.parse(raw); el('json').value = raw; if (!el('project').value) el('project').value = data.project_id || ''; }
    catch (_) { toast('无法读取 JSON 密钥文件',false); }
  };
  el('account-form').onsubmit = event => {
    event.preventDefault();
    guarded(async () => {
      let credentials = null;
      if (el('json').value.trim()) { try { credentials = JSON.parse(el('json').value); } catch (_) { throw new Error('JSON 格式无效'); } }
      if (!editing && !credentials) throw new Error('请上传或粘贴服务账号 JSON 密钥');
      if (el('direct').checked && el('proxy').value.trim()) throw new Error('移除代理时请清空代理地址');
      const body = {name:el('name').value.trim(),project_id:el('project').value.trim(),credentials,
        proxy_url:el('direct').checked ? '' : (el('proxy').value.trim() || (editing ? null : '')), remove_proxy:el('direct').checked, revision:editing?.revision};
      const saved = await api('/api/gcp/accounts' + (editing ? '/' + editing.id : ''), {method:editing ? 'PUT' : 'POST',body});
      el('account-form').reset(); editing = null;
      await loadAccounts(String(saved.id)); toast('GCP 账号已保存');
    });
  };
  el('remove').onclick = () => guarded(async () => {
    const a = capture();
    if (!confirm(`移除 GCP 账号“${a.name}”？云端实例和已有 SSH 会话会保留。`)) return;
    await api(`/api/gcp/accounts/${a.id}` + query(a), {method:'DELETE'}); await loadAccounts();
  });
  async function refresh(a) {
    message(`正在通过${a.route}读取 ${a.name}…`);
    const data = await api(path(a,'instances') + query(a));
    if (!valid(a)) return;
    const s = data.overview;
    el('summary').innerHTML = `<b>实例 ${Number(s.total)}</b><b>运行 ${Number(s.running)}</b><span>免费额度候选 ${Number(s.free_tier_candidates)}（不保证免费）</span><span>${esc(Object.entries(s.zones).map(([z,n]) => `${z}: ${n}`).join(' · '))}</span>`;
    el('instances').innerHTML = data.instances.map(i => `<article class="card gcp-instance"><h3>${esc(i.name)} <small>${esc(i.status)}</small></h3><p class="muted">${esc(a.name)} · ${esc(a.route)}</p><dl><dt>可用区</dt><dd>${esc(i.zone)}</dd><dt>机型</dt><dd>${esc(i.machine_type)}</dd><dt>公网 IP</dt><dd>${esc(i.public_ip || '无')}</dd><dt>内网 IP</dt><dd>${esc(i.private_ip)}</dd><dt>磁盘</dt><dd>${esc(i.disks.map(d => d.name + (d.auto_delete ? '（随实例删除）' : '（保留）')).join('、'))}</dd></dl><div class="bar">${[['start','启动'],['stop','停止'],['reset','重启'],['change-ip','更换临时 IP'],['delete','删除（保留磁盘）']].map(([action,label]) => `<button data-action="${action}" class="ghost">${label}</button>`).join('')}</div></article>`).join('');
    $$('.gcp-instance',el('instances')).forEach((card,index) => {
      $$('button[data-action]',card).forEach(button => button.onclick = () => guarded(async () => {
        if (!valid(a)) throw new Error('账号已变化，请刷新实例');
        const i = data.instances[index], action = button.dataset.action;
        let confirmed_name = '';
        if (['delete','change-ip'].includes(action)) {
          const description = action === 'delete' ? '删除实例，先关闭附加磁盘自动删除；保留的磁盘仍可能收费' : '移除并重新申请临时公网 IPv4；SSH 会暂时断开，IP 可能变化，静态 IP 不支持此操作';
          confirmed_name = prompt(`${a.name} / ${i.name}：${description}。请输入实例名称确认：`);
          if (confirmed_name === null) return;
          if (confirmed_name !== i.name) throw new Error('实例名称不匹配，操作已取消');
        } else if (!confirm(`${a.name} / ${i.name}：确认${button.textContent}？`)) return;
        message('正在提交云端操作…');
        const op = await api(path(a,'action'),{method:'POST',body:{revision:a.revision,zone:i.zone,name:i.name,action,confirmed_name,preserve_disks:true}});
        await track(a,i.zone,op);
      }));
    });
    message(`已读取 ${data.instances.length} 台实例 · ${a.name} · ${a.route}`);
  }
  async function track(a,zone,op) {
    if (!valid(a)) return;
    if (!op.name) throw new Error('操作已提交，但没有返回操作编号，请刷新实例确认');
    for (let count=0; count<60 && valid(a) && curView === 'gcp'; count++) {
      const result = await api(path(a,'operation') + query(a,{zone,name:op.name}));
      if (!valid(a)) return;
      message(`云端操作 ${op.name} · ${result.status}`);
      if (result.status === 'DONE') { await refresh(a); toast('GCP 操作完成'); return; }
      await new Promise(resolve => setTimeout(resolve,3000));
    }
    if (valid(a)) message('操作已提交；离开页面或等待超时会停止轮询，不会取消云端操作。请刷新实例确认。');
  }
  el('refresh').onclick = () => guarded(() => refresh(capture()));
  const fillSelect = (name, items, label = x => x, value = x => x) => { el(name).innerHTML = items.map(x => `<option value="${esc(value(x))}">${esc(label(x))}</option>`).join(''); };
  function subnets() {
    if (!options) return;
    const region = el('zone').value.split('-').slice(0,-1).join('-'), network = el('network').value;
    const automatic = options.networks.find(n => n.name === network)?.automatic;
    const items = options.subnets.filter(s => s.region === region && s.network === network);
    fillSelect('subnet',items,s => s.name,s => s.name);
    if (automatic) el('subnet').insertAdjacentHTML('afterbegin','<option value="">自动选择区域子网</option>');
    el('subnet').required = !automatic;
  }
  let machineGeneration = 0;
  async function machines(a) {
    const zone = el('zone').value, request = ++machineGeneration;
    el('machine').replaceChildren(); subnets();
    const data = await api(path(a,'options') + query(a,{zone}));
    if (!valid(a) || request !== machineGeneration || zone !== el('zone').value) return;
    const ordered = data.machine_types.sort((x,y) => (x.name === 'e2-micro' ? -1 : y.name === 'e2-micro' ? 1 : x.name.localeCompare(y.name)));
    fillSelect('machine',ordered,m => `${m.name} · ${m.cpus} vCPU / ${m.memory_mb} MB`,m => m.name);
  }
  el('zone').onchange = () => guarded(() => machines(capture()));
  el('network').onchange = subnets;
  el('open-create').onclick = () => guarded(async () => {
    const a = capture(); message('正在读取可用区、网络和机型…');
    const data = await api(path(a,'options') + query(a));
    if (!valid(a)) return;
    options = data; fillSelect('zone',data.zones); fillSelect('network',data.networks,n => n.name,n => n.name);
    await machines(a);
    if (valid(a)) { show('create-form'); message('请选择创建参数。'); }
  });
  el('cancel-create').onclick = () => hide('create-form');
  el('create-form').onsubmit = event => {
    event.preventDefault(); guarded(async () => {
      const a = capture();
      if (!confirm(`在 ${a.name} / ${a.project_id} 创建 ${el('vm-name').value}？资源可能收费。`)) return;
      const body = {revision:a.revision,name:el('vm-name').value.trim(),zone:el('zone').value,machine_type:el('machine').value,image:el('image').value,
        disk_gb:Number(el('disk').value),network:el('network').value,subnet:el('subnet').value,public_ip:el('public').checked,username:el('user').value.trim(),ssh_key:el('key').value.trim()};
      message('正在提交创建请求…');
      const op = await api(path(a,'create'),{method:'POST',body});
      if (valid(a)) hide('create-form');
      await track(a,body.zone,op);
    });
  };
  el('open-sync').onclick = () => { if (current()) { el('sync-form').reset(); show('sync-form'); } };
  el('cancel-sync').onclick = () => hide('sync-form');
  el('sync-form').onsubmit = event => {
    event.preventDefault(); guarded(async () => {
      const a = capture();
      const data = await api(path(a,'sync-ssh'),{method:'POST',body:{revision:a.revision,username:el('sync-user').value.trim(),ssh_proxy:el('sync-proxy').value.trim(),allow_private:el('sync-private').checked}});
      if (!valid(a)) return;
      message(`新增 ${data.added}，更新 ${data.updated}，跳过 ${data.skipped}。${data.note}`); hide('sync-form');
      if (typeof loadSessions === 'function') await loadSessions();
    });
  };
  el('traffic').onclick = () => guarded(async () => {
    const a = capture(); message('正在读取近 90 天网卡流量…');
    const data = await api(path(a,'traffic') + query(a));
    if (!valid(a)) return;
    el('traffic-summary').textContent = `${a.name} · ${a.route} · 接收 ${bytes(data.rx_bytes)} / 发送 ${bytes(data.tx_bytes)}`;
    el('traffic-rows').innerHTML = data.rows.length ? data.rows.map(r => `<tr><td>${esc(r.date)}</td><td>${esc(r.instance_id)}</td><td>${bytes(r.rx_bytes)}</td><td>${bytes(r.tx_bytes)}</td></tr>`).join('') : '<tr><td colspan="4">暂无监控数据</td></tr>';
    show('traffic-panel'); message('流量读取完成');
  });
})();
