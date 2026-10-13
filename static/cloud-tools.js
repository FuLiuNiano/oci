/* Provider pages own their selectors and epochs. Never merge accounts by numeric ID alone. */
(() => {
  const nav=$('#cloud-nav');
  function requestToken(){
    if(crypto.randomUUID)return crypto.randomUUID();
    const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
    const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }
  for (const group of $$('details[data-nav-group]',nav)) {
    const saved=localStorage.getItem('panel-nav-'+group.dataset.navGroup);
    if (saved!==null) group.open=saved==='1';
    group.addEventListener('toggle',()=>{
      if(group.open)for(const other of $$('details[data-nav-group]',nav))if(other!==group)other.open=false;
      localStorage.setItem('panel-nav-'+group.dataset.navGroup,group.open?'1':'0');
    });
  }
  const labels={id:'编号',name:'名称',status:'状态',state:'状态',region:'区域',zone:'可用区',date:'日期',time:'时间',amount:'费用',unit:'单位',value:'配额',code:'代码',metric:'指标',limit:'上限',usage:'已使用',email:'邮箱',mfa:'已启用 2FA',message:'说明',days:'天数',scope:'统计范围',total:'合计',total_gb:'总出站 GB',public_ip:'公网 IPv4',private_ip:'内网 IPv4',machine_type:'规格',cpu:'CPU %',rx_bytes:'接收字节',tx_bytes:'发送字节',enabled:'启用',error:'错误'};
  function display(value) {
    if (value===null || value===undefined) return '—';
    if (typeof value==='boolean') return value?'是':'否';
    return typeof value==='object'?JSON.stringify(value):String(value);
  }
  function renderResult(node,data) {
    if (Array.isArray(data) && data.length && data.every(x=>x && typeof x==='object' && !Array.isArray(x))) {
      const keys=[...new Set(data.flatMap(x=>Object.keys(x)))];
      node.innerHTML=`<div class="table-wrap"><table><thead><tr>${keys.map(k=>`<th>${esc(labels[k]||k)}</th>`).join('')}</tr></thead><tbody>${data.map(r=>`<tr>${keys.map(k=>`<td>${esc(display(r[k]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    } else if (data && typeof data==='object' && !Array.isArray(data)) {
      node.innerHTML=Object.entries(data).map(([k,v])=>`<details open><summary>${esc(labels[k]||k)}</summary><div data-result-key="${esc(k)}"></div></details>`).join('');
      for (const [k,v] of Object.entries(data)) renderResult(Array.from(node.children).find(d=>d.firstElementChild.textContent===(labels[k]||k)).lastElementChild,v);
    } else node.textContent=Array.isArray(data)?(data.length?data.map(display).join('\n'):'暂无数据'):display(data);
  }
  function panel(view,platform,title,buttons) {
    const section=document.createElement('section'); section.id='view-'+view; section.className='view hide';
    section.innerHTML=`<div class="page-heading"><h1>${title}</h1><p>按所选配置和线路操作；未授权的查询会明确显示权限错误。</p></div>
      <div class="card"><div class="bar"><label>当前账号</label><select data-account></select><span data-route></span><button class="ghost" data-reload>刷新账号</button><button class="ghost" data-toggle>停用配置</button></div><p data-identity class="muted"></p>
      <div class="bar"><label>查询范围</label><select data-days><option value="1">近 1 天</option><option value="7">近 7 天</option><option value="30">近 30 天</option><option value="90" selected>近 90 天</option></select></div><div class="tools-grid">${buttons.map(([op,label])=>`<button data-query="${op}">${label}</button>`).join('')}</div></div>
      <div class="card" data-forms></div><div class="card tool-result" data-result>选择账号后读取。</div>`;
    $('#app').append(section);
    let accounts=[],epoch=0,busy=false;
    const select=$('[data-account]',section),result=$('[data-result]',section),forms=$('[data-forms]',section);
    const current=()=>accounts.find(a=>String(a.id)===select.value);
    const capture=()=>{const a=current();if(!a) throw new Error('请先添加账号');return {...a,epoch};};
    const valid=a=>a.epoch===epoch && String(a.id)===select.value && a.revision===current()?.revision;
    const endpoint=(a,op)=>`/api/tools/${platform}/${a.id}/${op}`;
    const read=(a,op,extra={})=>api(endpoint(a,op)+'?'+new URLSearchParams({revision:a.revision,days:$('[data-days]',section).value,...extra}));
    const write=(a,op,body={})=>api(endpoint(a,platform==='oci'&&op!=='automation'?'actions/'+op:op),{method:'POST',body:{revision:a.revision,confirm:true,...body}});
    function choose() {
      epoch++;forms.replaceChildren();result.textContent='选择工具读取当前配置。';
      const a=current(); $('[data-route]',section).textContent=a?.route || (a?.params?.proxy_url?'代理':'直连');
      $('[data-identity]',section).textContent=a?`${a.name} · ${a.region || a.project_id} · ${a.enabled?'已启用':'已停用'}`:'尚未配置账号';
      $('[data-toggle]',section).textContent=a?.enabled?'停用配置':'启用配置';
    }
    async function load(preferred=select.value) {
      const started=epoch;
      const rows=await api('/api/tools/accounts/'+platform);
      if (started!==epoch) return;
      accounts=rows;select.innerHTML=rows.map(a=>`<option value="${a.id}">${esc(a.name)} · ${esc(a.region||a.project_id)}${a.enabled?'':'（已停用）'}</option>`).join('')||'<option value="">请添加账号</option>';
      if (rows.some(a=>String(a.id)===preferred))select.value=preferred;
      choose();
    }
    async function run(fn) {
      if(busy){toast('请等待当前操作完成',false);return;}
      let a;
      try {a=capture();busy=true;await fn(a);}
      catch(e){if(!a||valid(a)){result.textContent=e.message;toast(e.message,false);}}
      finally{busy=false;}
    }
    select.onchange=choose;$('[data-reload]',section).onclick=()=>run(()=>load());
    $('[data-toggle]',section).onclick=()=>run(async a=>{
      if(!confirm(`${a.enabled?'停用':'启用'}本地配置 ${a.name}？不会删除云资源，也不会修改 SSH 会话。`))return;
      await api(`/api/tools/accounts/${platform}/${a.id}/enabled`,{method:'POST',body:{revision:a.revision,enabled:!a.enabled}});
      if(valid(a))await load(String(a.id));
    });
    section.addEventListener('click',e=>{
      const b=e.target.closest('[data-query]');if(!b)return;
      run(async a=>{let extra={};if(b.dataset.query==='work-errors'){const resource=prompt('请输入当前 compartment 的工作请求 OCID');if(!resource)return;extra={resource};}
        if(b.dataset.query==='quotas'&&platform==='aws')extra={resource:'ec2'};
        const data=await read(a,b.dataset.query,extra);if(valid(a))renderResult(result,data);});
    });
    VIEW_LOADERS[view]=()=>load();
    return {section,forms,result,select,current,capture,valid,read,write,run,load,platform};
  }
  const oracle=panel('oci-tools','oci','Oracle 账号与自动化工具',[
    ['traffic','流量统计'],['costs','费用查询'],['quotas','计算配额'],['regions','区域订阅'],
    ['subscription','订阅信息'],['users','用户邮箱 / 2FA'],['work-requests','工作请求'],['work-errors','工作请求错误'],['tasks','开机任务（含暂停任务）'],['current-key','当前 API 密钥信息'],['automation','自动化状态']]);
  const bulkEmail=document.createElement('button');bulkEmail.textContent='批量查询各 OCI 配置的用户邮箱';bulkEmail.dataset.bulkEmail='';$('[data-query="users"]',oracle.section).after(bulkEmail);
  bulkEmail.onclick=()=>oracle.run(async a=>{
    const profiles=await api('/api/tools/accounts/oci');if(!oracle.valid(a))return;
    const results=[];
    for(const p of profiles.filter(x=>x.enabled)){
      if(!oracle.valid(a))return;
      try{const users=await api(`/api/tools/oci/${p.id}/users?`+new URLSearchParams({revision:p.revision}));results.push(...users.map(u=>({profile:p.name,region:p.region,name:u.name,email:u.email,mfa:u.mfa})));}
      catch(e){results.push({profile:p.name,region:p.region,error:e.message});}
    }
    if(oracle.valid(a))renderResult(oracle.result,results);
  });
  const aws=panel('aws-tools','aws','AWS 费用、配额与状态',[
    ['costs','费用查询（结算账号全部区域）'],['quotas','EC2 配额'],['identity','身份 / 凭据状态'],['operations','Lightsail 运行任务']]);
  const lightsailQuota=document.createElement('button');lightsailQuota.textContent='Lightsail 配额';$('[data-forms]',aws.section).append(lightsailQuota);
  lightsailQuota.onclick=()=>aws.run(async a=>{const r=await aws.read(a,'quotas',{resource:'lightsail'});if(aws.valid(a))renderResult(aws.result,r);});
  // choose() resets forms, so mount provider-specific controls after every account change/load.
  const gcp=panel('gcp-tools','gcp','GCP 项目概览与状态',[
    ['overview','项目概览 / 配额 / 结算状态'],['traffic','网卡流量'],['operations','云端任务']]);
  function mountOracle() {
    oracle.forms.innerHTML=`<h3>操作工具</h3><div class="bar"><button data-open-port>开放云端 TCP 端口</button><button data-subscribe>订阅新区域</button><button data-clear-mfa>清除所选用户 2FA</button><button data-delete-key class="danger">删除当前 API 密钥</button><button data-config-auto>设置监控 / 每日报告 / 自动换 IP</button><button data-shortcut>快捷开机</button></div><p class="muted">网络规则作用于共享子网；删除密钥、清除 2FA 等操作需确认。仅添加功能，不会自动执行。</p>`;
    $('[data-shortcut]',oracle.forms).onclick=()=>switchView('launch');
    $('[data-open-port]',oracle.forms).onclick=()=>oracle.run(async a=>{
      const r=await api(`/api/cloud/oci/instances?account_id=${a.id}`);if(!oracle.valid(a))return;
      const form=document.createElement('form');form.innerHTML=`<h4>追加子网 TCP 入站规则</h4><div class="form-grid"><div><label>实例</label><select name="instance_id">${r.data.map(i=>`<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('')}</select></div><div><label>来源 CIDR</label><input name="source" required placeholder="你的 IPv4/32 或 IPv6/128"></div><div><label>端口</label><input name="port" type="number" min="1" max="65535" value="22" required></div></div><p>同一安全列表的其他实例也会受影响。系统防火墙需另外配置。</p><button>添加规则</button>`;oracle.forms.append(form);
      form.onsubmit=e=>{e.preventDefault();oracle.run(async()=>{if(!oracle.valid(a))throw new Error('账号已变化，请重新打开表单');const d=Object.fromEntries(new FormData(form));if(!confirm(`允许 ${d.source} 访问子网 TCP ${d.port}？`))return;const r=await oracle.write(a,'open-port',{...d,port:Number(d.port)});if(oracle.valid(a))renderResult(oracle.result,r);});};
    });
    $('[data-subscribe]',oracle.forms).onclick=()=>oracle.run(async a=>{
      const r=await oracle.read(a,'regions');if(!oracle.valid(a))return;
      const region=prompt('请输入要订阅的区域名称：\n'+r.available.map(x=>x.name).join(', '));if(!region)return;
      if(!confirm(`为 ${a.name} 订阅 ${region}？`))return;const data=await oracle.write(a,'subscribe-region',{region});if(oracle.valid(a))renderResult(oracle.result,data);
    });
    $('[data-clear-mfa]',oracle.forms).onclick=()=>oracle.run(async a=>{
      const users=await oracle.read(a,'users');if(!oracle.valid(a))return;
      const form=document.createElement('form');form.innerHTML='<h4>选择要清除 2FA 的用户</h4>'+users.map(u=>`<label class="chk"><input name="user_ids" type="checkbox" value="${esc(u.id)}">${esc(u.name)} · ${esc(u.email)} · ${u.mfa?'已开启':'未开启'}</label>`).join('')+'<button class="danger">清除所选用户 2FA</button>';oracle.forms.append(form);
      form.onsubmit=e=>{e.preventDefault();oracle.run(async()=>{if(!oracle.valid(a))throw new Error('账号已变化');const ids=new FormData(form).getAll('user_ids');if(!ids.length||!confirm(`清除所选 ${ids.length} 个用户的 2FA？会降低这些用户的登录保护。`))return;const r=await oracle.write(a,'clear-mfa',{user_ids:ids});if(oracle.valid(a))renderResult(oracle.result,r);});};
    });
    $('[data-delete-key]',oracle.forms).onclick=()=>oracle.run(async a=>{
      const key=await oracle.read(a,'current-key');if(!oracle.valid(a))return;
      if(prompt(`删除当前密钥后此配置将无法连接 OCI。请输入指纹确认：\n${key.fingerprint}`)!==key.fingerprint)return;
      const r=await oracle.write(a,'delete-current-key');if(oracle.valid(a)){renderResult(oracle.result,r);await oracle.load(String(a.id));}
    });
    $('[data-config-auto]',oracle.forms).onclick=()=>oracle.run(async a=>{
      const [cfg,cloud,ssh]=await Promise.all([oracle.read(a,'automation'),api(`/api/cloud/oci/instances?account_id=${a.id}`),api('/api/ssh/sessions')]);if(!oracle.valid(a))return;
      const form=document.createElement('form');form.innerHTML=`<h4>自动化设置</h4><label class="chk"><input name="notify_changes" type="checkbox" ${cfg.notify_changes?'checked':''}>实例状态变化通知</label><label class="chk"><input name="daily_report" type="checkbox" ${cfg.daily_report?'checked':''}>每日报告（北京时间 09:00 后）</label><label class="chk"><input name="auto_ip" type="checkbox" ${cfg.auto_ip?'checked':''}>连续 3 次目标 SSH 连接超时后更换临时 IPv4</label><label class="chk"><input name="expand_regions" type="checkbox" ${cfg.expand_regions?'checked':''}>自动为已订阅区域创建独立 profile（复制本账号线路，不修改已有配置）</label><p>通知发送到设置中已配置的渠道。换 IP 后监控会暂停，请核对 SSH / 域名地址后重新启用；冷却时间 1 小时。SSH 使用代理时无法明确区分代理故障，自动换 IP 不启用。不会自动重写 SSH 地址、认证或代理。</p><label>实例</label><select name="instance_id">${cloud.data.map(i=>`<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('')}</select><label>已关联会话</label><select name="session_id"><option value="">请选择</option>${ssh.data.filter(s=>s.oci_account_id===a.id).map(s=>`<option value="${s.id}">${esc(s.name)} · ${esc(s.host)}</option>`).join('')}</select><button>保存自动化设置</button>`;oracle.forms.append(form);
      if(cfg.instance_id)form.elements.instance_id.value=cfg.instance_id;if(cfg.session_id)form.elements.session_id.value=String(cfg.session_id);
      form.onsubmit=e=>{e.preventDefault();oracle.run(async()=>{if(!oracle.valid(a))throw new Error('账号已变化');if(!confirm('保存并应用当前自动化选项？'))return;const body={notify_changes:form.elements.notify_changes.checked,daily_report:form.elements.daily_report.checked,auto_ip:form.elements.auto_ip.checked,expand_regions:form.elements.expand_regions.checked,instance_id:form.elements.instance_id.value,session_id:Number(form.elements.session_id.value)||null};const r=await oracle.write(a,'automation',body);if(oracle.valid(a))renderResult(oracle.result,r);});};
    });
  }
  const ec2=panel('ec2','aws','AWS EC2 实例管理',[]);
  function mountEc2() {
    ec2.forms.innerHTML='<div class="bar"><button data-ec2-refresh>刷新 EC2</button><button data-ec2-create>创建实例</button><button data-ec2-sync>同步到 SSH</button><button data-aws-account class="ghost">管理 AWS 配置</button></div><p class="muted">EC2 与 Lightsail 独立列出。创建与运行可能产生费用，不自动创建 VPC、安全组或授予实例 IAM 权限。</p>';
    $('[data-aws-account]',ec2.forms).onclick=()=>switchView('aws');
    $('[data-ec2-refresh]',ec2.forms).onclick=()=>ec2.run(async a=>{
      const rows=await ec2.read(a,'ec2-instances');if(!ec2.valid(a))return;
      ec2.result.innerHTML=`<div class="instance-grid">${rows.map(i=>`<article class="card"><h3>${esc(i.name)}</h3><p>${esc(i.id)} · ${esc(i.status)}</p><p>${esc(i.zone)} · ${esc(i.machine_type)}</p><p>IPv4 ${esc(i.public_ip||'无')}<br>IPv6 ${esc(i.ipv6.join(', ')||'无')}</p><div class="bar">${[['start','开机'],['stop','关机'],['reboot','重启'],['delete','终止']].map(([op,label])=>`<button data-ec2-action="${op}" data-id="${esc(i.id)}">${label}</button>`).join('')}<button data-ec2-metrics="${esc(i.id)}">CPU / 流量</button></div></article>`).join('')||'当前区域没有 EC2 实例'}</div>`;
      ec2.result.onclick=e=>{
        const b=e.target.closest('[data-ec2-action],[data-ec2-metrics]');if(!b)return;
        ec2.run(async()=>{if(!ec2.valid(a))throw new Error('账号已切换，请刷新实例');if(b.dataset.ec2Metrics){const r=await ec2.read(a,'ec2-metrics',{resource:b.dataset.ec2Metrics});if(ec2.valid(a))renderResult(ec2.result,r);return;}
          const action=b.dataset.ec2Action,id=b.dataset.id;
          if(action==='delete'){if(prompt(`终止 ${id} 可能按自动删除设置删除磁盘。请输入实例编号确认：`)!==id)return;}else if(!confirm(`对 ${id} 执行${b.textContent}？`))return;
          const r=await ec2.write(a,'ec2-action',{instance_id:id,action});if(ec2.valid(a))renderResult(ec2.result,r);
        });
      };
    });
    $('[data-ec2-create]',ec2.forms).onclick=()=>ec2.run(async a=>{
      const opts=await ec2.read(a,'ec2-options');if(!ec2.valid(a))return;
      const clientToken=requestToken();
      const form=document.createElement('form');form.innerHTML=`<h4>创建 1 台 EC2</h4><div class="form-grid"><div><label>名称</label><input name="name" required maxlength="100"></div><div><label>当前区域 AMI 编号</label><input name="image" required placeholder="ami-..." pattern="ami-[a-f0-9]{8,32}"></div><div><label>实例规格</label><input name="instance_type" required placeholder="例如 t3.micro"></div><div><label>子网</label><select name="subnet" required>${opts.subnets.map(s=>`<option value="${esc(s.id)}">${esc(s.id)} · ${esc(s.zone)} · ${esc(s.vpc)}</option>`).join('')}</select></div><div><label>安全组（可多选）</label><select name="groups" required multiple>${opts.groups.map(g=>`<option value="${esc(g.id)}">${esc(g.name)} · ${esc(g.vpc)}</option>`).join('')}</select></div><div><label>SSH 密钥对</label><select name="key_name" required>${opts.keys.map(k=>`<option>${esc(k)}</option>`).join('')}</select></div></div><label class="chk"><input name="public_ipv4" type="checkbox">分配公网 IPv4（可能收费）</label><p>使用镜像原有磁盘配置，终止时可能删除系统盘；请核对 AMI 来源、费用和安全组规则。</p><button>确认创建</button>`;ec2.forms.append(form);
      form.onsubmit=e=>{e.preventDefault();ec2.run(async()=>{if(!ec2.valid(a))throw new Error('账号已变化');if(!confirm('创建 EC2 会产生实例、磁盘及所选公网 IPv4 的相应费用，确认提交？'))return;const d=new FormData(form);const r=await ec2.write(a,'ec2-create',{...Object.fromEntries(d),groups:d.getAll('groups'),client_token:clientToken,public_ipv4:form.elements.public_ipv4.checked});if(ec2.valid(a))renderResult(ec2.result,r);});};
    });
    $('[data-ec2-sync]',ec2.forms).onclick=()=>ec2.run(async a=>{
      const form=document.createElement('form');form.innerHTML='<h4>同步 EC2 到 SSH</h4><label>镜像的 SSH 用户名</label><input name="username" value="ubuntu" required><label>新会话自己的 SOCKS5 代理（留空直连）</label><input name="ssh_proxy" placeholder="socks5h://地址:端口"><p>仅同步公网地址；已有会话保留地址、认证和线路，新会话需要填写私钥。</p><button>同步</button>';ec2.forms.append(form);
      form.onsubmit=e=>{e.preventDefault();ec2.run(async()=>{if(!ec2.valid(a))throw new Error('账号已变化');const r=await ec2.write(a,'ec2-sync',{...Object.fromEntries(new FormData(form)),allow_private:false});if(ec2.valid(a))renderResult(ec2.result,r);window.loadSessions?.();});};
    });
  }
  for (const [p,mount] of [[oracle,mountOracle],[ec2,mountEc2],[aws,()=>{
    aws.forms.innerHTML='<button data-lightsail-quota>Lightsail 配额</button><p>身份验证成功不代表结算账号未受限制。费用包含凭据可见的全部区域；具体结算状态请在 AWS 控制台核对。 Cost Explorer 需具备权限并启用，查询可能按请求收费。</p>';
    $('[data-lightsail-quota]',aws.forms).onclick=()=>aws.run(async a=>{const r=await aws.read(a,'quotas',{resource:'lightsail'});if(aws.valid(a))renderResult(aws.result,r);});
  }]]) {
    const original=VIEW_LOADERS[p.section.id.slice(5)];
    VIEW_LOADERS[p.section.id.slice(5)]=async()=>{await original();mount();};
    p.select.addEventListener('change',mount);
    // Refresh/toggle also reset forms; restore controls after that DOM change.
    new MutationObserver(()=>{if(!p.forms.children.length)mount();}).observe(p.forms,{childList:true});
  }
})();
