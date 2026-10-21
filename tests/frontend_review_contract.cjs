// Run production time and translation logic without a browser or live service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/index.js', 'utf8');
function extract(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first);
  return source.slice(first, last);
}

async function businessTimeRoundTrip(currentTime) {
  const context = vm.createContext({ Date, Intl, console });
  vm.runInContext(`
    const currentLang = 'zh', API_BASE = '';
    let isTimePickerEditing = false;
    const I18N = {zh: {simtimeHint: '设置基准时间'}};
    const elements = {timePicker: {value: ''}, simulatedTimeDisplay: {}, 'hint-simtime': {}};
    const document = {getElementById(id) {return elements[id];}};
    let time = ${JSON.stringify(currentTime)};
    const posts = [];
    const fetch = async (url, options) => {
      if (options) posts.push(JSON.parse(options.body));
      return {json: async () => ({code: 200, current_time: time})};
    };
    function alert() {}
    ${extract('    async function updateSimulatedTime(', '    function setAsrStatus(')}
    globalThis.test = {updateSimulatedTime, setSimulatedTime, elements, posts};
  `, context);
  await context.test.updateSimulatedTime(true);
  assert.equal(context.test.elements.timePicker.value, '2026-10-20T18:30:45');
  assert(context.test.elements['hint-simtime'].innerText.includes('UTC+08:00'));
  await context.test.setSimulatedTime();
  assert.equal(context.test.posts[0].time, '2026-10-20T18:30:45+08:00');
}

async function staleTranslationCannotReplaceFinalReply(manual, switchLanguage) {
  const context = vm.createContext({});
  vm.runInContext(`
    let currentLang = ${JSON.stringify(manual ? 'zh' : 'en')};
    const I18N = {zh: {}, en: {}};
    const bubble = {innerHTML: '最终答复'};
    const attrs = {'data-original': '处理中', 'data-role': 'bot'};
    const message = {
      dataset: {}, isConnected: true,
      getAttribute(key) {return attrs[key];},
      setAttribute(key, value) {attrs[key] = value;},
      querySelector(selector) {return selector === '.bubble' ? bubble : button;}
    };
    const button = {
      getAttribute() {return 'false';}, setAttribute() {},
      closest(selector) {return selector === '.message' ? message : {querySelector() {return bubble;}};}
    };
    let finish;
    const fetchTranslation = () => new Promise(resolve => finish = resolve);
    function renderMessageContent(text) {return text;}
    function hasChinese() {return true;}
    ${extract('    async function autoTranslateMessage(', '    function escapeHtml(')}
    globalThis.test = {
      start: () => ${manual ? 'toggleMessageTranslation(button)' : 'autoTranslateMessage(message)'},
      change() {${switchLanguage ? "currentLang = 'en';" : "attrs['data-original'] = '最终答复';"}},
      finish() {finish({translated: 'Outdated translation'});},
      bubble
    };
  `, context);
  const work = context.test.start();
  context.test.change();
  context.test.finish();
  await work;
  assert.equal(context.test.bubble.innerHTML, '最终答复');
}

(async () => {
  for (const currentTime of [
    '2026-10-20T18:30:45+08:00', '2026-10-20T10:30:45Z', '2026-10-20T18:30:45',
  ]) await businessTimeRoundTrip(currentTime);
  await staleTranslationCannotReplaceFinalReply(false, false);
  await staleTranslationCannotReplaceFinalReply(true, false);
  await staleTranslationCannotReplaceFinalReply(true, true);
  console.log('Business-time round trips and stale translation guards passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
