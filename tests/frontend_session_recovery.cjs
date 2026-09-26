// Exercise delayed startup/reset responses against a newer user-selected context.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/index.js', 'utf8');
function extract(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first);
  return source.slice(first, last);
}
const actualFunctions = [
  extract('    function cancelActiveRequest(', '    /**'),
  extract('    async function restoreHistory(', '    async function sendMessage('),
  extract('    async function restoreSessionFromStorage(', "    sendBtn.addEventListener('click'"),
].join('\n');
async function settle() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
function harness() {
  const context = vm.createContext({AbortController, console});
  vm.runInContext(`
    let sessionId='active', sessionGeneration=0, isSending=false, isDone=false;
    let currentRequestSeq=0, currentAbortController=null, currentReadOnly=false;
    let currentActions={can_send:true}, lastResponseData=null;
    const RESET_ACTIONS={can_send:true}, API_BASE='', currentLang='zh', window={};
    const I18N={zh:{resetFailed:'reset failed',none:'none'}}, requests=[], messages=[], updates=[];
    const elements=new Map();
    const document={getElementById(id){
      if(!elements.has(id))elements.set(id,{style:{},innerHTML:'',hidden:false});
      return elements.get(id);
    }};
    const messageInput={value:'',focus(){}}, sendBtn={};
    const messageContainer={innerHTML:''};
    let storedSession='saved';
    const localStorage={getItem(){return storedSession;},setItem(key,value){storedSession=value;},removeItem(){storedSession=null;}};
    function alert(text){messages.push({role:'alert',text});}
    function applyInteractionState(){}
    async function cancelVoiceActivity(){}
    function addMessage(role,text){messages.push({role,text});}
    function addWelcomeMessage(){addMessage('bot','welcome');}
    function removeReloadNotificationBubbles(){}
    function updateSidebar(data){lastResponseData=data;updates.push(data);}
    function fetch(url,options={}){
      // A response can already be queued when abort runs; guards must still work.
      return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
    }
    ${actualFunctions}
    globalThis.client={reset,restoreHistory,restoreSessionFromStorage,requests,messages,updates,
      state(){return {sessionId,storedSession,isSending};}};
  `,context);
  return context.client;
}
const restoredHistory = {code:200,session_id:'latest',conversation_history:[
  {role:'assistant',content:'latest history'}],ui_state:{phase:'collecting'}};

async function resetCannotEraseNewHistory() {
  for (const fail of [false,true]) {
    const client=harness();
    const reset=client.reset();
    await settle();
    const history=client.restoreHistory('latest');
    await settle();
    client.requests[1].resolve({async json(){return restoredHistory;}});
    await history;
    if(fail) client.requests[0].reject(new Error('late reset failure'));
    else client.requests[0].resolve({ok:true,async json(){return {ok:true,reset:true};}});
    await reset;
    assert.equal(client.state().sessionId,'latest','a stale reset must not erase restored history');
    assert.equal(client.state().storedSession,'latest');
    assert(!client.messages.some(message=>message.text==='reset failed'),'stale errors must not affect the new context');
  }
}

async function startupCannotOverwriteHistory() {
  const client=harness();
  const startup=client.restoreSessionFromStorage();
  assert.equal(client.state().isSending,true,'startup restoration must lock sends');
  const history=client.restoreHistory('latest');
  await settle();
  client.requests[1].resolve({async json(){return restoredHistory;}});
  await history;
  client.requests[0].resolve({ok:true,async json(){return {ok:true,exists:true,
    history:[{role:'assistant',content:'stale startup'}],ui_state:{phase:'done'}};}});
  assert.equal(await startup,true,'superseded initialization must not trigger reset');
  assert.equal(client.state().sessionId,'latest');
  assert(!client.messages.some(message=>message.text==='stale startup'));
}

async function failedStartupPreservesSession() {
  const client=harness();
  const startup=client.restoreSessionFromStorage();
  client.requests[0].reject(new Error('offline'));
  assert.equal(await startup,true,'transient failure must not invoke reset');
  assert.equal(client.state().storedSession,'saved');
  assert.equal(client.state().sessionId,'saved');
  assert.equal(client.state().isSending,false);
  assert(client.messages.some(message=>message.text.includes('会话恢复失败')));
}

(async()=>{
  await resetCannotEraseNewHistory();
  await startupCannotOverwriteHistory();
  await failedStartupPreservesSession();
  console.log('Session startup/reset recovery regressions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
