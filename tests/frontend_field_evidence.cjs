// Exercise the production field/evidence presentation without guessing missing facts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/index.js', 'utf8');
function extract(first, last) {
  const begin = source.indexOf(first), end = source.indexOf(last, begin + first.length);
  assert(begin >= 0 && end > begin);
  return source.slice(begin, end);
}
function nodes(element) { return [element, ...element.children.flatMap(nodes)]; }
function text(element) { return element.textContent + element.children.map(text).join(' '); }
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.dataset={}; this.style={}; this.listeners={}; this.textContent=''; this.className=''; }
  get childElementCount() { return this.children.length; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children=children; this.textContent=''; }
  addEventListener(event, listener) { this.listeners[event]=listener; }
  click() { return this.listeners.click?.(); }
}
const context=vm.createContext({Element,nodes,AbortController});
vm.runInContext(`
  let currentLang='zh', sessionGeneration=0, isSending=false, currentReadOnly=false;
  let currentActions={}, lastResponseData=null;
  let sessionId='active', currentRequestSeq=0, currentAbortController=null;
  const API_BASE='', I18N={zh:{}}, requests=[], updates=[], elements=new Map();
  const container=new Element('div'), sends=[], messageInput={value:'',focus(){}};
  const messageContainer={innerHTML:''}, localStorage={setItem(){}};
  const window={async sendMessage(message){sends.push(message);isSending=true;refreshConstraintActions();}};
  const document={createElement:tag=>new Element(tag),
    getElementById(id){if(!elements.has(id))elements.set(id,new Element('div'));return elements.get(id);},
    querySelectorAll(){return nodes(container).filter(item=>item.dataset.constraintAction);}};
  function getFieldLabel(key){return key;}
  function parseAndFormatCoord(){return null;}
  function alert(){}
  function addMessage(){}
  async function cancelVoiceActivity(){}
  function applyInteractionState(actions,readOnly){currentActions=actions;currentReadOnly=!!readOnly;refreshConstraintActions();}
  function updateSidebar(data,options={}){
    lastResponseData=data;currentActions=data.ui_state.actions;currentReadOnly=!!data.ui_state.read_only;
    renderConstraintCards(container,data.ui_state,slot=>slot.label.zh);updates.push({data,options});
  }
  function fetch(url,options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}
  ${extract('    function appendFieldMetadata(', '    /**\n     * updateSidebar')}
  ${extract('      function formatUiStateValue(', '      function getSlotLabel(')}
  ${extract('    function cancelActiveRequest(', '    /**')}
  ${extract('    async function restoreHistory(', '    async function sendMessage(')}
  globalThis.app={container,sends,messageInput,formatUiStateValue,restoreHistory,requests,updates,
    field(slot){const row=new Element('div');appendFieldMetadata(row,slot);return row;},
    render(state){updateSidebar({ui_state:state});},
    busy(value){isSending=value;refreshConstraintActions();},
    readOnly(value){currentReadOnly=value;refreshConstraintActions();},
    permissions(actions){currentActions={...currentActions,...actions};refreshConstraintActions();},
    changeContext(){sessionGeneration++;},
    replaceSnapshot(state){lastResponseData={ui_state:state};},
  };
`,context);
const app=context.app;
const state=(constraints={})=>({read_only:false,actions:{can_send:true,can_modify:true,can_ignore_soft_warning:true},
  slots:[{key:'water_depth',label:{zh:'水深（米）'}}],constraint_state:constraints});
function byClass(root, name) { return nodes(root).filter(node=>node.className.split(' ').includes(name)); }

(async()=>{
  const user=app.field({key:'water_depth',status:'valid',source:'user_input'});
  assert.equal(user.dataset.slotKey,'water_depth');
  assert.equal(user.dataset.slotStatus,'valid');
  assert(text(user).includes('已确定'));
  assert(text(user).includes('来源：用户提供'));
  assert(text(app.field({key:'robot',status:'candidate',source:'auto'})).includes('系统补全'));
  const unknown=app.field({key:'unknown',status:'unresolved',source:'undocumented_origin'});
  assert(text(unknown).includes('来源：未说明'));
  assert(!text(unknown).includes('系统补全'));
  assert.equal(byClass(unknown,'field-source-label')[0].title,'undocumented_origin');
  const missing=app.field({key:'missing',status:'missing',source:'user_input'});
  assert(text(missing).includes('来源：待补充'));
  assert(!text(missing).includes('用户提供'),'empty slots must not claim a supplied value');
  for(const status of ['conflict','invalid','unresolved','missing']) {
    assert.equal(app.field({key:'field',status}).dataset.slotKey,'field');
  }

  assert.equal(app.formatUiStateValue({key:'water_depth',value:300,label:{zh:'水深（米）'}}),'300');
  assert.equal(app.formatUiStateValue({key:'depth_unknown',value:'300 fathoms'}),'300 fathoms');
  assert.equal(app.formatUiStateValue({key:'depth_unknown',value:'300abc'}),'300abc');
  assert.equal(app.formatUiStateValue({key:'water_depth',value:300,display_value:'300 米'}),'300 米');
  assert.equal(app.formatUiStateValue({value:false}),'否');
  assert.equal(app.formatUiStateValue({value:0}),'0');

  app.render(state({source:'validation_result',validated_at:'2026-10-19T09:00:00Z',hard_violations:[{
    code:'C001',constraint_name:'深度条件',message:'<img src=x onerror=alert(1)>',
    observed_value:0,threshold:100,related_fields:['water_depth'],check_type:'depth_limit',
  }]}));
  const details=text(app.container);
  assert(details.includes('当前值 0'));
  assert(details.includes('限值 / 条件 100'));
  assert(details.includes('约束校验结果'));
  assert(details.includes('水深（米）'));
  assert(!nodes(app.container).some(node=>node.tag==='img'),'backend content must remain text');
  const edit=byClass(app.container,'constraint-edit-action')[0];
  await edit.click();
  assert.equal(app.messageInput.value,'修改水深（米）：');
  assert.equal(app.sends.length,0,'editing a constraint only fills the composer');
  app.messageInput.value='已有草稿';
  await edit.click();
  assert(app.messageInput.value.startsWith('已有草稿\n'));
  app.busy(true);
  assert.equal(edit.disabled,true);
  app.messageInput.value='protected';
  await edit.click();
  assert.equal(app.messageInput.value,'protected');
  app.busy(false);
  app.readOnly(true);
  await edit.click();
  assert.equal(app.messageInput.value,'protected');
  app.readOnly(false);
  app.permissions({can_modify:false});
  assert.equal(edit.disabled,true);
  await edit.click();
  assert.equal(app.messageInput.value,'protected','latest action revocation overrides a captured permission');
  app.permissions({can_modify:true,can_send:false});
  await edit.click();
  assert.equal(app.messageInput.value,'protected','can_send=false blocks modification shortcuts');
  app.permissions({can_send:true});
  app.replaceSnapshot(state());
  await edit.click();
  assert.equal(app.messageInput.value,'protected','old handlers may not modify a newer state');

  app.render(state({soft_warnings:[{constraint_id:'C002',message:'当前 450，最大 500'}]}));
  const noEvidence=text(app.container);
  assert(!noEvidence.includes('限值 / 条件'),'numbers in message are not structured evidence');
  assert(!noEvidence.includes('结果来源'),'missing source must not be invented');
  assert.equal(byClass(app.container,'constraint-edit-action').length,0,'missing related_fields do not imply an edit target');
  const acknowledge=byClass(app.container,'btn-ignore-warning-action')[0];
  await acknowledge.click(); await acknowledge.click();
  assert.deepEqual(Array.from(app.sends),['忽略警告'],'in-flight guard prevents repeated acknowledgement');
  app.busy(false);
  app.changeContext();
  await acknowledge.click();
  assert.equal(app.sends.length,1,'a former session cannot acknowledge new risks');

  app.render(state({soft_warnings:[{constraint_id:'C003',message:'old'}],ignored_soft_warnings:[{constraint_id:'C003',message:'acknowledged'}]}));
  assert.equal(byClass(app.container,'btn-ignore-warning-action').length,0);
  assert(!text(app.container).includes('old'));
  assert(text(app.container).includes('已确认忽略'));
  app.render(state());
  assert(!text(app.container).includes('通过'),'absence of violations is not proof of successful validation');
  app.render(state({error:'deadline exceeded'}));
  assert(text(app.container).includes('校验未完成：deadline exceeded'));

  for (const failure of ['rejected', 'network']) {
    app.render(state({hard_violations:[{constraint_id:'C-RESTORE',message:'Needs correction',related_fields:['water_depth']}]}));
    const previous=app.updates.at(-1).data;
    const previousEdit=byClass(app.container,'constraint-edit-action')[0];
    const restore=app.restoreHistory('unavailable-history');
    await Promise.resolve();
    app.messageInput.value='';
    await previousEdit.click();
    assert.equal(app.messageInput.value,'','restore in flight must block the former action');
    const request=app.requests.at(-1);
    if(failure==='rejected') request.resolve({async json(){return {code:500,msg:'unavailable'};}});
    else request.reject(new Error('connection lost'));
    await restore;
    assert.equal(app.updates.at(-1).data,previous,'failed restore must retain the original snapshot');
    assert.equal(app.updates.at(-1).options.origin,'render','failed restore is not a reply or new restored task');
    const recoveredEdit=byClass(app.container,'constraint-edit-action')[0];
    assert.notEqual(recoveredEdit,previousEdit,'render creates handlers for the current generation');
    assert.equal(recoveredEdit.disabled,false);
    await previousEdit.click();
    assert.equal(app.messageInput.value,'','former generation handlers stay inert');
    const sendsBefore=app.sends.length;
    await recoveredEdit.click();
    assert.equal(app.messageInput.value,'修改水深（米）：','the retained task remains editable after restore failure');
    assert.equal(app.sends.length,sendsBefore);
  }
  console.log('Field provenance, structured constraint evidence and guarded actions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
