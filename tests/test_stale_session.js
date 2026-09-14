/* 前端「过期会话自愈」验证：用 Node 的 vm 把真实的 static/js/app.js 跑起来。
 *
 * 为什么要有这个：`tests/e2e.js` 需要 headless Chrome，在受限环境里起不来；
 * 而这条自愈路径恰恰是「身份迁移后老浏览器卡在无权访问」的修复，必须有回归。
 * 这里不重新实现逻辑，而是加载**真实产物**，只把 DOM 与 fetch 换成可控的桩。
 *
 * 用法：node tests/test_stale_session.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = path.join(__dirname, '..', 'static', 'js', 'app.js');
const source = fs.readFileSync(APP_JS, 'utf8');

const results = [];
function check(name, ok, detail) {
  results.push(!!ok);
  console.log((ok ? 'PASS  ' : 'FAIL  ') + name + (detail !== undefined ? '   [' + detail + ']' : ''));
}

/* ---------- 最小 DOM 桩 ---------- */
function makeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    childNodes: [],
    parentNode: null,
    style: {},
    dataset: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) { this._s.add(c); } else { this._s.delete(c); } },
      contains(c) { return this._s.has(c); }
    },
    _html: '', _text: '',
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    className: '', value: '', hidden: false, open: false, title: '', type: '',
    scrollHeight: 0, scrollTop: 0, clientWidth: 0, clientHeight: 0, offsetWidth: 0, offsetHeight: 0,
    appendChild(c) { this.children.push(c); this.childNodes.push(c); c.parentNode = this; return c; },
    insertBefore(c) { this.children.unshift(c); this.childNodes.unshift(c); c.parentNode = this; return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); this.childNodes = this.childNodes.filter(x => x !== c); return c; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    /* 返回一个新桩而不是 null：真实 DOM 里 querySelector('.del') 之类都是有节点的，
       返回 null 会让 app.js 的会话列表渲染在异步里崩掉，测试就测不到后面的断言。 */
    querySelector() { return makeEl('div'); },
    querySelectorAll() { return []; },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    insertAdjacentHTML() {},
    closest() { return null; },
    contains() { return false; },
    focus() {},
    blur() {},
    click() {},
    getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  };
  return el;
}

const registry = new Map();
function stubElement(id) {
  if (!registry.has(id)) registry.set(id, makeEl('div'));
  return registry.get(id);
}

const documentStub = {
  body: makeEl('body'),
  documentElement: makeEl('html'),
  createElement: makeEl,
  getElementById: stubElement,
  querySelector: sel => (String(sel).startsWith('#') ? stubElement(String(sel).slice(1)) : makeEl('div')),
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
  readyState: 'complete'
};

/* ---------- 可控的 localStorage ---------- */
const storage = new Map();
const localStorageStub = {
  getItem: k => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => { storage.set(k, String(v)); },
  removeItem: k => { storage.delete(k); },
  clear: () => storage.clear()
};

/* ---------- fetch 桩：旧会话返回 403 = ---------- */
const calls = [];
function makeFetch(opts) {
  return async function fetchStub(url, init) {
    const u = String(url);
    calls.push({ url: u, body: init && init.body ? String(init.body) : null });
    if (u.includes('/api/identity')) {
      return jsonResponse(200, { org_id: 'onewidentity000000000000', user_id: 'unewidentity000000000000' });
    }
    if (u.includes('/api/history/')) {
      if (opts.historyStatus !== 200) {
        return jsonResponse(opts.historyStatus, { detail: opts.historyDetail });
      }
      /* 200 时必须带上 messages：restoreActiveThread 对「没有消息的会话」本来就会
         清掉 activeThread（空会话不该被恢复），那是既有行为，不是本测试要测的点。 */
      return jsonResponse(200, {
        thread_id: u.split('/api/history/')[1],
        messages: [
          { role: 'user', content: '你好', reasoning: null },
          { role: 'assistant', content: '你好，有什么可以帮你？', reasoning: null }
        ]
      });
    }
    if (u.includes('/api/threads')) return jsonResponse(200, []);
    if (u.includes('/api/usage')) return jsonResponse(200, { tokens_used: 0, tokens_budget: 0 });
    if (u.includes('/api/sandbox/status')) return jsonResponse(200, { available: false, code: 'not_initialized' });
    if (u.includes('/api/config')) return jsonResponse(200, { code_execution: false, assistant_name: '阿林对话助手' });
    if (u.includes('/api/chat/stream') || u.includes('/api/chat-with-file/stream')) {
      return jsonResponse(opts.chatStatus, { detail: opts.chatDetail });
    }
    return jsonResponse(404, {});
  };
}
function jsonResponse(status, obj) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => obj,
    text: async () => JSON.stringify(obj),
    body: null
  };
}

function runApp(opts) {
  storage.clear();
  calls.length = 0;
  // 模拟：身份迁移前的老浏览器——localStorage 里是 default-org 身份 + 旧会话
  for (const [k, v] of Object.entries(opts.storage || {})) storage.set(k, v);

  const sandbox = {
    console,
    document: documentStub,
    localStorage: localStorageStub,
    fetch: makeFetch(opts),
    location: { href: 'http://localhost/', origin: 'http://localhost', pathname: '/', search: '', hash: '' },
    navigator: { userAgent: 'node', language: 'zh-CN', clipboard: null },
    crypto: { randomUUID: () => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' },
    setTimeout: (fn) => { try { fn(); } catch (e) { /* 忽略动画回调 */ } return 0; },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: (fn) => { try { fn(0); } catch (e) {} return 0; },
    cancelAnimationFrame: () => {},
    performance: { now: () => Date.now() },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    innerWidth: 1280, innerHeight: 800,
    addEventListener() {}, removeEventListener() {},
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    Intl, JSON, Math, Date, String, Number, Boolean, Array, Object, Error, Promise, RegExp, Set, Map,
    TextDecoder: require('util').TextDecoder,
    WebSocket: function () { this.addEventListener = () => {}; },
    URLSearchParams
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;

  const ctx = vm.createContext(sandbox);
  try {
    vm.runInContext(source, ctx, { filename: 'app.js' });
  } catch (e) {
    return { bootError: e.message, calls, storage };
  }
  /* app.js 结尾会立即调 boot()，而 boot 内部是 async（要 await loadIdentity /
     restoreActiveThread）。断言前必须让这条微任务链跑完，否则测到的是「还没开始」。 */
  return { calls, storage };
}

async function runAppSettled(opts) {
  const r = runApp(opts);
  await new Promise(res => setImmediate(res));
  await new Promise(res => setImmediate(res));
  await new Promise(res => setImmediate(res));
  return r;
}

/* ---------- 主流程 ---------- */
(async function main() {
  const ID_KEY = 'alinchat.identity.v1';
  const ACTIVE_KEY = 'alinchat.activeThread.v1';
  const LEGACY_ACTIVE = 'default-org__u326862175996__mu1dkj7w';

  // 场景 1：localStorage 里是迁移前的身份 + 旧会话，恢复时必须被判为过期并清掉
  const r1 = await runAppSettled({
    storage: {
      [ID_KEY]: JSON.stringify({ org_id: 'default-org', user_id: 'u326862175996' }),
      [ACTIVE_KEY]: JSON.stringify(LEGACY_ACTIVE)
    },
    historyStatus: 403,
    historyDetail: '该会话不属于当前身份（可能是早期版本的旧会话，已作废）。请新建一个会话继续。',
    chatStatus: 403,
    chatDetail: '该会话不属于当前身份（可能是早期版本的旧会话，已作废）。请新建一个会话继续。'
  });
  if (r1.bootError) check('app.js 能在桩环境里启动', false, r1.bootError);
  else check('app.js 能在桩环境里启动（无 DOM 依赖错误）', true);

  const askedHistory = r1.calls.some(c => c.url.includes('/api/history/' + LEGACY_ACTIVE));
  check('启动时确实尝试恢复旧会话（否则这个测试没测到东西）', askedHistory,
    r1.calls.map(c => c.url).filter(u => u.includes('history')).join(','));

  const activeAfter = r1.storage.get(ACTIVE_KEY);
  check('旧会话被判定为过期后，本地 activeThread 被清掉',
    activeAfter === null || activeAfter === 'null' || !String(activeAfter).includes('default-org'),
    activeAfter);

  const identityAfter = r1.storage.get(ID_KEY);
  check('本地身份被换成服务端下发的那份',
    !!identityAfter && String(identityAfter).includes('onewidentity'), identityAfter);

  // 场景 2：正常会话不该被误伤（历史 200）
  const r2 = await runAppSettled({
    storage: {
      [ID_KEY]: JSON.stringify({ org_id: 'onewidentity000000000000', user_id: 'unewidentity000000000000' }),
      [ACTIVE_KEY]: JSON.stringify('onewidentity000000000000__unewidentity000000000000__good0001')
    },
    historyStatus: 200,
    chatStatus: 200
  });
  const stillThere = r2.storage.get(ACTIVE_KEY);
  check('正常会话不被误清（避免自愈逻辑过度触发）',
    !!stillThere && String(stillThere).includes('good0001'), stillThere);

  const total = results.length;
  const passed = results.filter(Boolean).length;
  console.log('\n' + passed + '/' + total + ' 通过');
  process.exit(passed === total ? 0 : 1);
})();
