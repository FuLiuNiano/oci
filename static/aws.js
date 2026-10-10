/* Lightsail owns its account state; OCI/GCP selectors and sessions remain separate. */
(() => {
  let accounts = [], selected = '', editing = null, generation = 0, regions = [], options = null;
  let busy = false, metricsInstance = '', instances = [];
  const el = id => document.getElementById('aws-' + id);
  const current = () => accounts.find(a => String(a.id) === selected);
  const message = text => { el('message').textContent = text; };
  const hide = name => el(name).classList.add('hide');
  const show = name => { el(name).classList.remove('hide'); el(name).scrollIntoView({behavior:'smooth',block:'nearest'}); };
  const bytes = n => n === null || n === undefined ? '—' : (Number(n) / 1024**3).toFixed(3) + ' GiB';
  const fill = (name,items,label=x=>x,value=x=>x) => { el(name).innerHTML = items.map(x => `<option value="${esc(value(x))}">${esc(label(x))}</option>`).join(''); };
  function capture() {
    const a = current();
    if (!a) throw new Error('请先添加或选择 AWS 账号');
    return {...a,generation};
  }
  const valid = a => a.generation === generation && String(a.id) === selected && a.revision === current()?.revision;
  const path = (a,suffix) => `/api/aws/accounts/${a.id}/${suffix}`;
  const query = (a,values={}) => '?' + new URLSearchParams({revision:a.revision,...values});
  const post = (a,suffix,body) => api(path(a,suffix),{method:'POST',body:{revision:a.revision,...body}});
  async function guarded(fn) {
    if (busy) { toast('请等待当前 AWS 请求完成',false); return; }
    const started = generation;
    busy = true;
    try { await fn(); }
    catch (error) { if (started === generation) { message(error.message); toast(error.message,false); } }
    finally { busy = false; }
  }
  function choose() {
    selected = el('account').value; generation++;
    const a = current();
    el('route').textContent = a?.route || '';
    el('identity').textContent = a ? `${a.name} · ${a.region} · Access Key ${a.access_key_hint}${a.temporary ? ' · 临时凭据' : ''}` : '尚未配置 AWS 账号';
    el('instances').replaceChildren(); el('summary').replaceChildren();
    for (const name of ['create-form','sync-form','metrics-panel','snapshots-panel','ips-panel','account-form']) hide(name);
    el('account-form').reset(); editing = null; options = null; instances = []; metricsInstance = '';
    for (const name of ['metrics-rows','snapshot-rows','ip-rows']) el(name).replaceChildren();
    message(a ? '点击刷新实例，读取当前账号和区域的 Lightsail。' : '请添加 AWS 账号。');
  }
  async function loadAccounts(preferred = selected) {
    const result = await Promise.all([api('/api/aws/accounts'),api('/api/aws/regions')]);
    accounts = result[0]; regions = result[1];
    el('account').innerHTML = accounts.length ? accounts.map(a => `<option value="${a.id}">${esc(a.name)} · ${esc(a.region)}</option>`).join('') : '<option value="">请添加账号</option>';
    el('account').value = accounts.some(a => String(a.id) === String(preferred)) ? String(preferred) : String(accounts[0]?.id || '');
    choose();
  }
  VIEW_LOADERS.aws = () => guarded(() => loadAccounts());
  el('account').onchange = choose;
  function accountForm(a = null) {
    editing = a ? {...a} : null; el('account-form').reset();
    el('form-title').textContent = a ? '编辑 AWS 账号' : '添加 AWS 账号';
    el('name').value = a?.name || '';
    fill('region',regions,r=>`${r.name} · ${r.id}`,r=>r.id);
    el('region').value = a?.region || 'ap-northeast-1';
    el('access').required = el('secret').required = !a;
    el('proxy').placeholder = a?.proxied ? '留空保留已保存代理；或填写新代理' : 'socks5h://地址:1080 或 http://地址:端口';
    show('account-form');
  }
  el('new').onclick = () => accountForm();
  el('edit').onclick = () => { if (current()) accountForm(current()); };
  el('cancel-account').onclick = () => { hide('account-form'); el('account-form').reset(); editing = null; };
  el('account-form').onsubmit = event => {
    event.preventDefault(); guarded(async () => {
      const access = el('access').value.trim(), secret = el('secret').value.trim();
      if (Boolean(access) !== Boolean(secret)) throw new Error('更换凭据请同时填写 Access Key 和 Secret Key');
      if (el('direct').checked && el('proxy').value.trim()) throw new Error('移除代理时请清空代理地址');
      const body = {name:el('name').value.trim(),region:el('region').value,access_key_id:access || null,secret_access_key:secret || null,
        session_token:el('token').value.trim() || null,clear_token:el('clear-token').checked,
        proxy_url:el('direct').checked ? '' : (el('proxy').value.trim() || (editing ? null : '')),remove_proxy:el('direct').checked,revision:editing?.revision};
      const saved = await api('/api/aws/accounts' + (editing ? '/' + editing.id : ''),{method:editing ? 'PUT':'POST',body});
      el('account-form').reset(); editing = null;
      await loadAccounts(String(saved.id)); toast('AWS 账号已保存');
    });
  };
  el('remove').onclick = () => guarded(async () => {
    const a = capture();
    if (!confirm(`移除 AWS 配置“${a.name}”？云端资源和已有 SSH 会话会保留。`)) return;
    await api(`/api/aws/accounts/${a.id}` + query(a),{method:'DELETE'}); await loadAccounts();
  });
  el('test').onclick = () => guarded(async () => {
    const a = capture(); message(`正在通过${a.route}验证 AWS 凭据…`);
    const identity = await api(path(a,'identity') + query(a));
    if (valid(a)) message(`认证成功 · AWS 账号 ${identity.account_id} · ${identity.arn} · ${a.route}。仍需具备 Lightsail 权限。`);
  });
  async function refresh(a) {
    message(`正在通过${a.route}读取 ${a.name} / ${a.region}…`);
    const data = await api(path(a,'instances') + query(a));
    if (!valid(a)) return;
    instances = data.instances;
    el('summary').textContent = `实例 ${data.overview.total} · 运行 ${data.overview.running}`;
    el('instances').innerHTML = instances.length ? instances.map(i => `<article class="card aws-instance"><h3>${esc(i.name)} <small>${esc(i.status)}</small></h3><p class="muted">${esc(a.name)} · ${esc(a.route)}</p><dl><dt>可用区</dt><dd>${esc(i.zone)}</dd><dt>系统 / 套餐</dt><dd>${esc(i.blueprint)} / ${esc(i.machine_type)}</dd><dt>CPU / 内存</dt><dd>${esc(i.cpus ?? '未知')} vCPU / ${esc(i.memory_gb ?? '未知')} GB</dd><dt>公网 IPv4</dt><dd>${esc(i.public_ip || '无')}${i.static_ip ? '（静态）' : ''}</dd><dt>公网 IPv6</dt><dd>${esc(i.ipv6.join('、') || '无')}</dd><dt>内网 IP</dt><dd>${esc(i.private_ip)}</dd><dt>SSH 用户</dt><dd>${esc(i.username)} · ${esc(i.key_name)}</dd></dl><div class="bar">${[['start','启动'],['stop','停止'],['reboot','重启'],['snapshot','创建快照'],['metrics','CPU / 流量'],['delete','删除实例']].map(([action,label])=>`<button class="ghost" data-action="${action}">${label}</button>`).join('')}</div></article>`).join('') : '<p class="muted">当前区域暂无 Lightsail 实例。</p>';
    $$('.aws-instance',el('instances')).forEach((card,index) => {
      $$('button[data-action]',card).forEach(button => button.onclick = () => guarded(async () => {
        if (!valid(a)) throw new Error('账号已变化，请刷新实例');
        const i = data.instances[index], action = button.dataset.action;
        if (action === 'metrics') { metricsInstance = i.name; return metrics(a); }
        if (action === 'snapshot') {
          const name = prompt(`为 ${a.name} / ${i.name} 创建快照（会产生存储费用），请输入快照名称：`,`${i.name}-backup-${Date.now()}`);
          if (!name) return;
          await track(a,await post(a,'snapshots',{instance_name:i.name,name,action:'create'}));
          if (valid(a)) await snapshots(a); return;
        }
        let confirmed_name = '';
        if (action==='delete') {
          confirmed_name = prompt(`删除 ${a.name} / ${i.name} 会删除系统盘，无法撤销！请先创建快照并确认可用。请输入实例名称确认：`);
          if (confirmed_name===null) return;
          if (confirmed_name!==i.name) throw new Error('名称不匹配，已取消');
        } else if (!confirm(`${a.name} / ${i.name}：确认${button.textContent}？`)) return;
        await track(a,await post(a,'action',{name:i.name,action,confirmed_name}));
        if (valid(a)) await refresh(a);
      }));
    });
    message(`已读取 ${instances.length} 台 Lightsail · ${a.name} · ${a.region} · ${a.route}`);
  }
  el('refresh').onclick = () => guarded(() => refresh(capture()));
  async function track(a,response) {
    if (!valid(a)) return;
    if (!response.operations?.length) throw new Error('请求已提交，但没有返回操作编号；请刷新核对，避免重复创建');
    for (let count=0; count<60 && valid(a) && curView==='aws'; count++) {
      const states = await Promise.all(response.operations.map(op => api(path(a,'operation') + query(a,{operation_id:op.id}))));
      if (!valid(a)) return;
      message('云端操作：' + states.map(op => op.status).join(' · '));
      if (states.every(op=>op.terminal || ['Succeeded','Completed'].includes(op.status))) { toast('Lightsail 操作完成'); return; }
      await new Promise(resolve=>setTimeout(resolve,2000));
    }
    if (valid(a)) message('云端操作已提交；等待超时或离开页面会停止轮询，请刷新核对最终状态。');
  }
  function bundles() {
    if (!options) return;
    const minimum = options.blueprints.find(b=>b.id===el('blueprint').value)?.min_power || 0;
    const items = options.bundles.filter(b=>b.power>=minimum && (el('ip-type').value==='ipv6' || b.ipv4_count>0)).sort((a,b)=>a.price-b.price);
    fill('bundle',items,b=>`${b.name} · ${b.cpus} vCPU / ${b.memory_gb} GB / ${b.disk_gb} GB · $${b.price}/月${b.ipv4_count ? '' : ' · 仅 IPv6'}`,b=>b.id);
  }
  el('blueprint').onchange = bundles; el('ip-type').onchange = bundles;
  el('open-create').onclick = () => guarded(async () => {
    const a = capture(); message('正在读取镜像、套餐、可用区和密钥对…');
    const data = await api(path(a,'options') + query(a)); if (!valid(a)) return;
    options = data; el('create-form').reset();
    fill('zone',data.zones); fill('blueprint',data.blueprints,b=>b.name,b=>b.id); fill('key-pair',data.key_pairs);
    bundles(); show('create-form'); message('请选择创建参数和参考月价。');
  });
  el('cancel-create').onclick = () => hide('create-form');
  el('create-form').onsubmit = event => {
    event.preventDefault(); guarded(async () => {
      const a = capture();
      if (!confirm(`在 ${a.name} / ${a.region} 创建 ${el('vm-name').value}？会产生费用，以 AWS 实际账单为准。`)) return;
      const response = await post(a,'create',{name:el('vm-name').value.trim(),zone:el('zone').value,blueprint_id:el('blueprint').value,
        bundle_id:el('bundle').value,key_name:el('key-pair').value,ip_type:el('ip-type').value});
      if (valid(a)) hide('create-form'); await track(a,response); if (valid(a)) await refresh(a);
    });
  };
  el('open-sync').onclick = () => { if (current()) { el('sync-form').reset(); show('sync-form'); } };
  el('cancel-sync').onclick = () => hide('sync-form');
  el('sync-form').onsubmit = event => {
    event.preventDefault(); guarded(async () => {
      const a = capture(); const data = await post(a,'sync-ssh',{username:el('sync-user').value.trim(),ssh_proxy:el('sync-proxy').value.trim(),allow_private:el('sync-private').checked});
      if (!valid(a)) return;
      hide('sync-form'); message(`新增 ${data.added}，更新 ${data.updated}，跳过 ${data.skipped}。${data.note}`);
      if (typeof loadSessions==='function') await loadSessions();
    });
  };
  async function metrics(a) {
    const name = metricsInstance, days = el('metrics-days').value;
    if (!name) throw new Error('请先在实例卡片选择 CPU / 流量');
    message('正在读取 Lightsail 监控…');
    const data = await api(path(a,'metrics') + query(a,{name,days}));
    if (!valid(a) || name!==metricsInstance || days!==el('metrics-days').value) return;
    el('metrics-title').textContent = `${name} · CPU 与流量`;
    el('metrics-summary').textContent = `${a.name} · ${a.region} · ${a.route} · 接收 ${bytes(data.rx_bytes)} / 发送 ${bytes(data.tx_bytes)}`;
    el('metrics-rows').innerHTML = data.rows.length ? data.rows.map(r=>`<tr><td>${esc(r.timestamp)}</td><td>${r.cpu===null ? '—' : Number(r.cpu).toFixed(1)+'%'}</td><td>${bytes(r.rx_bytes)}</td><td>${bytes(r.tx_bytes)}</td></tr>`).join('') : '<tr><td colspan="4">暂无监控数据</td></tr>';
    show('metrics-panel'); message('监控读取完成');
  }
  el('metrics-refresh').onclick = () => guarded(()=>metrics(capture()));
  el('metrics-days').onchange = () => guarded(()=>metrics(capture()));
  async function snapshots(a) {
    const rows = await api(path(a,'snapshots') + query(a)); if (!valid(a)) return;
    el('snapshot-rows').innerHTML = rows.length ? rows.map(s=>`<tr><td>${esc(s.name)}</td><td>${esc(s.source)}</td><td>${esc(s.state)}</td><td>${esc(s.size_gb ?? '未知')} GB</td><td><button class="ghost">删除</button></td></tr>`).join('') : '<tr><td colspan="5">暂无实例快照</td></tr>';
    $$('#aws-snapshot-rows button').forEach((button,index)=>button.onclick=()=>guarded(async()=>{
      if (!valid(a)) throw new Error('账号已变化，请刷新');
      const name = rows[index].name;
      if (prompt(`永久删除快照 ${a.name} / ${name}，请输入快照名称：`)!==name) return;
      await track(a,await post(a,'snapshots',{name,action:'delete',confirmed_name:name})); if (valid(a)) await snapshots(a);
    }));
    show('snapshots-panel'); message(`快照已读取 · ${a.name} · ${a.region} · ${a.route}`);
  }
  el('open-snapshots').onclick = el('snapshots-refresh').onclick = () => guarded(()=>snapshots(capture()));
  async function ips(a) {
    const rows = await api(path(a,'static-ips') + query(a)); if (!valid(a)) return;
    el('ip-rows').innerHTML = rows.length ? rows.map(ip=>`<tr><td>${esc(ip.name)}</td><td>${esc(ip.ip)}</td><td>${esc(ip.attached_to || '未绑定')}</td><td>${ip.attached ? '<button class="ghost" data-action="detach">解绑</button>' : '<button class="ghost" data-action="attach">绑定</button> <button class="ghost" data-action="release">释放</button>'}</td></tr>`).join('') : '<tr><td colspan="4">暂无静态 IP</td></tr>';
    $$('#aws-ip-rows tr').forEach((row,index)=>$$('button',row).forEach(button=>button.onclick=()=>guarded(async()=>{
      if (!valid(a)) throw new Error('账号已变化，请刷新');
      const ip = rows[index], action = button.dataset.action;
      let instance_name = '', confirmed_name = '';
      if (action==='attach') {
        instance_name = prompt(`将 ${a.name} / ${ip.name} 绑定到当前区域的哪个实例？请输入实例名称。会改变公网 IP：`);
        if (!instance_name) return;
      } else {
        confirmed_name = prompt(`${a.name} / ${ip.name}：${action==='detach' ? '解绑会改变实例公网 IP 并可能中断 SSH' : '释放后无法保证再次获得相同地址'}。请输入静态 IP 名称确认：`);
        if (confirmed_name!==ip.name) return;
      }
      await track(a,await post(a,'static-ips',{name:ip.name,action,instance_name,confirmed_name})); if (valid(a)) await ips(a);
    })));
    show('ips-panel'); message(`静态 IP 已读取 · ${a.name} · ${a.region} · ${a.route}`);
  }
  el('open-ips').onclick = el('ips-refresh').onclick = () => guarded(()=>ips(capture()));
  el('allocate-ip').onclick = () => guarded(async()=>{
    const a = capture(), name = prompt(`在 ${a.name} / ${a.region} 申请静态 IP，未绑定可能收费，请输入名称：`);
    if (!name) return;
    await track(a,await post(a,'static-ips',{name,action:'allocate'})); if (valid(a)) await ips(a);
  });
})();
