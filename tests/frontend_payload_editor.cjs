const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/index.js', 'utf8');
const first = source.indexOf('    function buildPayloadSections(');
const last = source.indexOf('    async function updateSimulatedTime(', first);

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.listeners = {}; this.className = ''; this.textContent = ''; }
  get childElementCount() { return this.children.length; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  click() { this.listeners.click?.({stopPropagation() {}}); }
  remove() { this.parent.children = this.parent.children.filter(x => x !== this); }
  get classList() { return { toggle: (name, enabled) => { const c = new Set(this.className.split(' ')); if (enabled) c.add(name); else c.delete(name); this.className = [...c].join(' '); } }; }
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
function harness() {
  const root = new Element('div');
  const context = vm.createContext({Element, root, assert});
  vm.runInContext(`
    const currentLang='zh'; let isSending=false;
    let lastResponseData=null;
    const messageInput={value:''}; const messageContainer=root; const sent=[];
    const document={createElement: tag=>new Element(tag),getElementById: id=>all(root).find(e=>e.id===id)};
    function all(node) {return [node,...node.children.flatMap(all)];}
    function getSlotUiLabel(slot) {return slot.label.zh;}
    function sendMessage(text) {sent.push(text);}
    ${source.slice(first,last)}
    globalThis.app={sent, render(state) {lastResponseData={ui_state:state};renderOptionChips(state);}, sections: buildPayloadSections};
  `, context);
  return {root, app:context.app};
}
const slot = {
  key:'payload',label:{zh:'携带工具'},schema_type:'list',status:'valid',value:['多波束声呐系统'],
  allowed_values:['高清水下摄像机','浑水水下成像系统','多波束声呐系统','机械扫描声呐','云台摄像机'],
  payload_groups:{Visual_sensor:['高清水下摄像机','浑水水下成像系统','云台摄像机'],Acoustic_sensor:['多波束声呐系统','机械扫描声呐']},
  onboard_payload_groups:{Visual_sensor:['单目水下成像系统','云台摄像机']},
};
const state={phase:'collecting',read_only:false,task_type_key:'pipeline_inspection',editing_slot:'payload',slots:[
  ...['start_time','end_time','start_point'].map(key=>({key,status:'missing',schema_type:'string'})),slot,
]};
const {root,app}=harness();
app.render(state);
let nodes=descendants(root);
assert(nodes.some(e=>e.className==='payload-selector-panel'),'Explicit editor must appear ahead of the three missing time/coordinate slots');
const camera=nodes.find(e=>e.tag==='button' && e.textContent==='高清水下摄像机');
assert(camera,'Supported HD camera must not be inferred to be permanent onboard hardware');
const sonar=nodes.find(e=>e.tag==='button' && e.textContent==='✓ 多波束声呐系统');
assert(sonar,'Committed sonar must be preselected');
camera.click(); sonar.click();
assert.equal(app.sent.length,0,'Option clicks must only change the unsaved local selection');
assert.deepEqual(slot.value,['多波束声呐系统'],'Option clicks must not mutate committed UI slot');
nodes.find(e=>e.className==='payload-selector-confirm').click();
assert.equal(app.sent.length,1);
assert(app.sent[0].includes('高清水下摄像机'));
assert(!app.sent[0].includes('多波束声呐系统'),'Confirmation is the complete newly selected list');
app.render(state);
nodes=descendants(root);
assert(nodes.some(e=>e.tag==='button' && e.textContent==='✓ 多波束声呐系统'),'Reload restores committed selection, not an unsubmitted choice');
const cancel=nodes.find(e=>e.className==='payload-selector-cancel');
assert(cancel,'An explicit editor must allow cancellation');
cancel.click();
assert.equal(app.sent.at(-1),'取消载荷修改');
app.render({...state,editing_slot:null});
assert(!descendants(root).some(e=>e.className==='payload-selector-panel'),'Valid payload should stop editing after cancellation or submission');
app.render({...state,read_only:true});
assert(!descendants(root).some(e=>e.className==='payload-selector-panel'),'Read-only task cannot be edited');
console.log('Payload editor DOM regressions passed');
