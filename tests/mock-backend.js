/* TEMP verification harness — mimics the app/routes.py contract so the
   static/ frontend can be exercised without the real backend.
   Not part of the deliverable; deleted after the smoke test. */
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const STATIC = path.join(ROOT, 'static');
const PORT = Number(process.env.PORT || 8099);

const allRequests = [];
const uploadedFiles = new Map();

/* 身份由后端签发：真实后端用签名 cookie（app/identity.py），这里固定一组即可，
   前端启动时会 GET /api/identity 取回，thread_id 依赖它。 */
const MOCK_IDENTITY = { org_id: 'omock0000000000000000abcd', user_id: 'umock0000000000000000abcd' };

const THREADS = [
  { thread_id: MOCK_IDENTITY.org_id + '__' + MOCK_IDENTITY.user_id + '__aaaa1111', created_at: null, last_message: '你能做什么？' },
  { thread_id: MOCK_IDENTITY.org_id + '__' + MOCK_IDENTITY.user_id + '__bbbb2222', created_at: null, last_message: '总结最近的核心观点' }
];

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml'
};

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function sseChat(req, res, body) {
  const payload = JSON.parse(body || '{}');
  const withFile = req.url.includes('chat-with-file');
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no'
  });
  const frame = (type, extra) => res.write('data: ' + JSON.stringify(Object.assign({ type }, extra)) + '\n\n');

  frame('reasoning_token', { content: '先看看手头有什么…' });
  await sleep(40);
  frame('tool_call', { name: 'execute' });
  await sleep(40);
  frame('tool_result', { output: 'stdout: 3 documents matched' });
  await sleep(40);

  const reply = (withFile ? '[file]' : '[plain]') +
    ' 收到：' + (payload.message || '') + ' —— 这是 mock 后端的固定回复。';
  let acc = '';
  for (const ch of reply) {
    acc += ch;
    frame('token', { content: ch });
    await sleep(4);
  }
  frame('reasoning_token', { content: '（补充）已完成引用对齐。' });
  await sleep(20);
  frame('done', { reply, reasoning: '先看看手头有什么…（补充）已整理完要点。' });
  res.end();
}

function parseMultipart(buf) {
  const text = buf.toString('latin1');
  const nameMatch = /filename="([^"]*)"/.exec(text);
  const orgMatch = /name="org_id"\r\n\r\n([^\r]*)/.exec(text);
  return {
    filename: nameMatch ? Buffer.from(nameMatch[1], 'latin1').toString('utf8') : 'unknown',
    org_id: orgMatch ? orgMatch[1] : 'default-org'
  };
}

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const body = Buffer.concat(chunks);
  allRequests.push({ method: req.method, url: req.url, body: body.length ? body.toString('utf8').slice(0, 400) : '' });
  console.log('[mock]', req.method, req.url);

  try {
    if (u.pathname === '/__log') return json(res, 200, { requests: allRequests });
    if (u.pathname === '/__reset') { allRequests.length = 0; return json(res, 200, { ok: true }); }

    if (u.pathname === '/api/identity') return json(res, 200, MOCK_IDENTITY);

    /* 账号：mock 里始终是游客——前端据此隐藏上传、显示"体验额度"。
       要测登录后的界面，把 /api/auth/me 改成 {registered:true, tier:'member'} 即可。 */
    if (u.pathname === '/api/auth/me') return json(res, 200, { registered: false, tier: 'guest', user: null });
    if (u.pathname === '/api/auth/login' || u.pathname === '/api/auth/register') {
      return json(res, 200, {
        user: { user_id: MOCK_IDENTITY.user_id, username: 'mockuser' },
        tier: 'member', registered: true
      });
    }
    if (u.pathname === '/api/auth/logout') return json(res, 200, { ok: true, tier: 'guest', registered: false });

    /* 额度：后端真实实现按签名身份 + 档位计量，这里只回固定快照，
       让前端的额度条有数据可渲染。 */
    if (u.pathname === '/api/usage') return json(res, 200, {
      day: '2026-01-01', tier: 'guest', tokens_used: 8000, tokens_budget: 20000,
      requests_today: 3, requests_per_min_limit: 3, max_input_tokens: 200
    });

    if (u.pathname === '/api/threads' && req.method === 'GET') {
      const uid = u.searchParams.get('user_id');
      const scoped = uid ? THREADS.map(t => Object.assign({}, t, { thread_id: t.thread_id.replace(MOCK_IDENTITY.user_id, uid) })) : [];
      return json(res, 200, scoped);
    }
    if (u.pathname.startsWith('/api/threads/') && req.method === 'DELETE') return json(res, 200, { status: 'ok' });

    if (u.pathname.startsWith('/api/history/')) {
      const id = decodeURIComponent(u.pathname.slice('/api/history/'.length));
      return json(res, 200, {
        thread_id: id,
        messages: [
          { role: 'user', content: '你能做什么？', reasoning: null },
          { role: 'assistant', content: '我可以回答问题，也能读你的文件、在沙箱里跑代码。', reasoning: '先列出手头有哪些工具。' },
          { role: 'system', content: 'tool output noise' }
        ]
      });
    }

    if (u.pathname === '/api/sandbox/status') return json(res, 200, { available: true, active_count: 2 });

    if (u.pathname === '/api/files/upload' && req.method === 'POST') {
      const info = parseMultipart(body);
      const file_id = 'file_' + Math.random().toString(36).slice(2, 8);
      uploadedFiles.set(file_id, info);
      await sleep(60);
      return json(res, 200, {
        file_id,
        filename: info.filename,
        file_type: 'csv',
        row_count: 42,
        columns: ['id', 'name', 'score'],
        preview: 'id,name,score\n1,a,9',
        analysis_type: 'llm_direct',
        code: null
      });
    }

    if (u.pathname === '/api/chat/stream' || u.pathname === '/api/chat-with-file/stream') {
      return sseChat(req, res, body.toString('utf8'));
    }

    /* static — index.html 引用 /static/css|js/...，这里与真实 app.mount("/static", ...) 对齐 */
    let rel = u.pathname === '/' ? '/index.html' : u.pathname;
    rel = rel.replace(/^\/static(?=\/)/, '');
    const file = path.join(STATIC, path.normalize(rel).replace(/^[\\/]+/, ''));
    if (!file.startsWith(STATIC) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404); return res.end('not found');
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    return res.end(fs.readFileSync(file));
  } catch (e) {
    console.error('[mock] error', e);
    return json(res, 500, { detail: String(e) });
  }
});

server.listen(PORT, '127.0.0.1', () => console.log('mock backend on http://127.0.0.1:' + PORT));
