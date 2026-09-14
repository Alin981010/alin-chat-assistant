/* TEMP — 读真实前端渲染出来的 #engineState 文案与 tooltip。
   用于验证「密钥不匹配」是否真的显示在侧栏，而不是只存在于接口里。 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = process.env.BASE || 'http://127.0.0.1:8088';
const CDP_PORT = 9338;
const PROFILE = path.join(os.tmpdir(), 'alinagent-label-' + Date.now());
const EXPECT = process.env.EXPECT || '';
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--remote-debugging-port=' + CDP_PORT, '--user-data-dir=' + PROFILE, 'about:blank'], { stdio: 'ignore' });
  let ws;
  try {
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      await sleep(250);
      try { target = (await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()).find(t => t.type === 'page'); } catch (e) {}
    }
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise(r => ws.addEventListener('open', r));
    let id = 0; const pending = new Map();
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
    });
    const send = (method, params) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params: params || {} })); });
    const js = async e => (await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true })).result.value;

    await send('Runtime.enable'); await send('Page.enable');
    await send('Page.navigate', { url: BASE + '/' });
    await sleep(4000);

    const text = await js("document.querySelector('#engineState').textContent.trim()");
    const title = await js("document.querySelector('#engineState').title");
    const cursor = await js("document.querySelector('#engineState').style.cursor");
    console.log('侧栏文案 :', JSON.stringify(text));
    console.log('tooltip  :', JSON.stringify(title.slice(0, 90) + (title.length > 90 ? '…' : '')));
    console.log('cursor   :', JSON.stringify(cursor));

    const ok = EXPECT ? text.includes(EXPECT) : text.length > 0;
    // 可用时 code=ok、reason 为空 —— 此时没有 tooltip 才是正确的
    const healthy = /sandbox ·/.test(text);
    const titleOk = healthy ? title === '' : title.length > 0;
    console.log((ok ? 'PASS  ' : 'FAIL  ') + `文案${EXPECT ? '包含 ' + EXPECT : '非空'}`);
    console.log((titleOk ? 'PASS  ' : 'FAIL  ') + (healthy ? '可用时不挂 tooltip' : '不可用原因挂在 tooltip 上'));
    process.exitCode = ok && titleOk ? 0 : 1;
  } finally {
    try { ws && ws.close(); } catch (e) {}
    chrome.kill(); await sleep(300);
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}
  }
})();
