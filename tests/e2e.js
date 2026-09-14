/* 前端契约测试：headless Chrome 经 CDP 驱动真实 static/ 前端，
   打的是 tests/mock-backend.js（按 app/routes.py 契约实现的假后端）。
   不需要真后端、不烧 token。
   用法：先 `node tests/mock-backend.js`，再 `node tests/e2e.js`。 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = Number(process.env.PORT || 8099);
const CDP_PORT = 9333;
const PROFILE = path.join(os.tmpdir(), 'alinagent-e2e-' + Date.now());
const SAMPLE = path.join(__dirname, 'sample.csv');

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });
  console.log((ok ? 'PASS  ' : 'FAIL  ') + name + (detail !== undefined ? '   [' + detail + ']' : ''));
}

async function main() {
  fs.writeFileSync(SAMPLE, 'id,name,score\n1,alpha,91\n2,beta,77\n3,gamma,88\n');

  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--remote-debugging-port=' + CDP_PORT,
    '--user-data-dir=' + PROFILE, 'about:blank'
  ], { stdio: 'ignore' });

  const errors = [];
  let ws, send;

  try {
    /* wait for the devtools endpoint */
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      await sleep(250);
      try {
        const list = await (await fetch('http://127.0.0.1:' + CDP_PORT + '/json/list')).json();
        target = list.find(t => t.type === 'page');
      } catch (e) { /* not up yet */ }
    }
    if (!target) throw new Error('chrome devtools endpoint never came up');

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
      if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
        errors.push('log: ' + m.params.entry.text + ' @ ' + (m.params.entry.url || ''));
      }
    });
    send = (method, params) => new Promise((resolve, reject) => {
      const id = ++msgId;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params: params || {} }));
    });

    await send('Runtime.enable');
    await send('Log.enable');
    await send('Page.enable');
    await send('DOM.enable');

    const evalJs = async (expression) => {
      const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };

    await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/' });
    await sleep(2000);

    /* ---------- 1. boot / asset loading ---------- */
    check('page loads with stylesheet applied',
      await evalJs("getComputedStyle(document.body).backgroundColor") === 'rgb(239, 226, 200)',
      await evalJs("getComputedStyle(document.body).backgroundColor"));
    check('app.js executed (hero rendered)',
      await evalJs("!!document.querySelector('.hero h1 .line .ch')"));
    check('no page errors on boot', errors.length === 0, errors.join(' | '));

    /* ---------- 2. session list from GET /api/threads ---------- */
    check('session list built from /api/threads',
      await evalJs("document.querySelectorAll('#sessionList .session-item').length") === 2,
      await evalJs("document.querySelectorAll('#sessionList .session-item').length"));
    check('session titles come from ThreadInfo.last_message',
      (await evalJs("document.querySelector('#sessionList .session-item .t').textContent")) === '你能做什么？');
    check('sandbox status wired into the rail foot',
      /sandbox/.test(await evalJs("document.querySelector('#engineState').textContent")),
      await evalJs("document.querySelector('#engineState').textContent"));

    /* ---------- 3. open a thread via GET /api/history/{id} ---------- */
    await evalJs("document.querySelectorAll('#sessionList .session-item')[0].click()");
    await sleep(700);
    check('history replay renders turns (system rows filtered)',
      await evalJs("document.querySelectorAll('#stage .thread .msg').length") === 2,
      await evalJs("document.querySelectorAll('#stage .thread .msg').length"));
    check('replayed reasoning lands in a collapsed .reason',
      await evalJs("!!document.querySelector('#stage .thread .msg-ai .reason .rbody') && !document.querySelector('#stage .thread .msg-ai .reason').open"));

    /* ---------- 4. streaming: reasoning + tool_call + token + done ---------- */
    await evalJs("document.querySelector('#newBtn').click()");
    await sleep(200);
    await evalJs("(()=>{const i=document.querySelector('#input'); i.value='你能做什么？'; i.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('#send').click();})()");
    await sleep(3000);

    const aiBody = await evalJs("(document.querySelector('#stage .thread .msg-ai .body')||{}).textContent || ''");
    check('SSE token stream rendered into the assistant bubble',
      aiBody.startsWith('[plain] 收到：你能做什么？'), aiBody.slice(0, 60));
    check('done event reply wins over the token accumulator',
      aiBody.includes('—— 这是 mock 后端的固定回复。'), aiBody.slice(-30));
    check('reasoning_token rendered in .reason',
      /先看看手头有什么/.test(await evalJs("(document.querySelector('#stage .thread .msg-ai .reason .rbody')||{}).textContent||''")));
    check('tool_call + tool_result rendered as a tool row',
      /execute/.test(await evalJs("(document.querySelector('#stage .thread .msg-ai .tool .tn')||{}).textContent||''")) &&
      /3 documents matched/.test(await evalJs("(document.querySelector('#stage .thread .msg-ai .tool .tout')||{}).textContent||''")));
    check('live trail collapsed once the turn finished',
      await evalJs("!document.querySelector('#stage .thread .msg-ai .reason').open"));

    const sent = await (await fetch('http://127.0.0.1:' + PORT + '/__log')).json();
    const chatReq = sent.requests.filter(r => r.url === '/api/chat/stream').pop();
    /* org_id / user_id 现在由后端 GET /api/identity 下发（mock 里是 MOCK_IDENTITY），
       前端不再自己造身份、也不再写死 default-org：thread_id 必须用下发的那两个值拼。 */
    check('POST /api/chat/stream used the backend field names',
      !!chatReq && /"thread_id":"omock[^"]*__umock[^"]*__[0-9a-f]{8}"/.test(chatReq.body) &&
      /"org_id":"omock[0-9a-z]*"/.test(chatReq.body) && !/"org_id":"default-org"/.test(chatReq.body),
      chatReq && chatReq.body);
    check('new thread appears in the rail right after send',
      await evalJs("document.querySelectorAll('#sessionList .session-item').length") >= 1);

    /* ---------- 5. upload -> attachment -> chat-with-file ---------- */
    await fetch('http://127.0.0.1:' + PORT + '/__reset');
    await evalJs("document.querySelector('#uploadBtn').click()");
    await sleep(500);
    const obj = await send('Runtime.evaluate', { expression: "document.querySelector('#fileInput')" });
    await send('DOM.setFileInputFiles', { files: [SAMPLE], objectId: obj.result.objectId });
    await sleep(400);
    check('dropped file staged in the modal (real File object kept)',
      await evalJs("document.querySelectorAll('#fileList .file-row').length") === 1);
    await evalJs("document.querySelector('#uploadGo').click()");
    await sleep(1200);
    check('upload row reports the parsed shape from the response',
      /CSV/.test(await evalJs("(document.querySelector('#fileList .file-row')||{}).textContent||''")) &&
      /42 行/.test(await evalJs("(document.querySelector('#fileList .file-row')||{}).textContent||''")),
      await evalJs("(document.querySelector('#fileList .file-row')||{}).textContent||''"));
    check('attachment chip shows above the composer',
      await evalJs("document.querySelectorAll('#attachList .attach').length") === 1,
      await evalJs("(document.querySelector('#attachList .attach')||{}).textContent||''"));
    check('kb counter updated',
      /已上传 1 个/.test(await evalJs("document.querySelector('#kbCount').textContent")),
      await evalJs("document.querySelector('#kbCount').textContent"));

    await evalJs("document.querySelector('#uploadClose').click()");
    await sleep(500);
    await evalJs("(()=>{const i=document.querySelector('#input'); i.value='这份表格的结论是什么'; i.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('#send').click();})()");
    await sleep(3000);

    const log2 = await (await fetch('http://127.0.0.1:' + PORT + '/__log')).json();
    const upReq = log2.requests.find(r => r.url === '/api/files/upload');
    check('POST /api/files/upload sent multipart file + org_id',
      !!upReq && /filename="sample\.csv"/.test(upReq.body) && /org_id/.test(upReq.body), upReq && upReq.body.slice(0, 120));
    const fileChat = log2.requests.filter(r => r.url === '/api/chat-with-file/stream').pop();
    check('next question rode /api/chat-with-file/stream with file_id',
      !!fileChat && /"file_id":"file_[a-z0-9]+"/.test(fileChat.body), fileChat && fileChat.body);
    check('file turn marked on the user bubble',
      /sample\.csv/.test(await evalJs("Array.from(document.querySelectorAll('#stage .thread .msg-user .attach-note')).map(n=>n.textContent).join('|')")));
    check('attachment consumed after the turn',
      await evalJs("document.querySelectorAll('#attachList .attach').length") === 0);
    check('no page errors across all flows', errors.length === 0, errors.join(' | '));

    /* ---------- 6. delete thread ---------- */
    await fetch('http://127.0.0.1:' + PORT + '/__reset');
    const before = await evalJs("document.querySelectorAll('#sessionList .session-item').length");
    await evalJs("document.querySelector('#sessionList .session-item .del').click()");
    await sleep(600);
    const log3 = await (await fetch('http://127.0.0.1:' + PORT + '/__log')).json();
    check('DELETE /api/threads/{id} called by the row control',
      log3.requests.some(r => r.method === 'DELETE' && r.url.startsWith('/api/threads/')),
      log3.requests.filter(r => r.method === 'DELETE').map(r => r.url).join(','));
    check('deleted thread leaves the rail',
      await evalJs("document.querySelectorAll('#sessionList .session-item').length") < before);
  } catch (e) {
    check('harness completed', false, e.message);
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
