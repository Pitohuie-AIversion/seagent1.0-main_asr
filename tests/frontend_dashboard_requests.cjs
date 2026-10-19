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
    const requests=[],renders=[],failures=[],alerts=[],nodes=new Map();
    const dashboardApi={status:'/status',gateway:'/gateway'};
    const document={getElementById(id){
      if(!nodes.has(id))nodes.set(id,{value:id==='gatewayPort'?'9090':'localhost',disabled:false,open:true,
        listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},close(){this.open=false;},showModal(){this.open=true;}});
      return nodes.get(id);
    }};
    const window={alert(message){alerts.push(message);}};
    function renderStatus(data){renders.push(data);}
    function showStatusFailure(error){failures.push(error.message || String(error));}
    function fetch(url,options={}){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}
    ${html.slice(first,last)}
    globalThis.client={refreshStatus,requests,renders,failures,alerts,node:id=>document.getElementById(id),
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
    assert.equal(client.failures.length,0,'stale errors must not mark current data unavailable');
  }
}
async function pollingFailurePreservesPreviousSnapshot(){
  const client=harness();
  const first=client.refreshStatus();
  client.requests[0].resolve({ok:true,async json(){return {mcp_connected:true,host:'known-gateway'};}});
  await first;
  const broken=client.refreshStatus();
  client.requests[1].reject(new Error('network unavailable'));
  await broken;
  assert.equal(client.renders.length,1,'failed requests must not render an empty replacement snapshot');
  assert.deepEqual(Array.from(client.failures),['network unavailable']);
  const recovered=client.refreshStatus();
  client.requests[2].resolve({ok:true,async json(){return {mcp_connected:true,host:'recovered-gateway'};}});
  await recovered;
  assert.equal(client.renders.at(-1).host,'recovered-gateway');
}
async function gatewayReadFailureDoesNotChangeTelemetry(){
  const client=harness();
  const opened=client.node('gatewayButton').listeners.click();
  client.requests[0].reject(new Error('settings unavailable'));
  await opened;
  assert.equal(client.renders.length,0);
  assert.equal(client.failures.length,0);
  assert(client.alerts[0].includes('settings unavailable'));
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
  await pollingFailurePreservesPreviousSnapshot();
  await gatewayReadFailureDoesNotChangeTelemetry();
  await gatewaySaveRecovers();
  console.log('Dashboard request recovery regressions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
