/* Read-only views of final backend snapshots. No inferred tool trace or task writes. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const present = value => value !== undefined && value !== null && value !== '';
  const node = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const copy = {
    zh: {
      valid: '已确认', review: '待核对', missing: '待补充', candidate: '待确认候选',
      conflict: '存在冲突', invalid: '无效', unresolved: '待解析', unknown: '状态未知',
      before: '之前', after: '现在', added: '新增', removed: '已移除', changed: '本轮更新',
      retained: '原有值', proposal: '候选值', input: '输入', empty: '未提供', locate: '定位字段：',
      receipt: '本轮处理结果', snapshot: '当前任务状态', restored: '恢复的任务状态',
      parameters: '参数状态', validation: '条件校验', validationTime: '校验时间',
      stateTime: '所用状态更新时间', unit: '校验所用单机', stateVersion: '所用状态版本',
      next: '下一步', collecting: '补充或核对任务参数', validating: '等待条件核验结果',
      confirming: '核对摘要后明确确认发布', blocked_soft: '处理警告后继续', blocked_hard: '调整不满足条件的参数',
      done: '任务已归档；下发与执行结果另行查看', rejected: '本任务已结束',
      validCheck: '校验通过', pendingCheck: '等待运行条件校验', warningCheck: '存在待处理警告',
      acknowledgedCheck: '存在已确认风险',
      blockedCheck: '条件未满足', errorCheck: '校验未完成：返回异常', noCheck: '尚无有效校验结果',
      staleCheck: '历史校验结果已过期，需重新核验', accepted: '已接受的风险',
      reviewNote: '仍有字段待核对或补充，请查看工作单。', noValues: '暂无已确认参数。',
      waterCurrent: '海流速度', waterTurbidity: '水体浑浊度',
    },
    en: {
      valid: 'Confirmed', review: 'Review', missing: 'Missing', candidate: 'Candidate',
      conflict: 'Conflict', invalid: 'Invalid', unresolved: 'Unresolved', unknown: 'Unknown status',
      before: 'Before', after: 'Now', added: 'Added', removed: 'Removed', changed: 'Updated this turn',
      retained: 'Previous value', proposal: 'Candidate', input: 'Input', empty: 'Not provided', locate: 'Go to field: ',
      receipt: 'Result of this turn', snapshot: 'Current task state', restored: 'Restored task state',
      parameters: 'Parameters', validation: 'Validation', validationTime: 'Validated at',
      stateTime: 'State updated at', unit: 'Unit used for validation', stateVersion: 'State version used',
      next: 'Next step', collecting: 'Complete or review task parameters', validating: 'Await validation results',
      confirming: 'Review the summary and explicitly confirm publication', blocked_soft: 'Review warnings to continue', blocked_hard: 'Revise parameters that do not meet conditions',
      done: 'Task archived; see dispatch and execution results separately', rejected: 'This task is closed',
      validCheck: 'Validation passed', pendingCheck: 'Runtime validation pending', warningCheck: 'Warnings need review',
      acknowledgedCheck: 'Acknowledged risks remain',
      blockedCheck: 'Conditions not met', errorCheck: 'Validation incomplete: error returned', noCheck: 'No current validation result',
      staleCheck: 'Validation is out of date; revalidation required', accepted: 'Acknowledged risks',
      reviewNote: 'Some parameters still need review or completion. Check the worksheet.', noValues: 'No confirmed parameters yet.',
      waterCurrent: 'Current Velocity', waterTurbidity: 'Turbidity',
    },
  };
  let lang = 'zh';
  let baseline = null;
  let changes = [];
  let origin = 'snapshot';
  let callbacks = {};
  let receiptVisible = false;
  const t = () => copy[lang];
  const stable = value => JSON.stringify(value, (_, item) => {
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      return Object.keys(item).sort().reduce((result, key) => { result[key] = item[key]; return result; }, {});
    }
    return item;
  });
  const plain = value => typeof value === 'object' ? JSON.stringify(value) : String(value);
  const status = slot => t()[slot?.status] || t().unknown;
  const label = slot => callbacks.slotLabel ? callbacks.slotLabel(slot)
    : typeof slot.label === 'string' ? slot.label : slot.label?.[lang] || slot.label?.zh || slot.key;
  const value = (slot, candidate = false) => {
    const raw = candidate ? slot.candidate_value : slot.value;
    if (!present(raw)) return t().empty;
    return callbacks.formatSlotValue ? callbacks.formatSlotValue(slot, candidate ? raw : undefined) : plain(raw);
  };

  function counts(slots) {
    const valid = slots.filter(slot => slot.status === 'valid').length;
    const missing = slots.filter(slot => slot.status === 'missing').length;
    return { valid, review: slots.length - valid - missing, missing };
  }

  function countText(slots) {
    const n = counts(slots);
    return `${t().valid} ${n.valid} · ${t().review} ${n.review} · ${t().missing} ${n.missing}`;
  }

  function sameTask(previous, current) {
    if (!previous || previous.session !== current.session || previous.generation !== current.generation) return false;
    const a = previous.ui, b = current.ui;
    if (a.task_type_key !== b.task_type_key) return false;
    if (a.task_id && a.task_id !== b.task_id) return false;
    if (['done', 'rejected'].includes(a.phase) && !['done', 'rejected'].includes(b.phase)) return false;
    if (Number.isFinite(a.slot_version) && Number.isFinite(b.slot_version) && b.slot_version < a.slot_version) return false;
    return true;
  }

  function semantic(slot) {
    // Source/confidence/version changes alone are not parameter edits.
    return slot ? stable({ value: slot.value ?? null, candidate: slot.candidate_value ?? null, status: slot.status,
      raw: ['invalid', 'unresolved'].includes(slot.status) ? slot.raw_value ?? null : null }) : null;
  }

  function compare(previous, current) {
    const old = new Map((sameTask(previous, current) ? previous.ui.slots : []).map(slot => [slot.key, slot]));
    const next = new Map(current.ui.slots.map(slot => [slot.key, slot]));
    return [...new Set([...old.keys(), ...next.keys()])].flatMap(key => {
      const before = old.get(key), after = next.get(key);
      if (semantic(before) === semantic(after) || (!before && after?.status === 'missing')) return [];
      return [{ key, before, after }];
    });
  }

  function fieldRow(key) {
    return [...document.querySelectorAll('#collectedFields .field-row, #missingFields .field-row')]
      .find(row => row.dataset.slotKey === key);
  }

  function fieldLink(slot, text) {
    const button = node('button', 'field-reference', text);
    button.type = 'button';
    button.dataset.slotKey = slot.key;
    button.setAttribute('aria-label', `${t().locate}${label(slot)} · ${text}`);
    button.addEventListener('click', () => {
      const row = fieldRow(slot.key);
      if (!row) return;
      if ($('taskWorksheet')) $('taskWorksheet').open = true;
      row.tabIndex = -1;
      row.scrollIntoView({ block: 'nearest' });
      row.focus({ preventScroll: true });
    });
    return button;
  }

  function describe(slot) {
    if (!slot) return t().removed;
    const parts = [status(slot)];
    if (['invalid', 'unresolved'].includes(slot.status) && present(slot.raw_value)) parts.push(`${t().input}：${plain(slot.raw_value)}`);
    if (present(slot.value)) parts.push(`${slot.status === 'valid' ? '' : t().retained + '：'}${value(slot)}`);
    if (slot.status !== 'valid' && present(slot.candidate_value)) parts.push(`${t().proposal}：${value(slot, true)}`);
    return parts.join(' · ');
  }

  function renderChanges(animate = false) {
    const list = $('taskChangesList');
    if (!list) return;
    list.replaceChildren();
    document.querySelectorAll('.field-changed').forEach(row => row.classList.remove('field-changed', 'field-change-flash'));
    document.querySelectorAll('.field-change-label').forEach(el => el.remove());
    changes.forEach(change => {
      const slot = change.after || change.before;
      const item = node('li', 'task-change');
      item.append(change.after ? fieldLink(slot, label(slot)) : node('strong', '', label(slot)));
      if (change.before) item.append(node('p', 'change-before', `${t().before} · ${describe(change.before)}`));
      item.append(node('p', 'change-after', `${change.before ? t().after : t().added} · ${describe(change.after)}`));
      list.append(item);
      const row = fieldRow(change.key);
      if (row) {
        row.classList.add('field-changed');
        if (animate) row.classList.add('field-change-flash');
        const badge = node('span', 'field-change-label', t().changed);
        (row.querySelector('.field-metadata') || row).append(badge);
      }
    });
    $('taskChangesCard').hidden = changes.length === 0;
  }

  function validationText(cs) {
    if (cs.source === 'stale_validation_result') return t().staleCheck;
    if (cs.error) return t().errorCheck;
    const outcome = cs.overall_status || cs.status;
    if (['warning', 'blocked_soft'].includes(outcome) && !cs.hard_violations?.length &&
        !cs.soft_warnings?.length && cs.ignored_soft_warnings?.length) return t().acknowledgedCheck;
    const key = {
      valid: 'validCheck', pending_runtime_validation: 'pendingCheck', warning: 'warningCheck',
      blocked_soft: 'warningCheck', blocked_hard: 'blockedCheck', validation_error: 'errorCheck',
    }[outcome];
    return t()[key] || t().noCheck;
  }

  function pair(dl, name, content) {
    dl.append(node('dt', '', name), typeof content === 'string' ? node('dd', '', content) : content);
  }

  function renderConfirmation(ui) {
    const summary = $('confirmationSummary'), review = $('confirmationReview');
    if (!summary || !review) return;
    summary.replaceChildren();
    review.replaceChildren();
    if (ui.phase !== 'confirming' || ui.read_only) return;
    ui.slots.filter(slot => slot.status === 'valid').forEach(slot => {
      const dd = node('dd');
      dd.append(fieldLink(slot, value(slot)));
      pair(summary, label(slot), dd);
    });
    if (!summary.children.length) review.append(node('p', '', t().noValues));
    if (ui.slots.some(slot => slot.status !== 'valid')) review.append(node('p', 'review-pending', t().reviewNote));
    const cs = ui.constraint_state || {};
    review.append(node('p', 'review-validation', `${t().validation} · ${validationText(cs)}`));
    if (cs.ignored_soft_warnings?.length) {
      review.append(node('strong', '', t().accepted));
      const risks = node('ul');
      cs.ignored_soft_warnings.forEach(risk => risks.append(node('li', '',
        typeof risk === 'string' ? risk : risk.message || risk.constraint_name || risk.constraint_id || risk.code || t().unknown)));
      review.append(risks);
    }
  }

  function renderReceipt(ui) {
    const receipt = $('turnReceipt'), body = $('turnReceiptBody');
    if (!receipt || !body) return;
    receipt.hidden = !receiptVisible;
    body.replaceChildren();
    if (!receiptVisible) return;
    const heading = origin === 'reply' ? t().receipt : origin === 'restore' ? t().restored : t().snapshot;
    const next = t()[ui.phase] && !['valid', 'missing', 'candidate'].includes(ui.phase) ? t()[ui.phase] : t().collecting;
    $('turnReceiptSummary').textContent = `${heading} · ${next}`;
    const dl = node('dl', 'receipt-facts');
    pair(dl, t().parameters, countText(ui.slots));
    const cs = ui.constraint_state || {};
    pair(dl, t().validation, validationText(cs));
    if (present(cs.validated_at)) pair(dl, t().validationTime, plain(cs.validated_at));
    const snapshot = cs.state_snapshot || {};
    if (present(snapshot.unit_id)) pair(dl, t().unit, plain(snapshot.unit_id));
    if (present(snapshot.updated_at)) pair(dl, t().stateTime, plain(snapshot.updated_at));
    if (present(snapshot.state_version)) pair(dl, t().stateVersion, plain(snapshot.state_version));
    if (snapshot.state && typeof snapshot.state === 'object') {
      const vel = snapshot.state.water_current_velocity !== undefined ? snapshot.state.water_current_velocity : snapshot.state.current_velocity;
      const turb = snapshot.state.water_turbidity !== undefined ? snapshot.state.water_turbidity : snapshot.state.turbidity;
      if (present(vel)) pair(dl, t().waterCurrent, `${vel} m/s`);
      if (present(turb)) pair(dl, t().waterTurbidity, `${turb} NTU`);
    }
    pair(dl, t().next, next);
    body.append(dl);
  }

  function updateState(data, language, options = {}) {
    lang = language === 'en' ? 'en' : 'zh';
    callbacks = { ...callbacks, ...options };
    const ui = data?.ui_state;
    if (!ui || ui.state_status === 'error' || !(ui.task_type_key || ui.task_id || ui.slots?.length)) {
      reset();
      return;
    }
    const current = {
      session: data.session_id || null, generation: window.sessionGeneration,
      ui: JSON.parse(JSON.stringify({ ...ui, slots: Array.isArray(ui.slots) ? ui.slots : [] })),
    };
    const reason = options.origin || 'snapshot';
    if (reason !== 'render') {
      changes = reason === 'reply' ? compare(baseline, current) : [];
      baseline = current;
      origin = reason;
      receiptVisible = true;
    } else if (baseline?.session === current.session && stable(baseline.ui) === stable(current.ui)) {
      // A failed reset/history request may cancel work with a new generation while
      // retaining this exact session snapshot. Repaint must not invent a new task.
      baseline.generation = current.generation;
    }
    const counter = $('worksheetCounts');
    if (counter) {
      counter.replaceChildren();
      Object.entries(counts(current.ui.slots)).forEach(([key, number]) => {
        const cell = node('span', `count-${key}`);
        cell.append(node('strong', '', number), node('span', '', t()[key]));
        counter.append(cell);
      });
    }
    renderChanges(reason === 'reply');
    renderConfirmation(current.ui);
    renderReceipt(current.ui);
  }

  function beginTurn() {
    changes = [];
    receiptVisible = false;
    renderChanges();
    if ($('turnReceipt')) { $('turnReceipt').hidden = true; $('turnReceipt').open = false; }
  }

  function reset() {
    baseline = null;
    beginTurn();
    ['worksheetCounts', 'confirmationSummary', 'confirmationReview', 'turnReceiptBody', 'turnReceiptSummary']
      .forEach(id => $(id)?.replaceChildren());
  }

  window.SEAgentTaskInsights = { updateState, beginTurn, reset };
})();
