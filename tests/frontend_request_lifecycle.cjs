// Run the application's actual request functions with a controlled network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../frontend/js/index.js'), 'utf8');
function extract(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `Missing application function: ${start}`);
  return source.slice(first, last);
}
const functions = [
  extract('    function cancelActiveRequest(', '    /**'),
  extract('    async function restoreHistory(', '    async function sendMessage('),
  extract('    async function sendMessage(', '    async function restoreSessionFromStorage('),
  extract('    async function refreshSessionStateAfterReload(', '    async function pollReloadEvents('),
  extract('    function buildPayloadSections(', '    function renderPayloadSelector('),
].join('\n');

function createClient() {
  const context = vm.createContext({AbortController, TextDecoder, TextEncoder, console});
  vm.runInContext(`
    let isSending = false, isDone = false, currentReadOnly = false;
    let currentActions = {can_send: true}, currentRequestSeq = 0;
    let sessionGeneration = 0, currentAbortController = null, sessionId = 'sid';
    let reloadStateRequestSeq = 0, lastResponseData = null;
    const API_BASE = '', currentLang = 'zh';
    const window = {ReadableStream: true};
    const I18N = {zh: {networkError: 'network error'}};
    const messageInput = {value: '', focus() {}};
    const messageContainer = {innerHTML: '', scrollTop: 0, scrollHeight: 0};
    const localStorage = {setItem() {}};
    const requests = [], messages = [], renders = [], updates = [], interactionStates = [];
    function applyInteractionState() { interactionStates.push({sending: isSending}); }
    function renderDecisionCardInBubble() {}
    function updateSidebar(data) { updates.push(data); }
    function renderMessageContent(text, role) { renders.push({text, role}); return text; }
    function addMessage(role, text) {
      messages.push({role, text});
      const bubble = {innerHTML: ''};
      return {dataset: {}, querySelector() { return bubble; }, setAttribute() {}};
    }
    function fetch(url, options = {}) {
      return new Promise((resolve, reject) => {
        requests.push({url, options, resolve, reject});
        options.signal?.addEventListener('abort', () => {
          reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
        });
      });
    }
    ${functions}
    globalThis.client = {
      sendMessage, restoreHistory, cancelActiveRequest, requests, messages, renders, updates,
      refreshSessionStateAfterReload, interactionStates,
      setSession(id, generation = sessionGeneration) { sessionId = id; sessionGeneration = generation; },
      disableStreams() { window.ReadableStream = false; },
      session() { return sessionId; },
      payloadSections(taskType, slot) {
        lastResponseData = {ui_state: {task_type_key: taskType}};
        return buildPayloadSections(slot);
      },
      locked() { return isSending; },
      streamResponse(events, readError = null) {
        let consumed = false;
        return {
          ok: true, headers: {get() { return 'text/event-stream'; }},
          body: {getReader() { return {
            async read() {
              if (consumed) {
                if (readError) throw new Error(readError);
                return {done: true};
              }
              consumed = true;
              return {done: false, value: new TextEncoder().encode(
                events.map(event => 'data: ' + JSON.stringify(event) + '\\n\\n').join('')
              )};
            },
            cancel() {},
          }; }},
        };
      },
    };
  `, context);
  return context.client;
}

async function staleStreamPreservesHistoryLock() {
  const client = createClient();
  const previous = client.sendMessage('old request');
  const restore = client.restoreHistory('history-id');
  await previous;
  assert.equal(client.locked(), true, 'aborted SSE must preserve history restore lock');
  await client.sendMessage('card action while restoring');
  assert.equal(client.requests.length, 2, 'history restore must block additional sends');
  client.cancelActiveRequest();
  await restore;
  assert.equal(client.locked(), false);
}

async function successfulStreamReleasesLock() {
  const client = createClient();
  const first = client.sendMessage('first');
  client.requests[0].resolve(client.streamResponse([
    {delta: 'draft'}, {delta: ' text'},
    {code: 200, ui_state: {phase: 'collecting'}, reply: 'final reply'},
  ]));
  await first;
  assert.equal(client.locked(), false, 'completed SSE must allow the next message');
  assert.equal(client.renders.at(-1).text, 'final reply');
  assert(client.renders.every(render => render.role === 'bot'));
  assert.equal(client.updates.length, 1);
  const next = client.sendMessage('next');
  assert.equal(client.requests.length, 2);
  client.cancelActiveRequest();
  await next;
}

async function streamErrorReleasesLock() {
  const client = createClient();
  const request = client.sendMessage('first');
  client.requests[0].resolve(client.streamResponse([
    {error: 'SERVER_ERROR', msg: 'failed'},
  ]));
  await request;
  assert.equal(client.locked(), false, 'SSE error must release its own lock');
  assert.equal(client.requests.length, 1, 'handled SSE error must not resend the message');
  assert(client.messages.some(message => message.text.includes('SERVER_ERROR')));
}

async function settleNetwork() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}

async function interruptedStreamDoesNotResend() {
  for (const mode of ['eof', 'reader-error', 'fetch-error', 'non-stream', 'http-error']) {
    const client = createClient();
    client.setSession(null);
    const request = client.sendMessage('确认');
    if (mode === 'fetch-error') {
      client.requests[0].reject(new Error('connection lost'));
    } else if (mode === 'non-stream' || mode === 'http-error') {
      client.requests[0].resolve({
        ok: mode === 'non-stream', status: mode === 'non-stream' ? 200 : 500,
        headers: {get() { return 'application/json'; }},
      });
    } else {
      client.requests[0].resolve(client.streamResponse([
        {status: 'connected', session_id: 'created-session'}, {delta: '已处理确认'},
      ], mode === 'reader-error' ? 'connection lost' : null));
    }
    await settleNetwork();
    assert.equal(client.requests.length, 1, mode + ' must not repeat a possibly applied mutation');
    await request;
    assert.equal(client.locked(), false, mode + ' must restore interaction');
    assert.equal(client.interactionStates.at(-1).sending, false);
    assert(client.messages.some(message => message.text.includes('未自动重发')), mode + ' needs an explicit interruption notice');
    if (mode === 'eof' || mode === 'reader-error') {
      assert.equal(client.session(), 'created-session', 'the stream session must remain recoverable');
    }
  }
}

async function unsupportedEndpointCanFallback() {
  for (const status of [404, 405]) {
    const client = createClient();
    const request = client.sendMessage('first');
    client.requests[0].resolve({ok: false, status, headers: {get() { return 'text/html'; }}});
    await settleNetwork();
    assert.equal(client.requests.length, 2);
    assert.equal(client.requests[1].url, '/api/chat');
    client.requests[1].resolve({ok: true, status: 200, async text() {
      return JSON.stringify({code: 200, reply: 'ok', ui_state: {phase: 'collecting'}});
    }});
    await request;
    assert.equal(client.updates.length, 1);
    assert.equal(client.locked(), false);
  }
}

async function clientWithoutStreamsUsesSyncOnce() {
  const client = createClient();
  client.disableStreams();
  const request = client.sendMessage('你好');
  assert.equal(client.requests.length, 1);
  assert.equal(client.requests[0].url, '/api/chat');
  client.requests[0].resolve({ok: true, status: 200, async text() {
    return JSON.stringify({code: 200, reply: '你好', ui_state: {phase: 'chatting'}});
  }});
  await request;
  assert.equal(client.updates.length, 1);
  assert.equal(client.locked(), false);
}

async function reloadRefreshRespectsSessionAndRequest() {
  for (const invalidate of [
    client => client.setSession(null, 1),
    client => client.setSession('different-session'),
    client => client.cancelActiveRequest(),
  ]) {
    const client = createClient();
    const request = client.refreshSessionStateAfterReload();
    invalidate(client);
    client.requests[0].resolve({ok: true, async json() {
      return {ok: true, exists: true, session_id: 'sid', ui_state: {phase: 'done'}};
    }});
    await request;
    assert.equal(client.updates.length, 0, 'outdated reload response must not change the sidebar');
  }
  const client = createClient();
  const older = client.refreshSessionStateAfterReload();
  const newer = client.refreshSessionStateAfterReload();
  client.requests[1].resolve({ok: true, async json() {
    return {ok: true, exists: true, ui_state: {phase: 'collecting'}};
  }});
  await newer;
  client.requests[0].resolve({ok: true, async json() {
    return {ok: true, exists: true, ui_state: {phase: 'done'}};
  }});
  await older;
  assert.equal(client.updates.length, 1, 'latest reload refresh wins');
  assert.equal(client.updates[0].ui_state.phase, 'collecting');
}

function payloadToolsFollowActualTaskState() {
  const client = createClient();
  const slot = {key: 'payload', allowed_values: ['高压水射流喷冲埋设模块'],
    payload_groups: {Operation_tool: ['高压水射流喷冲埋设模块']}, onboard_payload_groups: {}};
  const burial = client.payloadSections('pipeline_burial', slot);
  assert.equal(burial.find(section => section.key === 'Operation_tool').options[0], '高压水射流喷冲埋设模块');
  for (const taskType of ['pipeline_inspection', null]) {
    assert(!client.payloadSections(taskType, slot).some(section => section.key === 'Operation_tool'));
  }
}

(async () => {
  await staleStreamPreservesHistoryLock();
  await successfulStreamReleasesLock();
  await streamErrorReleasesLock();
  await interruptedStreamDoesNotResend();
  await unsupportedEndpointCanFallback();
  await clientWithoutStreamsUsesSyncOnce();
  await reloadRefreshRespectsSessionAndRequest();
  payloadToolsFollowActualTaskState();
  console.log('Frontend request lifecycle scenarios passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
