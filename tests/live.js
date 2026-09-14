/* TEMP verification — 真实后端 + 真实浏览器（不再用 mock）。
   指向正在运行的 uvicorn（默认 8088），验证 static 挂载、真实 /api/*、以及一轮真实对话。 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = process.env.BASE || 'http://127.0.0.1:8088';
const CDP_PORT = 9336;
const PROFILE = path.join(os.tmpdir(), 'alinagent-live-' + Date.now());
const SEND_CHAT = process.env.SEND_CHAT !== '0';

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log((ok ? 'PASS  ' : 'FAIL  ') + name + (detail !== undefined ? '   [' + detail + ']' : ''));
}

async function main() {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--remote-debugging-port=' + CDP_PORT,
    '--user-data-dir=' + PROFILE, 'about:blank'
  ], { stdio: 'ignore' });

  const errors = [];
  let ws, send;
  try {
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      await sleep(250);
      try {
        const list = await (await fetch('http://127.0.0.1:' + CDP_PORT + '/json/list')).json();
        target = list.find(t => t.type === 'page');
      } catch (e) {}
    }
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });

    let msgId = 0;
    const pending = new Map();
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const { resolve, reject } = pending.get(m.id);
        pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
        return;
      }
      if (m.method === 'Runtime.exceptionThrown') {
        errors.push('exception: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        errors.push('console.error: ' + m.params.args.map(a => a.value ?? a.description).join(' '));
      }
      if (m.method === 'Network.loadingFailed') errors.push('loadFailed: ' + m.params.errorText);
      if (m.method === 'Network.responseReceived' && m.params.response.status >= 400) {
        errors.push('HTTP ' + m.params.response.status + ' ' + m.params.response.url);
      }
    });
    send = (method, params) => new Promise((resolve, reject) => {
      const id = ++msgId;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params: params || {} }));
    });

    await send('Runtime.enable'); await send('Log.enable');
    await send('Page.enable'); await send('Network.enable');
    const js = async expr => {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };
    const waitFor = async (expr, timeoutMs, label) => {
      const t0 = Date.now();
      while (Date.now() - t0 < timeoutMs) {
        try { if (await js(expr)) return true; } catch (e) {}
        await sleep(1500);
      }
      return false;
    };

    /* ---------- 1. 静态资源 + 启动 ---------- */
    await send('Page.navigate', { url: BASE + '/' });
    // 等前端真正启动完成（engineState 是 /api/sandbox/status 回来后才写入的），
    // 固定 sleep 在机器忙的时候会假失败。
    const booted = await waitFor("!!document.querySelector('.hero h1 .line .ch') && (document.querySelector('#engineState')||{}).textContent.trim().length > 0", 20000);
    check('前端启动完成（hero + 引擎状态均已渲染）', booted);

    check('样式表已生效（/static/css/app.css 200 + 应用）',
      await js("getComputedStyle(document.body).backgroundColor") === 'rgb(239, 226, 200)',
      await js("getComputedStyle(document.body).backgroundColor"));
    check('app.js 已执行（hero 逐字渲染）', await js("!!document.querySelector('.hero h1 .line .ch')"));
    check('没有 4xx/5xx 与加载失败', errors.length === 0, errors.slice(0, 4).join(' | '));

    /* ---------- 2. 真实 /api/* ---------- */
    check('真实 /api/threads 返回空列表 → 侧栏空态',
      await js("!!document.querySelector('#sessionList .rail-empty')"),
      await js("(document.querySelector('#sessionList .rail-empty')||{}).textContent || '(无空态)'"));
    const engine = await js("document.querySelector('#engineState').textContent");
    check('真实 /api/sandbox/status → 显示沙箱已就绪', /sandbox ·/.test(engine), engine);
    check('引擎状态未标记 degraded',
      !(await js("document.querySelector('#engineState').classList.contains('degraded')")));

    /* ---------- 3. 一轮真实对话 ---------- */
    if (SEND_CHAT) {
      const before = Date.now();
      await js("(()=>{const i=document.querySelector('#input'); i.value='用一句话介绍你自己'; i.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('#send').click();})()");
      const got = await waitFor(
        "(document.querySelector('#stage .thread .msg-ai .body')||{}).textContent?.trim().length > 0 && !document.querySelector('#send').classList.contains('sending')",
        150000
      );
      const reply = await js("((document.querySelector('#stage .thread .msg-ai .body')||{}).textContent||'').trim()");
      check('真实 SSE 流式对话返回正文', got && reply.length > 0, `${Math.round((Date.now() - before) / 1000)}s, ${reply.length} 字`);
      console.log('      回复: ' + reply.slice(0, 120).replace(/\n/g, ' '));
      check('有正文且不是错误态',
        !(await js("(document.querySelector('#stage .thread .msg-ai .body')||{}).classList?.contains('is-error')")),
        await js("((document.querySelector('#stage .thread .msg-ai .body')||{}).textContent||'').slice(0,80)"));

      /* ---------- 4. 刷新后会话能恢复（真实 DB 持久化 + 本地身份） ---------- */
      await send('Page.navigate', { url: BASE + '/' });
      await waitFor("document.querySelectorAll('#stage .thread .msg').length >= 2", 20000);
      const restored = await js("document.querySelectorAll('#stage .thread .msg').length");
      check('刷新后从 /api/history 恢复会话', restored >= 2, `msg=${restored}`);
      check('侧栏出现该会话（来自 /api/threads）',
        await js("document.querySelectorAll('#sessionList .session-item').length") >= 1,
        await js("document.querySelectorAll('#sessionList .session-item').length"));
      check('列表标题取自 ThreadInfo.last_message',
        /介绍你自己/.test(await js("(document.querySelector('#sessionList .session-item .t')||{}).textContent||''")),
        await js("(document.querySelector('#sessionList .session-item .t')||{}).textContent||''"));
    }

    check('全流程零浏览器错误', errors.length === 0, errors.slice(0, 4).join(' | '));
  } catch (e) {
    check('harness 正常结束', false, e.message);
  } finally {
    try { ws && ws.close(); } catch (e) {}
    chrome.kill();
    await sleep(300);
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}
  }
  const failed = results.filter(r => !r.ok);
  console.log('\n' + (results.length - failed.length) + '/' + results.length + ' checks passed');
  process.exit(failed.length ? 1 : 0);
}
main();
