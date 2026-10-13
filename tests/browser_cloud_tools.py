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
      } finally {
        api=original;window.confirm=originalConfirm;state.sessions=originalSessions;
        switchView('ssh');document.querySelector('#session-search').value='';document.querySelector('#session-search').dispatchEvent(new Event('input'));
        await window.loadSessions();
      }
    }""")
