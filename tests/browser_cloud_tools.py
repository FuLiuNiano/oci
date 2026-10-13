"""Account-switch and route guards for the newly added provider tools."""


def check_cloud_tools(page):
    page.evaluate("""async () => {
      const original=api,originalConfirm=window.confirm,originalSessions=state.sessions;
      const tick=()=>new Promise(r=>setTimeout(r,0));
      const assert=(ok,message)=>{if(!ok)throw new Error(message);};
      const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};};
      let pending=deferred(),writes=[];
      const accounts=[{id:901,name:'A',region:'ap-northeast-1',revision:'rev-A',route:'直连',enabled:true},
                      {id:902,name:'B',region:'us-west-2',revision:'rev-B',route:'代理',enabled:true}];
      try {
        api=async (path,opts={})=>{
          if(opts.method==='POST'){writes.push({path,body:opts.body});return {message:'submitted'};}
          if(path.startsWith('/api/tools/accounts/'))return accounts;
          if(path.includes('ec2-options'))return {subnets:[{id:'subnet-12345678',zone:'zone-A',vpc:'vpc-A'}],groups:[{id:'sg-12345678',name:'group-A',vpc:'vpc-A'}],keys:['key-A']};
          if(path.includes('/901/ec2-instances'))return pending.promise;
          if(path.includes('/902/ec2-instances'))return [{id:'i-bbbbbbbb',name:'B VPS',status:'running',zone:'us-west-2a',machine_type:'t3.micro',public_ip:'203.0.113.2',ipv6:[]}];
          return {data:[]};
        };
        window.confirm=()=>true;
        assert(document.querySelectorAll('#cloud-nav details[data-nav-group]').length===3,'provider navigation groups missing');
        switchView('ec2');await tick();
        const section=document.querySelector('#view-ec2'),select=section.querySelector('[data-account]');
        assert(select.value==='901','EC2 did not select AWS account');
        section.querySelector('[data-ec2-refresh]').click();await tick();
        select.value='902';select.dispatchEvent(new Event('change'));
        pending.resolve([{id:'i-aaaaaaaa',name:'A VPS',status:'running',zone:'ap-northeast-1a',machine_type:'t3.micro',public_ip:'203.0.113.1',ipv6:[]}]);await tick();
        assert(!section.querySelector('[data-result]').textContent.includes('A VPS'),'stale EC2 instance leaked into B');
        section.querySelector('[data-ec2-refresh]').click();await tick();
        assert(section.querySelector('[data-result]').textContent.includes('B VPS'),'B instances not displayed');
        select.value='901';select.dispatchEvent(new Event('change'));
        section.querySelector('[data-ec2-create]').click();await tick();
        const form=section.querySelector('form');
        form.elements.name.value='new-A';form.elements.image.value='ami-12345678';form.elements.instance_type.value='t3.micro';form.elements.groups.options[0].selected=true;
        form.requestSubmit();await tick();
        assert(writes.at(-1).path==='/api/tools/aws/901/ec2-create' && writes.at(-1).body.revision==='rev-A','EC2 creation used wrong account revision');
        assert(writes.at(-1).body.groups[0]==='sg-12345678' && !writes.at(-1).body.public_ipv4,'EC2 network input changed');
        select.value='902';select.dispatchEvent(new Event('change'));
        form.requestSubmit();await tick();
        assert(writes.length===1,'old EC2 form submitted a mutation after switching accounts');
        state.sessions=[{id:1,name:'Tokyo direct',host:'203.0.113.1',port:22,username:'root',auth_type:'password',tags:'',proxy_command:'',region:'日本东京',metadata:{}},
                        {id:2,name:'Phoenix proxy',host:'203.0.113.2',port:22,username:'root',auth_type:'password',tags:'',proxy_command:'socks5h://proxy:1080',region:'美西凤凰城',metadata:{}}];
        switchView('ssh');document.querySelector('#session-search').value='';document.querySelector('#session-search').dispatchEvent(new Event('input'));
        document.querySelector('[data-ssh-region="日本东京"]').click();
        assert(document.querySelector('#ssh-session-list').textContent.includes('Tokyo direct')&&!document.querySelector('#ssh-session-list').textContent.includes('Phoenix proxy'),'region did not filter SSH cards');
        document.querySelector('[data-ssh-region="美西凤凰城"]').click();
        assert(document.querySelector('#ssh-session-list').textContent.includes('Phoenix proxy')&&!document.querySelector('#ssh-session-list').textContent.includes('Tokyo direct'),'second region did not filter SSH cards');
        assert(state.sessions[0].host==='203.0.113.1'&&state.sessions[1].proxy_command==='socks5h://proxy:1080','filter changed SSH transport');
        state.sessions=[
          {id:11,name:'Legacy Tokyo',host:'203.0.113.11',tags:'oci:东京',metadata:{},proxy_command:''},
          {id:12,name:'Linked Tokyo',host:'203.0.113.12',oci_region:'ap-tokyo-1',metadata:{cloud_provider:'oci',cloud_account_id:2},proxy_command:'socks5h://proxy:1080'}
        ];
        document.querySelector('#session-search').dispatchEvent(new Event('input'));
        const tokyo=document.querySelector('[data-ssh-region="ap-tokyo-1"]');
        assert(tokyo && tokyo.textContent.includes('(2)'),'legacy and linked Tokyo not grouped together');
        assert(!document.querySelector('[data-ssh-region="东京"]'),'duplicate Tokyo group');
        tokyo.click();
        assert(document.querySelector('#ssh-session-list').textContent.includes('Legacy Tokyo')&&document.querySelector('#ssh-session-list').textContent.includes('Linked Tokyo'),'merged group missing sessions');
        assert(state.sessions[1].metadata.cloud_account_id===2&&state.sessions[1].proxy_command==='socks5h://proxy:1080','region normalization mutated account or route');
      } finally {
        api=original;window.confirm=originalConfirm;state.sessions=originalSessions;
        switchView('ssh');document.querySelector('#session-search').value='';document.querySelector('#session-search').dispatchEvent(new Event('input'));
        await window.loadSessions();
      }
    }""")

    page.evaluate("""async () => {
      const original=api,select=document.querySelector('#dashboard-account');
      const html=select.innerHTML,value=select.value,requests=[];
      const tick=()=>new Promise(r=>setTimeout(r,0));
      let release;
      const old=new Promise(r=>release=r);
      try {
        api=async path=>{
          requests.push(path);
          if(path==='/api/accounts/901/usage')return old;
          if(path.endsWith('/usage'))return {currencies:{USD:222}};
          if(path.endsWith('/traffic'))return {total_gb:2,per_resource:{}};
          if(path.endsWith('/regions'))return {data:[]};
          if(path.endsWith('/stats'))return {limits:[]};
          return {};
        };
        select.innerHTML='<option value="901">A</option><option value="902">B</option>';
        select.value='901';select.dispatchEvent(new Event('change'));await tick();
        select.value='902';select.dispatchEvent(new Event('change'));await tick();
        for(const kind of ['usage','traffic','regions','stats']) {
          if(!requests.includes('/api/accounts/902/'+kind))throw new Error('account switch did not auto-load '+kind);
        }
        release({currencies:{USD:111}});await tick();
        const box=document.querySelector('#summary-usage').textContent;
        if(!box.includes('222.00')||box.includes('111.00'))throw new Error('stale dashboard account response displayed');
        if(document.querySelector('[data-cloud-metric="usage"]').disabled)throw new Error('new account button stuck disabled');
      } finally {
        api=original;select.innerHTML=html;select.value=value;
        select.dispatchEvent(new Event('change'));await tick();
      }
    }""")

    page.evaluate("""async () => {
      const a={id:1001,region:'ap-tokyo-1',cache_revision:'private-configuration-A'};
      const b={id:1002,region:'ap-tokyo-1',cache_revision:'private-configuration-B'};
      const epoch=cloudSnapshots.epoch;
      cloudSnapshots.write('oci',a,'usage',{currencies:{USD:42}},epoch);
      if(cloudSnapshots.read('oci',b,'usage'))throw new Error('snapshot crossed accounts');
      if(cloudSnapshots.read('oci',{...a,cache_revision:'changed-proxy'},'usage'))throw new Error('snapshot survived route revision');
      if(cloudSnapshots.read('aws',a,'usage'))throw new Error('snapshot crossed providers');
      if(cloudSnapshots.read('oci',{...a,region:'us-phoenix-1'},'usage'))throw new Error('snapshot crossed regions');
      const original=api,select=document.querySelector('#dashboard-account');
      const html=select.innerHTML,value=select.value,accounts=state.accounts;
      let release,queries=0;
      try {
        state.accounts=[a]; select.innerHTML='<option value="1001">A</option>';
        api=path=>{queries++;return path.endsWith('/usage')?new Promise(r=>release=r):Promise.resolve(path.endsWith('/traffic')?{total_gb:0,per_resource:{}}:path.endsWith('/regions')?{data:[]}:{limits:[]});};
        select.dispatchEvent(new Event('change'));
        const box=document.querySelector('#summary-usage');
        if(!box.textContent.includes('42.00')||!box.textContent.includes('缓存'))throw new Error('cache not visible before cloud reply');
        if(release)throw new Error('cached usage automatically queried cloud');
        document.querySelector('[data-cloud-metric="usage"]').click();
        if(!release)throw new Error('manual refresh did not query cloud');
        release({currencies:{USD:43}}); await new Promise(r=>setTimeout(r,0));
        if(!box.textContent.includes('43.00')||box.textContent.includes('缓存'))throw new Error('fresh data did not replace snapshot');
        release=null;select.dispatchEvent(new Event('change'));
        if(release)throw new Error('reopening cached metric queried again');
        cloudSnapshots.clear();
        cloudSnapshots.write('oci',a,'usage',{currencies:{USD:44}},epoch);
        if(cloudSnapshots.read('oci',a,'usage'))throw new Error('logged-out request restored cache');
      } finally {
        api=original;state.accounts=accounts;select.innerHTML=html;select.value=value;
        select.dispatchEvent(new Event('change'));
      }
    }""")
