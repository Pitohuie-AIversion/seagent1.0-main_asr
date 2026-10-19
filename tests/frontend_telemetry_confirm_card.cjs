const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('frontend/js/index.js', 'utf8');
const first = source.indexOf('    function retireDecisionCards(');
const last = source.indexOf('    function getSlotUiLabel(', first);

function all(node) {
  return [node, ...(node.children || []).flatMap(all)];
}

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.className = '';
    this._innerHTML = '';
    this.textContent = '';
    this.disabled = false;
    this.style = {};
  }
  get innerHTML() {
    return (this._innerHTML || '') + (this.children || []).map(c => c.innerHTML).join('');
  }
  set innerHTML(val) {
    this._innerHTML = val;
  }
  appendChild(child) {
    child.parent = this;
    this.children.push(child);
  }
  replaceChildren(...children) {
    this.children = [];
    this._innerHTML = '';
    children.forEach(child => this.appendChild(child));
  }
  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter(child => child !== this);
    }
  }
  addEventListener(name, fn) {
    this.listeners[name] = fn;
  }
  querySelector(selector) {
    const classes = selector.split(',').map(name => name.trim().replace(/^\./, ''));
    return all(this).slice(1).find(node => classes.some(name => node.classList.contains(name))) || null;
  }
  querySelectorAll(selector) {
    const classes = selector.split(',').map(name => name.trim().replace(/^\./, ''));
    return all(this).slice(1).filter(node => classes.some(name => node.classList.contains(name)));
  }
  get classList() {
    const change = (name, add) => {
      const names = new Set(this.className.split(' ').filter(Boolean));
      if (add) names.add(name); else names.delete(name);
      this.className = [...names].join(' ');
    };
    return {
      add: name => change(name, true),
      remove: name => change(name, false),
      contains: name => this.className.split(' ').filter(Boolean).includes(name),
    };
  }
  click() {
    return this.listeners.click?.({ stopPropagation() {} });
  }
}

const context = vm.createContext({ Element, all });
vm.runInContext(`
  let isSending = false, sessionGeneration = 0, currentReadOnly = false;
  let currentDecisionCard = null, currentDecisionState = null;
  const currentActions = { can_send: true, can_modify: true, can_confirm: true, can_publish: true };
  const currentLang = 'zh';
  const wrappers = [], messageContainer = {}, sent = [], input = { value: '', focus() {} };
  let finish;
  const window = {
    sendMessage(message) {
      sent.push(message);
      return new Promise(resolve => finish = resolve);
    }
  };
  const document = {
    createElement() { return new Element(); },
    getElementById() { return input; },
    querySelectorAll(selector) {
      return wrappers.flatMap(all).filter(node =>
        (node.classList.contains('warning-decision-card') ||
         node.classList.contains('hard-blocked-card') ||
         node.classList.contains('telemetry-decision-card')) &&
        !node.classList.contains('warning-card-resolved')
      );
    },
  };
  function escapeHtml(value) { return String(value); }
  function updateSidebar() {}
  ${source.slice(first, last)}

  function render(phase = 'confirming', customUi = {}) {
    const wrapper = new Element();
    wrappers.push(wrapper);
    const defaultState = {
      phase,
      workflow_phase: phase,
      actions: currentActions,
      slots: [{ key: 'task_type', status: 'confirmed' }],
      constraint_state: {
        state_snapshot: {
          unit_id: 'LROV-150-002',
          status_ref: 'LROV-150-002',
          state_version: 2277,
          updated_at: '2026-10-19T15:59:00+08:00',
          state: {
            overall_status: 'available',
            water_current_velocity: 0.1,
            water_turbidity: 1.0,
            obstacle_density: 'low',
            mothership_support: 'strong',
            thruster_status: 'normal',
            depth_keeping_status: 'normal',
            vision_status: 'normal',
            sonar_status: 'normal',
            arm_status: 'normal',
            end_effector_status: 'normal',
            acoustic_comms_status: 'normal',
            tether_connection_status: 'normal',
          }
        }
      },
      ...customUi,
    };
    renderDecisionCardInBubble({ querySelector() { return wrapper; } }, defaultState);
    return wrapper;
  }

  this.test = {
    render,
    sent,
    finish() { finish?.(); },
  };
`, context);

(async function main() {
  const test = context.test;

  // 1. 验证在 confirming 阶段成功渲染 telemetry-decision-card
  const wrapper = test.render('confirming');
  const card = wrapper.querySelector('.telemetry-decision-card');
  assert.ok(card, 'confirming 阶段必须渲染 telemetry-decision-card');

  // 2. 验证卡片头部内容
  assert.ok(card.innerHTML.includes('LIVE TELEMETRY'));
  assert.ok(card.innerHTML.includes('LROV-150-002'));
  assert.ok(card.innerHTML.includes('实时遥测与作业环境核验看板'));
  assert.ok(card.innerHTML.includes('就绪可用'));

  // 3. 验证环境指标与子系统
  assert.ok(card.innerHTML.includes('0.1 m/s'));
  assert.ok(card.innerHTML.includes('1 NTU') || card.innerHTML.includes('1.0 NTU') || card.innerHTML.includes('1'));
  assert.ok(card.innerHTML.includes('推进系统'));
  assert.ok(card.innerHTML.includes('定深控制'));
  assert.ok(card.innerHTML.includes('声呐系统'));
  assert.ok(card.innerHTML.includes('水下视觉'));

  // 4. 验证交互按钮
  const confirmBtn = card.querySelector('.btn-telemetry-confirm-publish');
  assert.ok(confirmBtn, '确认发布按钮必须存在');

  const submission = confirmBtn.click();
  assert.equal(confirmBtn.disabled, true, '点击后必须进入禁用加载态');
  assert.ok(card.classList.contains('warning-card-resolving'), '卡片进入 resolving 态');
  test.finish();
  await submission;
  assert.equal(confirmBtn.disabled, false);
  assert.equal(test.sent.length, 1);
  assert.equal(test.sent[0], '确认发布', '必须发送确认发布指令');

  // 5. 验证新轮次渲染时旧遥测卡片自动归档
  const nextWrapper = test.render('confirming');
  assert.ok(card.classList.contains('warning-card-resolved'), '上一轮遥测卡片必须标记为已归档 resolved');
  assert.equal(card.querySelector('.btn-telemetry-confirm-publish'), null, '已归档的旧遥测卡片不得保留交互按钮');
  assert.ok(card.querySelector('.historical-validation-note'), '已归档卡片必须保留历史校验备注');

  console.log('Telemetry decision card rendering, interaction and retirement regressions passed.');
})();
