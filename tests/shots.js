/* TEMP — screenshot pass over the wired UI. Deleted after use. */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = Number(process.env.PORT || 8099);
const CDP_PORT = 9334;
const PROFILE = path.join(os.tmpdir(), 'alinagent-shot-' + Date.now());
const SAMPLE = path.join(__dirname, 'sample.csv');
const OUT = __dirname;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--force-device-scale-factor=1', '--window-size=1440,940',
    '--remote-debugging-port=' + CDP_PORT, '--user-data-dir=' + PROFILE, 'about:blank'
  ], { stdio: 'ignore' });

  let ws;
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
    await new Promise(r => ws.addEventListener('open', r));
    let id = 0; const pending = new Map();
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
    });
    const send = (method, params) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params: params || {} })); });
    const js = async e => (await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true })).result.value;

    await send('Runtime.enable'); await send('Page.enable'); await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 940, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/' });
    await sleep(2200);

    const shot = async (name) => {
      const r = await send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(OUT, name), Buffer.from(r.data, 'base64'));
      console.log('wrote', name);
    };

    /* 1 — hero + rail */
    await shot('shot-1-boot.png');

    /* 2 — a finished turn with reasoning + tool trail（实时视图） */
    await js("(()=>{const i=document.querySelector('#input'); i.value='你能做什么？'; i.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('#send').click();})()");
    // 等这一轮真的结束再截图/刷新：抢在流式中间会让回放缺一条 assistant 消息
    for (let i = 0; i < 60; i++) {
      const busy = await js("document.querySelector('#send').classList.contains('sending')");
      if (!busy) break;
      await sleep(1000);
    }
    await sleep(800);
    await js("window.__liveCount = document.querySelectorAll('#stage .thread .msg').length");
    console.log('  实时消息数:', await js("window.__liveCount"));
    await js("document.querySelector('#stage .thread .msg-ai .reason').open = true; document.querySelector('#stage').scrollTop = document.querySelector('#stage').scrollHeight;");
    await sleep(400);
    await shot('shot-2-turn.png');

    /* 2b — 刷新后回放：思考过程是否还在、用户气泡是否干净（走 /api/history） */
    await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/' });
    await sleep(4000);
    console.log('  回放消息数:', await js("document.querySelectorAll('#stage .thread .msg').length"));
    await js("(()=>{const r=document.querySelector('#stage .thread .msg-ai .reason'); if(r) r.open = true; document.querySelector('#stage').scrollTop = 0;})()");
    await sleep(400);
    await shot('shot-5-replay.png');

    /* 3 — upload modal with a parsed file, plus the attachment bar */
    await js("document.querySelector('#uploadBtn').click()");
    await sleep(500);
    const obj = await send('Runtime.evaluate', { expression: "document.querySelector('#fileInput')" });
    await send('DOM.enable');
    await send('DOM.setFileInputFiles', { files: [SAMPLE], objectId: obj.result.objectId });
    await sleep(300);
    await js("document.querySelector('#uploadGo').click()");
    // 等真的解析完（真实后端还要把文件传进沙箱，比 mock 慢），别抢拍
    for (let i = 0; i < 40; i++) {
      if (await js("document.querySelectorAll('#attachList .attach').length > 0")) break;
      await sleep(500);
    }
    await sleep(400);
    await shot('shot-3-upload.png');

    await js("document.querySelector('#uploadClose').click()");
    await sleep(600);
    await shot('shot-4-attach.png');
  } finally {
    try { ws && ws.close(); } catch (e) {}
    chrome.kill();
    await sleep(300);
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) {}
  }
}
main();
