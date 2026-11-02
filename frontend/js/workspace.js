/* Presentation for the conversation workspace. Backend ui_state remains authoritative. */
(function () {
  'use strict';
  const copy = {
    zh: {
      skip: '跳转到输入框', workspace: '对话工作台', monitor: '运行监控', language: '界面语言',
      conversation: '智能交互', latest: '回到最新消息', recording: '正在录音', cancelRecording: '取消录音',
      inputLabel: '作业需求或查询问题', inputHint: '支持自然语言描述与语音输入', keyboardHint: 'Enter 发送 · Shift＋Enter 换行',
      worksheet: '任务工作单', emptyTitle: '尚未创建任务', emptyBody: '描述作业需求后，任务参数、待补充信息和校验结果将在这里呈现。',
      emptyNote: '知识查询不会创建或修改任务', confirmTitle: '任务已就绪，等待你的确认',
      confirmBody: '请核对下方参数。确认后归档任务，发送结果将单独显示。', publish: '确认发布', modify: '修改参数',
      timeLabel: '设置模拟基准时间（上海时间 UTC+08:00）', timeZone: '上海时间 · UTC+08:00', archiveNote: '任务归档、指令发送与执行结果分别记录',
      welcomeEyebrow: '面向深海作业的智能协作', welcomeTitle: '从一个问题，或一项任务开始。',
      welcomeBody: '描述你的作业需求，或查询机器人、设备与环境信息。',
      queryTitle: '知识与状态查询', queryBody: '了解机器人能力、设备参数与相关状态。', queryNote: '只读查询 · 不修改任务',
      taskTitle: '任务创建与准入', taskBody: '收集作业参数，核对约束条件，形成待确认任务。', taskNote: '明确确认后发布',
      queryExample: '金牛座一号机的最大作业水深是多少？', taskExample: '创建一个海底管线巡检任务。',
      exampleLabel: '填入示例', exampleHint: '点击示例填入输入框，编辑后发送', details: '了解系统能力与使用说明',
      idle: '等待输入', busy: '正在处理', chat: '对话中', query: '知识查询 · 只读', task: '任务规划', archived: '任务已归档', emergency: '紧急干预模式',
      empty: '待创建', collecting: '待补充', validating: '核验中', confirming: '待确认', done: '已归档', rejected: '已结束',
      blocked_soft: '待处理警告', blocked_hard: '条件未满足', steps: ['参数收集', '任务准入', '确认归档'],
      modifyPrompt: '修改任务参数：', waiting: '正在提交…', log: '对话记录', sidebar: '任务与会话', progress: '任务流程',
      changesTitle: '本轮参数变更', unavailable: '状态暂不可用', errorTitle: '任务状态暂不可用', errorBody: '当前返回的任务状态不完整，请恢复会话状态后再核对和发布。',
      shortcutTask: '任务', shortcutView: '查看任务工作单', shortcutReadOnly: '只读', shortcutCollecting: '参数收集',
      shortcutMissing: count => `待补充 ${count} 项`, shortcutReview: count => `待核对 ${count} 项`,
    },
    en: {
      skip: 'Skip to message input', workspace: 'Conversation', monitor: 'Monitor', language: 'Interface language',
      conversation: 'Intelligent interaction', latest: 'Latest messages', recording: 'Recording', cancelRecording: 'Cancel recording',
      inputLabel: 'Task description or question', inputHint: 'Describe your task or use voice input', keyboardHint: 'Enter to send · Shift+Enter for a new line',
      worksheet: 'Task worksheet', emptyTitle: 'No task yet', emptyBody: 'Describe an operation to see its parameters, missing information and validation results here.',
      emptyNote: 'Knowledge queries do not create or modify tasks', confirmTitle: 'Ready for your confirmation',
      confirmBody: 'Review the parameters below. Confirmation archives the task; dispatch is reported separately.', publish: 'Confirm publication', modify: 'Edit parameters',
      timeLabel: 'Set simulation reference time (Shanghai UTC+08:00)', timeZone: 'Shanghai · UTC+08:00', archiveNote: 'Archival, dispatch and execution are recorded separately',
      welcomeEyebrow: 'INTELLIGENT SUBSEA OPERATIONS', welcomeTitle: 'Start with a question. Or a mission.',
      welcomeBody: 'Describe an operation, or ask about robots, equipment and conditions.',
      queryTitle: 'Knowledge & Status Query', queryBody: 'Explore robot capabilities, equipment specifications and status.', queryNote: 'Read-only · No task changes',
      taskTitle: 'Task Creation & Admission', taskBody: 'Collect parameters and check constraints before confirming a task.', taskNote: 'Publish after explicit confirmation',
      queryExample: 'What is the maximum operating depth of Taurus Unit 1?', taskExample: 'Create a subsea pipeline inspection task.',
      exampleLabel: 'Use example', exampleHint: 'Examples fill the composer; review and send when ready', details: 'Capabilities and usage guide',
      idle: 'Awaiting input', busy: 'Processing', chat: 'In conversation', query: 'Knowledge query · Read-only', task: 'Task planning', archived: 'Task archived', emergency: 'Emergency intervention',
      empty: 'Not started', collecting: 'Collecting', validating: 'Validating', confirming: 'Confirming', done: 'Archived', rejected: 'Closed',
      blocked_soft: 'Warning to review', blocked_hard: 'Conditions not met', steps: ['Parameters', 'Admission', 'Confirmation'],
      modifyPrompt: 'Modify task parameters: ', waiting: 'Submitting…', log: 'Conversation history', sidebar: 'Task and session', progress: 'Task workflow',
      changesTitle: 'Changes this turn', unavailable: 'State unavailable', errorTitle: 'Task state unavailable', errorBody: 'The returned task state is incomplete. Recover the session state before reviewing or publishing.',
      shortcutTask: 'Task', shortcutView: 'View task worksheet', shortcutReadOnly: 'Read-only', shortcutCollecting: 'Collecting',
      shortcutMissing: count => `${count} missing`, shortcutReview: count => `${count} to review`,
    },
  };
  const byId = id => document.getElementById(id);
  let language = 'zh';
  let latestState = null;
  let hasConversation = false;
  let publishing = false;
  let hadActiveTask = false;
  let interaction = { isSending: false, canSend: true, actions: {}, readOnly: false };
  const compactQuery = typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 760px)') : null;
  const isCompact = () => Boolean(compactQuery?.matches);
  const t = () => copy[language];
  const show = (id, visible) => { const el = byId(id); if (el) el.hidden = !visible; };
  const setText = (id, text) => { const el = byId(id); if (el) el.textContent = text; };
  const make = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };

  function setLanguage(lang) {
    language = lang === 'en' ? 'en' : 'zh';
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    document.querySelectorAll('[data-ui]').forEach(el => {
      const value = t()[el.dataset.ui];
      if (typeof value === 'string') el.textContent = value;
    });
    byId('messages')?.setAttribute('aria-label', t().log);
    byId('sidebar')?.setAttribute('aria-label', t().sidebar);
    byId('taskProgress')?.setAttribute('aria-label', t().progress);
    if (latestState) updateState(latestState, language, { origin: 'render' });
    else {
      setText('worksheetState', t().empty);
      updateTaskShortcut();
    }
    updateControls();
  }

  function capabilityCard(kind, number) {
    const card = make('article', `capability-card ${kind}`);
    const head = make('div', 'capability-card-head');
    const icon = make('span', 'capability-icon');
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = kind === 'query'
      ? '<svg viewBox="0 0 24 24"><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6M7 10h6M10 7v6"/></svg>'
      : '<svg viewBox="0 0 24 24"><path d="m12 2 9 5v10l-9 5-9-5V7Z"/><path d="m3 7 9 5 9-5M12 12v10"/></svg>';
    head.append(icon, make('span', 'capability-number', number));
    const example = make('button', 'example-button');
    example.type = 'button';
    example.dataset.example = t()[`${kind}Example`];
    example.setAttribute('aria-label', `${t().exampleLabel}: ${example.dataset.example}`);
    const arrow = make('span', 'example-arrow');
    arrow.dataset.icon = 'arrow-up-right';
    arrow.setAttribute('aria-hidden', 'true');
    example.append(make('span', '', example.dataset.example), arrow);
    card.append(head, make('h3', '', t()[`${kind}Title`]), make('p', '', t()[`${kind}Body`]),
      make('span', 'capability-note', t()[`${kind}Note`]), example);
    return card;
  }

  function renderWelcome(content, lang) {
    setLanguage(lang);
    const message = document.querySelector('.message[data-message-kind="welcome"]');
    const bubble = message?.querySelector('.bubble');
    if (!bubble) return;
    message.hidden = hasConversation;
    const layout = make('div', 'welcome-layout');
    const contours = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    contours.setAttribute('class', 'welcome-contours');
    contours.setAttribute('viewBox', '0 0 320 180');
    contours.setAttribute('aria-hidden', 'true');
    contours.setAttribute('focusable', 'false');
    // Static bathymetric linework: decorative branding, not live telemetry.
    contours.innerHTML = '<path d="M320 8C240-10 257 48 206 44S154 11 106 38 62 89 0 78M320 32C249 13 270 71 215 66S166 36 119 59 75 111 0 99M320 57C261 37 281 96 227 90S180 58 134 82 86 135 0 123M320 83C273 64 296 121 239 116S194 85 148 108 101 160 0 148M320 111C287 89 309 148 251 144S210 111 164 135 121 183 50 180"/>';
    const intro = make('div', 'welcome-intro');
    intro.append(make('span', 'welcome-eyebrow', t().welcomeEyebrow), make('h2', '', t().welcomeTitle), make('p', '', t().welcomeBody));
    const cards = make('div', 'capability-grid');
    cards.append(capabilityCard('query', '01'), capabilityCard('task', '02'));
    const details = make('details', 'welcome-details');
    details.append(make('summary', '', t().details));
    const guide = make('div', 'welcome-guide');
    if (window.SEAgentMarkdown) guide.innerHTML = window.SEAgentMarkdown.render(content, 'bot');
    else guide.textContent = content;
    details.append(guide);
    layout.append(contours, intro, cards, make('p', 'example-hint', t().exampleHint), details);
    bubble.replaceChildren(layout);
    if (!hasConversation) byId('messages').scrollTop = 0;
    layout.addEventListener('click', event => {
      const button = event.target.closest('[data-example]');
      if (!button || !interaction.canSend || interaction.isSending) return;
      const input = byId('messageInput');
      input.value = button.dataset.example;
      resizeComposer();
      input.focus();
    });
    updateControls();
  }

  function hasTask(data) {
    const ui = data?.ui_state;
    if (ui) return Boolean(ui.task_type_key || ui.task_id || ui.slots?.length);
    return Boolean(data?.task_type || data?.collected?.task_type || data?.collected?.task_id);
  }

  function updateTaskShortcut() {
    const shortcut = byId('mobileTaskShortcut');
    if (!shortcut) return;
    shortcut.hidden = !hasTask(latestState);
    shortcut.setAttribute('aria-expanded', String(Boolean(byId('taskWorksheet')?.open)));
    if (shortcut.hidden) {
      shortcut.textContent = '';
      shortcut.setAttribute('aria-label', t().shortcutView);
      positionLatestButton();
      return;
    }
    const ui = latestState.ui_state || {};
    const phase = ui.phase || latestState.phase;
    let state;
    if (ui.state_status === 'error') state = t().unavailable;
    else if (['done', 'rejected'].includes(phase)) state = t()[phase];
    else if (ui.read_only) state = t().shortcutReadOnly;
    else if (['blocked_soft', 'blocked_hard', 'confirming', 'validating'].includes(phase)) state = t()[phase];
    else if (phase === 'collecting') {
      const slots = Array.isArray(ui.slots) ? ui.slots : [];
      const missing = slots.filter(slot => slot.status === 'missing').length;
      const review = slots.filter(slot => !['valid', 'missing'].includes(slot.status)).length;
      state = [missing ? t().shortcutMissing(missing) : '', review ? t().shortcutReview(review) : '']
        .filter(Boolean).join(' · ') || t().shortcutCollecting;
    } else state = t().shortcutView;
    shortcut.textContent = `${t().shortcutTask} · ${state}`;
    shortcut.setAttribute('aria-label', `${t().shortcutView}${language === 'zh' ? '：' : ': '}${state}`);
    positionLatestButton();
  }

  function renderProgress(ui) {
    const progress = byId('taskProgress');
    if (!progress) return;
    progress.replaceChildren();
    const phase = ui.workflow_phase || ui.phase;
    if (phase === 'rejected') return;
    const current = { collecting: 0, validating: 1, blocked_soft: 1, blocked_hard: 1, confirming: 2, done: 3 }[phase] ?? 0;
    t().steps.forEach((label, index) => {
      const step = make('li', index < current ? 'complete' : index === current ? 'current' : '', label);
      if (index === current) step.setAttribute('aria-current', 'step');
      progress.append(step);
    });
  }

  function updateState(data, lang, options = {}) {
    language = lang === 'en' ? 'en' : 'zh';
    latestState = data;
    const ui = data.ui_state || {};
    const unavailable = ui.state_status === 'error';
    const active = !unavailable && hasTask(data);
    if (isCompact() && active && !hadActiveTask) {
      const sheet = byId('taskWorksheet');
      if (sheet) sheet.open = false;
    }
    hadActiveTask = active;
    const slots = Array.isArray(ui.slots) ? ui.slots : [];
    show('taskEmptyState', !active);
    show('taskDetails', active);
    const empty = byId('taskEmptyState');
    if (empty) {
      empty.querySelector('h3').textContent = unavailable ? t().errorTitle : t().emptyTitle;
      empty.querySelector('p').textContent = unavailable ? t().errorBody : t().emptyBody;
      empty.querySelector('.empty-task-note').hidden = unavailable;
    }
    const constraints = ui.constraint_state || {};
    const warnings = Boolean(constraints.error) || ['hard_violations', 'soft_warnings', 'ignored_soft_warnings'].some(key => constraints[key]?.length);
    show('constraintsCard', active && warnings);
    show('missingCard', active && (slots.some(slot => slot.status === 'missing') || (!data.ui_state && data.missing?.length > 0)));
    show('collectedCard', active && (slots.some(slot => slot.status !== 'missing') || (!data.ui_state && Object.keys(data.collected || {}).length > 0)));
    const valid = slots.filter(slot => slot.status === 'valid').length;
    setText('fieldCount', slots.length ? `${valid} / ${slots.length}` : '');
    setText('worksheetState', unavailable ? t().unavailable : active ? t()[ui.phase || data.phase] || t().task : t().empty);
    byId('worksheetState')?.setAttribute('data-phase', ui.phase || '');
    renderProgress(ui);
    show('confirmationCard', active && ui.phase === 'confirming' && !ui.read_only);
    window.SEAgentTaskInsights?.updateState(data, language, options);
    updateTaskShortcut();
    updateControls();
  }

  function publicationAllowed() {
    const ui = latestState?.ui_state;
    return Boolean(ui && ui.state_status !== 'error' && ui.phase === 'confirming' && !ui.read_only && !interaction.readOnly &&
      ui.actions?.can_publish && ui.actions?.can_confirm && interaction.actions?.can_publish &&
      interaction.actions?.can_confirm && interaction.canSend && !interaction.isSending && !publishing);
  }

  function updateControls() {
    const publish = byId('publishTaskBtn');
    if (publish) { publish.disabled = !publicationAllowed(); publish.textContent = publishing ? t().waiting : t().publish; }
    const modify = byId('modifyTaskBtn');
    if (modify) modify.disabled = !interaction.canSend || interaction.isSending || interaction.readOnly || !interaction.actions?.can_modify;
    document.querySelectorAll('[data-example]').forEach(button => { button.disabled = !interaction.canSend || interaction.isSending; });
    const mode = latestState?.ui_state?.dialogue_mode;
    const emergency = mode === 'emergency_intervention' || latestState?.ui_state?.mode === 'emergency';
    const status = interaction.isSending ? t().busy : latestState?.ui_state?.state_status === 'error' ? t().unavailable : emergency ? t().emergency : mode === 'knowledge_qa' ? t().query
      : latestState?.ui_state?.phase === 'done' ? t().archived : hasTask(latestState) ? t().task : hasConversation ? t().chat : t().idle;
    setText('interactionStatus', status);
    byId('interactionStatus')?.classList.toggle('is-busy', interaction.isSending);
    byId('interactionStatus')?.classList.toggle('is-emergency', emergency);
    byId('messages')?.setAttribute('aria-busy', String(interaction.isSending));
  }

  function setInteractionState(state) {
    interaction = { ...interaction, ...state };
    updateControls();
  }

  function onUserMessage() {
    hasConversation = true;
    window.SEAgentTaskInsights?.beginTurn();
    document.querySelectorAll('.message[data-message-kind="welcome"]').forEach(el => { el.hidden = true; });
    document.querySelector('.chat-area')?.classList.add('has-conversation');
    updateControls();
  }

  function reset(lang) {
    hasConversation = false;
    publishing = false;
    latestState = null;
    hadActiveTask = false;
    const worksheet = byId('taskWorksheet');
    if (worksheet) worksheet.open = true;
    window.SEAgentTaskInsights?.reset();
    interaction = { isSending: false, canSend: true, actions: {}, readOnly: false };
    document.querySelector('.chat-area')?.classList.remove('has-conversation');
    document.querySelectorAll('.message[data-message-kind="welcome"]').forEach(el => { el.hidden = false; });
    ['taskDetails', 'confirmationCard', 'constraintsCard', 'missingCard', 'collectedCard', 'jumpToLatestBtn', 'mobileTaskShortcut'].forEach(id => show(id, false));
    show('taskEmptyState', true);
    byId('taskEmptyState')?.querySelector('.empty-task-note')?.removeAttribute('hidden');
    byId('messages').scrollTop = 0;
    setLanguage(lang);
    resizeComposer();
  }

  function resizeComposer() {
    const input = byId('messageInput');
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = `${Math.min(168, Math.max(52, input.scrollHeight))}px`;
    positionLatestButton();
  }

  function positionLatestButton() {
    const messages = byId('messages'), chat = document.querySelector('.chat-area');
    if (!messages || !chat) return;
    // Anchor above the current composer, including the task shortcut and ASR panels.
    const offset = Math.max(10, chat.getBoundingClientRect().bottom - messages.getBoundingClientRect().bottom + 10);
    chat.style.setProperty('--latest-offset', `${Math.ceil(offset)}px`);
  }

  function isNearBottom() {
    const messages = byId('messages');
    return !messages || messages.scrollHeight - messages.clientHeight - messages.scrollTop < 100;
  }

  function followLatest(shouldFollow) {
    const messages = byId('messages');
    if (shouldFollow && messages) messages.scrollTop = messages.scrollHeight;
    show('jumpToLatestBtn', hasConversation && !isNearBottom());
  }

  function init() {
    byId('messageInput')?.addEventListener('input', resizeComposer);
    if (typeof ResizeObserver === 'function') {
      const observer = new ResizeObserver(positionLatestButton);
      [byId('messages'), document.querySelector('.composer')].filter(Boolean).forEach(el => observer.observe(el));
    }
    window.addEventListener('resize', positionLatestButton, { passive: true });
    byId('messages')?.addEventListener('scroll', () => show('jumpToLatestBtn', hasConversation && !isNearBottom()), { passive: true });
    byId('jumpToLatestBtn')?.addEventListener('click', () => followLatest(true));
    byId('taskWorksheet')?.addEventListener('toggle', updateTaskShortcut);
    byId('mobileTaskShortcut')?.addEventListener('click', () => {
      const shortcut = byId('mobileTaskShortcut'), sheet = byId('taskWorksheet');
      if (!sheet || !shortcut || shortcut.hidden || !shortcut.getClientRects().length || !hasTask(latestState)) return;
      sheet.open = true;
      updateTaskShortcut();
      sheet.scrollIntoView({ block: 'start' });
      sheet.querySelector('summary')?.focus({ preventScroll: true });
    });
    byId('publishTaskBtn')?.addEventListener('click', async () => {
      if (!publicationAllowed() || typeof window.sendMessage !== 'function') return;
      publishing = true;
      updateControls();
      try { await window.sendMessage('确认发布'); }
      finally { publishing = false; updateControls(); }
    });
    byId('modifyTaskBtn')?.addEventListener('click', () => {
      if (interaction.isSending || interaction.readOnly || !interaction.canSend || !interaction.actions?.can_modify) return;
      const input = byId('messageInput');
      if (!input.value.trim()) input.value = t().modifyPrompt;
      resizeComposer();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
    const setCompact = () => {
      const sheet = byId('taskWorksheet');
      if (sheet) sheet.open = !isCompact() || !hadActiveTask;
    };
    setCompact();
    compactQuery?.addEventListener('change', setCompact);
    setLanguage('zh');
    resizeComposer();
  }

  window.SEAgentWorkspace = { renderWelcome, setLanguage, updateState, setInteractionState, onUserMessage, reset, resizeComposer, isNearBottom, followLatest };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
