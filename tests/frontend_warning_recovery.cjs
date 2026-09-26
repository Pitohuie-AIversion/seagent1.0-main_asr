const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/index.js', 'utf8');
const first = source.indexOf('    function renderDecisionCardInBubble(');
const last = source.indexOf('    function getSlotUiLabel(', first);
class Element {
  constructor() { this.children=[]; this.listeners={}; this.className=''; this.innerHTML=''; this.disabled=false; }
  appendChild(child) { this.children.push(child); }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  querySelector() { return null; }
  get classList() {
    const change=(name,add)=>{const names=new Set(this.className.split(' '));add?names.add(name):names.delete(name);this.className=[...names].join(' ');};
    return {add:name=>change(name,true),remove:name=>change(name,false),contains:name=>this.className.split(' ').includes(name)};
  }
  click() { return this.listeners.click?.({stopPropagation(){}}); }
}
const context=vm.createContext({Element});
vm.runInContext(`
  let isSending=false, sessionGeneration=0;
  const currentActions={can_send:true,can_ignore_soft_warning:true}, currentLang='zh';
  const wrapper=new Element(), messageContainer={}, sent=[];
  let finish;
  const window={sendMessage(message){sent.push(message);return new Promise(resolve=>finish=resolve);}};
  const document={createElement(){return new Element();},querySelectorAll(){return [];}};
  function escapeHtml(value){return value;}
  ${source.slice(first,last)}
  renderDecisionCardInBubble({querySelector(){return wrapper;}},{phase:'blocked_soft',actions:currentActions});
  globalThis.test={wrapper,sent,finish(){finish();},busy(value){isSending=value;}};
`,context);
function all(node){return [node,...node.children.flatMap(all)];}
(async()=>{
  const test=context.test;
  const button=all(test.wrapper).find(node=>node.className==='btn-ignore-warning-confirm');
  const card=all(test.wrapper).find(node=>node.className==='warning-decision-card');
  test.busy(true);
  await button.click();
  assert.equal(test.sent.length,0,'an unrelated in-flight request must not consume the warning button');
  assert.equal(button.disabled,false);
  test.busy(false);
  const submission=button.click();
  assert.equal(button.disabled,true);
  test.finish();
  await submission;
  assert.equal(button.disabled,false,'handled network failure must allow a deliberate retry');
  assert(!card.classList.contains('warning-card-resolving'));
  assert(button.innerHTML.includes('忽略警告并继续发布'));
  assert.equal(test.sent.length,1,'the client must not replay the warning acknowledgement');
  console.log('Warning action recovery regressions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
