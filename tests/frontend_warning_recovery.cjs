const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/index.js', 'utf8');
const first = source.indexOf('    function retireDecisionCards(');
const last = source.indexOf('    function getSlotUiLabel(', first);
function all(node) { return [node, ...node.children.flatMap(all)]; }
class Element {
  constructor() { this.children=[]; this.listeners={}; this.className=''; this.innerHTML=''; this.disabled=false; this.style={}; }
  appendChild(child) { child.parent=this; this.children.push(child); }
  replaceChildren(...children) { this.children=[]; children.forEach(child=>this.appendChild(child)); }
  remove() { this.parent.children=this.parent.children.filter(child=>child!==this); }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  querySelector(selector) {
    const classes=selector.split(',').map(name=>name.trim().slice(1));
    return all(this).slice(1).find(node=>classes.some(name=>node.classList.contains(name))) || null;
  }
  get classList() {
    const change=(name,add)=>{const names=new Set(this.className.split(' '));add?names.add(name):names.delete(name);this.className=[...names].join(' ');};
    return {add:name=>change(name,true),remove:name=>change(name,false),contains:name=>this.className.split(' ').includes(name)};
  }
  // Deliberately invoke listeners even on detached/disabled nodes to test handler guards.
  click() { return this.listeners.click?.({stopPropagation(){}}); }
}
const context=vm.createContext({Element, all});
vm.runInContext(`
  let isSending=false, sessionGeneration=0, currentReadOnly=false;
  let currentDecisionCard=null, currentDecisionState=null;
  const currentActions={can_send:true,can_modify:true,can_ignore_soft_warning:true}, currentLang='zh';
  const wrappers=[], messageContainer={}, sent=[], input={value:'',focus(){}};
  let finish;
  const window={sendMessage(message){sent.push(message);return new Promise(resolve=>finish=resolve);}};
  const document={
    createElement(){return new Element();},
    getElementById(){return input;},
    querySelectorAll(){return wrappers.flatMap(all).filter(node=>
      (node.classList.contains('warning-decision-card') || node.classList.contains('hard-blocked-card')) &&
      !node.classList.contains('warning-card-resolved'));},
  };
  function escapeHtml(value){return value;}
  ${source.slice(first,last)}
  function render(phase='blocked_soft'){
    const wrapper=new Element(); wrappers.push(wrapper);
    renderDecisionCardInBubble({querySelector(){return wrapper;}},{phase,workflow_phase:'validating',actions:currentActions});
    return wrapper;
  }
  globalThis.test={wrapper:render(),render,sent,input,retireDecisionCards,
    finish(){finish();},busy(value){isSending=value;},nextContext(){sessionGeneration++;},
    revoke(){currentActions.can_ignore_soft_warning=false;},replaceState(){currentDecisionState={};}};
`,context);
(async()=>{
  const test=context.test;
  const button=all(test.wrapper).find(node=>node.className==='btn-ignore-warning-confirm');
  const card=all(test.wrapper).find(node=>node.className==='warning-decision-card');
  const oldModify=all(test.wrapper).find(node=>node.className==='btn-modify-warning-task');
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
  assert(button.innerHTML.includes('确认风险并继续核验'));
  assert.equal(test.sent.length,1,'the client must not replay the warning acknowledgement');

  const next=test.render();
  assert(card.classList.contains('warning-card-resolved'),'consecutive soft warnings must retire older controls');
  assert.equal(card.querySelector('.historical-validation-note').textContent,'历史校验结果，以当前状态为准');
  assert.equal(card.querySelector('.btn-ignore-warning-confirm'),null);
  await button.click();
  oldModify.click();
  assert.equal(test.sent.length,1,'a detached stale warning button must not acknowledge current risks');
  assert.equal(test.input.value,'','a detached stale modify button must not change the composer');

  const newest=next.querySelector('.btn-ignore-warning-confirm');
  const pending=newest.click();
  const hard=test.render('blocked_hard');
  test.finish();
  await pending;
  assert.equal(next.querySelector('.btn-ignore-warning-confirm'),null,'late completion must not reactivate a retired card');
  assert.equal(test.sent.length,2);
  const hardModify=hard.querySelector('.btn-modify-warning-task');
  hardModify.click();
  assert.equal(test.input.value,'修改参数：');
  test.input.value='protected draft';
  test.nextContext();
  hardModify.click();
  assert.equal(test.input.value,'protected draft','history/reset generation invalidates previous controls');

  const replaced=test.render().querySelector('.btn-ignore-warning-confirm');
  test.replaceState();
  await replaced.click();
  assert.equal(test.sent.length,2,'a new authoritative state invalidates the previous card');
  const revoked=test.render().querySelector('.btn-ignore-warning-confirm');
  test.revoke();
  await revoked.click();
  assert.equal(test.sent.length,2,'latest action permissions remain authoritative');
  console.log('Warning action recovery and stale-card regressions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
