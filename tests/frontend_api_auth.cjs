const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('frontend/js/api-auth.js','utf8');
function harness(pathname='/'){
  const nodes=new Map(), stored=new Map(), requests=[], events=[];
  const document={getElementById(id){
    if(!nodes.has(id))nodes.set(id,{hidden:false,open:false,value:'',listeners:{},
      addEventListener(name,fn){this.listeners[name]=fn;},focus(){},showModal(){this.open=true;},close(){this.open=false;}});
    return nodes.get(id);
  }};
  const window={location:{origin:'https://example.test',href:'https://example.test'+pathname,pathname},
    sessionStorage:{getItem:key=>stored.get(key),setItem:(key,value)=>stored.set(key,value),removeItem:key=>stored.delete(key)},
    dispatchEvent:event=>events.push(event.type),
    async fetch(url,options){requests.push({url,options});return {status:requests.length===1?401:200};},
  };
  vm.runInNewContext(source,{window,document,URL,Headers,Event});
  return {window,requests,nodes,stored,events,element:id=>document.getElementById(id)};
}
(async()=>{
  for(const prefix of ['', '/jupyter/proxy/8890']){
    const app=harness(prefix+'/');
    assert.equal(app.element('apiCredentialsButton').hidden,true,'default UI has no credential clutter');
    await app.window.SEAgentAuth.fetch(prefix+'/api/chat',{method:'POST',body:'first'});
    assert.equal(app.element('apiCredentialsDialog').open,true);
    app.element('apiTokenInput').value=' test-token ';
    app.element('apiCredentialsForm').listeners.submit({preventDefault(){}});
    assert.equal(app.requests.length,1,'saving credentials must not replay a mutation');
    assert.deepEqual(app.events,['seagent-auth-change']);
    assert.equal(app.element('apiCredentialsDialog').open,false);
    assert.equal(app.window.SEAgentAuth.hasToken(),true);
    await app.window.SEAgentAuth.fetch(prefix+'/api/asr',{method:'POST',headers:{'X-Test':'kept'}});
    assert.equal(app.requests[1].options.headers.get('Authorization'),'Bearer test-token');
    assert.equal(app.requests[1].options.headers.get('X-Test'),'kept');
    assert(!app.requests[1].url.includes('test-token'));
    await app.window.SEAgentAuth.fetch('https://other.test/api/chat');
    assert.equal(app.requests[2].options.headers.has('Authorization'),false);
    if(prefix){
      await app.window.SEAgentAuth.fetch('/jupyter/proxy/9999/api/chat');
      assert.equal(app.requests[3].options.headers.has('Authorization'),false);
    }
    app.element('apiCredentialsClear').listeners.click();
    assert.equal(app.window.SEAgentAuth.hasToken(),false);
    assert.equal(app.stored.size,0);
  }
  console.log('Built-in UI authentication regressions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
