// Execute production context-switch, ASR and dispatch code with controlled I/O.
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
  extract('    async function releaseVoiceRecordingResources(', '    async function startVoiceRecording('),
  extract('    async function stopVoiceRecording(', '    async function toggleVoiceRecording('),
  extract('    function renderDispatchStatus(', '    /**'),
].join('\n');
async function settle() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
function harness() {
  const context = vm.createContext({AbortController, console, Float32Array});
  vm.runInContext(`
    let sessionId='active', sessionGeneration=0, isSending=false, isDone=false;
    let currentRequestSeq=0, currentAbortController=null, asrAbortController=null;
    let currentReadOnly=false, currentActions={can_send:true}, isRecording=true;
    let recordedChunks=[new Float32Array([0.5])], recordingSampleRate=16000;
    let audioContext={state:'running',async close(){closed++;}}, closed=0, stopped=0;
    let mediaStream={getTracks(){return [{stop(){stopped++;}}];}};
    let recorderProcessor={disconnect(){}}, recorderSource={disconnect(){}};
    let lastResponseData=null;
    const API_BASE='', currentLang='zh', window={};
    const I18N={zh:{}}, requests=[], sends=[], messages=[], statuses=[], updates=[];
    const elements=new Map();
    const document={getElementById(id){
      if(!elements.has(id))elements.set(id,{style:{},innerText:'',textContent:'',hidden:false});
      return elements.get(id);
    }};
    const voiceBtn={classList:{remove(){}},disabled:false};
    const messageInput={value:'',focus(){}};
    const messageContainer={innerHTML:''};
    const localStorage={setItem(){}};
    function alert(){}
    function applyInteractionState(){}
    function addMessage(role,text){messages.push({role,text});}
    function updateSidebar(data){lastResponseData=data;updates.push(data);renderDispatchStatus(data);}
    function mergeFloat32Chunks(){return new Float32Array([0.5]);}
    function encodeWav(){return {};}
    function renderAsrNormalization(){return 'recognized';}
    function setAsrStatus(text){statuses.push(text);}
    function escapeHtml(text){return text;}
    async function sendMessage(text,options){sends.push({text,options,sessionId});}
    function fetch(url,options={}){
      // Deliberately allow late responses after abort to exercise generation guards.
      return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
    }
    function uploadAudioForAsr(blob,signal){return fetch('/api/asr',{signal});}
    ${actualFunctions}
    globalThis.client={stopVoiceRecording,restoreHistory,retryDispatch,requests,sends,messages,updates,
      state(){return {sessionId,sessionGeneration,input:messageInput.value,closed,stopped,isRecording};},
      input(value){messageInput.value=value;},
      render(data){updateSidebar(data);},element(id){return document.getElementById(id);}};
  `,context);
  return context.client;
}

async function historyInvalidatesPendingAudio(autoSend) {
  const client=harness();
  const asr=client.stopVoiceRecording();
  await settle();
  assert.equal(client.requests[0].url,'/api/asr');
  const history=client.restoreHistory('other-history');
  await settle();
  assert.equal(client.requests[0].options.signal.aborted,true,'history must abort old ASR');
  client.requests[1].resolve({async json(){return {code:200,session_id:'active',
    conversation_history:[{role:'assistant',content:'new task context'}],ui_state:{phase:'done',read_only:true}};}});
  await history;
  client.input('draft for restored task');
  client.requests[0].resolve({text:'old task voice command',corrected_text:'old task voice command',
    direct_to_llm:autoSend,normalization_changed:false,replacements:[],warnings:[]});
  await asr;
  assert.equal(client.state().sessionGeneration,1);
  assert.equal(client.state().input,'draft for restored task');
  assert.equal(client.sends.length,0,'stale ASR must never auto-send into restored context');
  assert.equal(client.state().stopped,1);
  assert.equal(client.state().closed,1);
}

async function historyStopsActiveRecording() {
  const client=harness();
  const history=client.restoreHistory('other-history');
  await settle();
  assert.equal(client.state().isRecording,false);
  assert.equal(client.state().stopped,1);
  assert.equal(client.state().closed,1);
  client.requests[0].resolve({async json(){return {code:200,session_id:'active',
    conversation_history:[],ui_state:{phase:'collecting'}};}});
  await history;
}

async function overlappingHistoryRestoresIgnoreOldResults() {
  const client=harness();
  const old=client.restoreHistory('old');
  await settle();
  const latest=client.restoreHistory('latest');
  await settle();
  assert.equal(client.requests[0].options.signal.aborted,true);
  client.requests[1].resolve({async json(){return {code:200,session_id:'active',
    conversation_history:[{role:'assistant',content:'latest'}],ui_state:{phase:'done'}};}});
  await latest;
  client.requests[0].resolve({async json(){return {code:200,session_id:'old',
    conversation_history:[{role:'assistant',content:'stale'}],ui_state:{phase:'collecting'}};}});
  await old;
  assert.equal(client.state().sessionId,'active');
  assert(!client.messages.some(message=>message.text==='stale'));
}

async function dispatchStateControlsPresentationAndExplicitRetry() {
  const client=harness();
  const done={ui_state:{phase:'done'}};
  const cases=[['FAILED','发送失败',true],['UNKNOWN','待核对',true],
    ['SCHEDULED','等待计划时间',true],['BLOCKED','未满足',true],['SENT','遥测确认',false]];
  for(const [state,text,retry] of cases){
    client.render({...done,ros2_dispatch:{state,retry_allowed:retry,error:'<text remains text>'}});
    assert(client.element('dispatchStatus').textContent.includes(text));
    assert.equal(client.element('dispatchRetryBtn').hidden,!retry);
    assert.equal(client.element('dispatchDetail').textContent,'<text remains text>');
  }
  await client.retryDispatch();
  assert.equal(client.requests.length,0,'SENT may not be retried');
  client.render(done);
  assert(client.element('dispatchStatus').textContent.includes('待核对'));
  assert.equal(client.element('dispatchRetryBtn').hidden,true,'legacy history has no proven retry permission');
  client.render({...done,ros2_dispatch:{state:'FAILED',retry_allowed:true}});
  assert.equal(client.requests.length,0,'rendering failure must not send a command');
  const retry=client.retryDispatch();
  assert.equal(client.requests.length,1);
  assert.equal(client.requests[0].url,'/api/mcp/dispatch');
  assert.deepEqual(JSON.parse(client.requests[0].options.body),{session_id:'active'});
  client.requests[0].resolve({ok:true,async json(){return {ros2_dispatch:{state:'SENT',retry_allowed:false}};}});
  await retry;
  assert(client.element('dispatchStatus').textContent.includes('遥测确认'));
  assert.equal(client.element('dispatchRetryBtn').hidden,true);

  client.render({...done,ros2_dispatch:{state:'FAILED',retry_allowed:true}});
  const interrupted=client.retryDispatch();
  client.requests[1].reject(new Error('connection lost'));
  await interrupted;
  assert.equal(client.requests.length,2,'network failures cannot replay dispatch');
  assert(client.element('dispatchStatus').textContent.includes('待核对'));
  assert.equal(client.element('dispatchRetryBtn').hidden,true);
}

(async()=>{
  await historyInvalidatesPendingAudio(false);
  await historyInvalidatesPendingAudio(true);
  await historyStopsActiveRecording();
  await overlappingHistoryRestoresIgnoreOldResults();
  await dispatchStateControlsPresentationAndExplicitRetry();
  console.log('History/ASR context and dispatch presentation regressions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
