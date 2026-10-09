"""Exercise account switches while previous OCI requests are still pending."""


def check_oci_account_switching(page):
    page.evaluate("""async () => {
      const original = api;
      const select = (id, value) => { document.querySelector(id).value=value; document.querySelector(id).dispatchEvent(new Event('change')); };
      const tick = () => new Promise(resolve => setTimeout(resolve, 0));
      const assert = (ok, text) => { if (!ok) throw new Error(text); };
      const deferred = () => { let resolve; const promise=new Promise(r=>resolve=r); return {promise,resolve}; };
      let mode='', pending, writes=[];
      const volume = name => ({name,id:name,size_gbs:50,vpus:10,state:'AVAILABLE'});
      try {
        state.accounts=[{id:901,name:'account-A',platform:'oci',params:{}},{id:902,name:'account-B',platform:'oci',params:{}}];
        for (const id of ['inst','l','a1','vol','net','usr','os']) document.querySelector('#'+id+'-account').innerHTML='<option value="901">A</option><option value="902">B</option>';
        api = async (path, opts={}) => {
          if (opts.method && opts.method !== 'GET') writes.push({path,body:opts.body});
          if (mode==='volume' && path.includes('/boot-volumes')) return path.includes('901') ? pending.promise : {data:[volume('disk-B')]};
          if (mode==='users' && path.startsWith('/api/oci/users?')) return pending.promise;
          if (mode==='launch' && path.startsWith('/api/oc-info?')) return path.includes('901') ? pending.promise : {ads:['AD-B'],subnets:[{id:'subnet-B',name:'B',public:true}]};
          if (path.startsWith('/api/oci/buckets?')) return {data:[{name:'bucket-A',created:''}]};
          if (path.startsWith('/api/oci/objects?')) return {objects:[{name:'file-A',size:1,modified:''}],prefixes:[]};
          return {data:[]};
        };
        mode='volume'; pending=deferred();
        const oldVolumes=loadVolumes();
        select('#vol-account','902'); await tick();
        assert(document.querySelector('#vol-table').textContent.includes('disk-B'),'B volume was not displayed');
        pending.resolve({data:[volume('disk-A')]}); await oldVolumes;
        assert(!document.querySelector('#vol-table').textContent.includes('disk-A'),'A response replaced B volume');

        mode='users'; pending=deferred();
        document.querySelector('#btn-usr-load').click(); await tick();
        select('#usr-account','902');
        pending.resolve({data:[{id:'user-A',name:'user-A',email:'',mfa:false}]}); await tick();
        assert(!document.querySelector('#usr-table').textContent.includes('user-A'),'stale A user became actionable under B');

        mode='launch'; pending=deferred();
        const oldLaunch=fillLaunchMeta();
        select('#l-account','902'); await tick();
        pending.resolve({ads:['AD-A'],subnets:[{id:'subnet-A',name:'A',public:true}]}); await oldLaunch;
        assert(document.querySelector('#l-ad').textContent.includes('AD-B'),'late launch metadata changed account');
        assert(!document.querySelector('#l-subnet').textContent.includes('subnet-A'),'stale subnet remained');

        mode='objects'; document.querySelector('#btn-os-load').click(); await tick();
        document.querySelector('[data-bopen="bucket-A"]').click(); await tick();
        assert(!document.querySelector('#os-files').classList.contains('hide'),'bucket did not open');
        const file=new File(['payload'],'test.txt'); const transfer=new DataTransfer(); transfer.items.add(file);
        const reading=deferred(); file.arrayBuffer=()=>reading.promise;
        document.querySelector('#os-file').files=transfer.files;
        document.querySelector('#os-file').dispatchEvent(new Event('change')); await tick();
        select('#os-account','902'); reading.resolve(new Uint8Array([1,2,3]).buffer); await tick();
        assert(document.querySelector('#os-files').classList.contains('hide'),'old bucket panel remained after switch');
        assert(!document.querySelector('#os-table').textContent.includes('file-A'),'old file remained after switch');
        assert(writes.length===0,'account switch submitted a mutation using stale resources');
      } finally { api=original; await reloadAccounts(); }
    }""")
