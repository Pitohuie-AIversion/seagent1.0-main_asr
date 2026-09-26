const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=fs.readFileSync('frontend/ros2_dashboard.html','utf8');
const first=html.indexOf('    async function refreshStatus()');
const last=html.indexOf('    refreshStatus();',first);
function harness(){
  const context=vm.createContext({console});
  vm.runInContext(`
    let statusRequestSeq=0,renderedStatusSeq=0,isSavingGateway=false;
    const requests=[],renders=[],alerts=[],nodes=new Map();
    const dashboardApi={status:'/status',gateway:'/gateway'};
    const document={getElementById(id){
      if(!nodes.has(id))nodes.set(id,{value:id==='gatewayPort'?'9090':'localhost',disabled:false,open:true,
        listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},close(){this.open=false;},showModal(){this.open=true;}});
      return nodes.get(id);
    }};
    const window={alert(message){alerts.push(message);}};
    function renderStatus(data){renders.push(data);}
    function fetch(url,options={}){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}
    ${html.slice(first,last)}
    globalThis.client={refreshStatus,requests,renders,alerts,node:id=>document.getElementById(id),
      submit(){return document.getElementById('gatewayForm').listeners.submit({preventDefault(){}});}};
  `,context);
  return context.client;
}
async function staleStatusCannotOverwrite(){
  for(const fail of [false,true]){
    const client=harness();
    const older=client.refreshStatus(),newer=client.refreshStatus();
    client.requests[1].resolve({ok:true,async json(){return {mcp_connected:true,host:'new'};}});
    await newer;
    if(fail)client.requests[0].reject(new Error('old disconnected result'));
    else client.requests[0].resolve({ok:true,async json(){return {mcp_connected:false,host:'old'};}});
    await older;
    assert.equal(client.renders.length,1,'stale poll responses must not overwrite current telemetry');
    assert.equal(client.renders[0].host,'new');
  }
}
async function slowPollingStillMakesProgress(){
  const client=harness();
  let pending=client.refreshStatus();
  for(let i=0;i<4;i++){
    const next=client.refreshStatus();
    client.requests[i].resolve({ok:true,async json(){return {mcp_connected:true,host:'gateway-'+i};}});
    await pending;
    assert.equal(client.renders.length,i+1,'a pending newer poll must not starve completed updates');
    assert.equal(client.renders.at(-1).host,'gateway-'+i);
    pending=next;
  }
  client.requests[4].resolve({ok:true,async json(){return {mcp_connected:true,host:'latest'};}});
  await pending;
  assert.equal(client.renders.at(-1).host,'latest');
}
async function gatewaySaveRecovers(){
  const client=harness();
  const save=client.submit();
  // Attach rejection handling immediately so an unhandled event error fails deterministically.
  const result=Promise.resolve(save).then(()=>null,error=>error);
  assert.equal(client.node('saveGatewayButton').disabled,true);
  await client.submit();
  assert.equal(client.requests.length,1,'double submit must not switch the gateway twice');
  client.requests[0].reject(new Error('connection interrupted'));
  assert.equal(await result,null,'gateway save must handle network rejection');
  assert.equal(client.node('saveGatewayButton').disabled,false);
  assert.equal(client.node('gatewayDialog').open,true,'failed save must preserve form for retry');
  assert(client.alerts[0].includes('connection interrupted'));
}
(async()=>{
  await staleStatusCannotOverwrite();
  await slowPollingStillMakesProgress();
  await gatewaySaveRecovers();
  console.log('Dashboard request recovery regressions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
