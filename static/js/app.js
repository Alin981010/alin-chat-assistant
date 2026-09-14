(function(){
  "use strict";

  const $  = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- element refs ---------- */
  const stage       = $('#stage');
  const sessionList = $('#sessionList');
  const sessionCount= $('#sessionCount');
  const crumbTitle  = $('#crumbTitle');
  const crumbMeta   = $('#crumbMeta');
  const composer    = $('#composer');
  const input       = $('#input');
  const sendBtn     = $('#send');
  const newBtn      = $('#newBtn');
  const newTop      = $('#newTop');
  const menuBtn     = $('#menuBtn');
  const railClose   = $('#railClose');
  const scrim       = $('#scrim');
  const uploadBtn   = $('#uploadBtn');
  const fileInput   = $('#fileInput');
  const toast       = $('#toast');
  const toastMsg    = $('#toastMsg');
  const charCount   = $('#charCount');
  const wordmark    = $('#wordmark');
  const attachBar   = $('#attachBar');
  const attachList  = $('#attachList');
  const engineState = $('#engineState');

  const uploadModal = $('#uploadModal');
  const uploadClose = $('#uploadClose');
  const dropzone    = $('#dropzone');
  const dropBtn     = $('#dropBtn');
  const fileList    = $('#fileList');
  const fileHint    = $('#fileHint');
  const uploadGo    = $('#uploadGo');
  const uploadCount = $('#uploadCount');
  const kbCount     = $('#kbCount');

  const acctOpen    = $('#acctOpen');
  const acctLabel   = $('#acctLabel');
  const acctLogout  = $('#acctLogout');
  const authModal   = $('#authModal');
  const authClose   = $('#authClose');
  const authForm    = $('#authForm');
  const authUser    = $('#authUser');
  const authPass    = $('#authPass');
  const authError   = $('#authError');
  const authTitle   = $('#authTitle');
  const authSub     = $('#authSub');
  const authGo      = $('#authGo');
  const authGoLabel = $('#authGoLabel');
  const authNote    = $('#authNote');
  const tabLogin    = $('#tabLogin');
  const tabRegister = $('#tabRegister');

  const META_DEFAULT = 'ALIN CHAT ASSISTANT · DIALOGUE ENGINE';

  /* ============================================================
     BACKEND CONTRACT — app/routes.py, mounted under /api
     ------------------------------------------------------------
     POST   /api/chat/stream              { message, thread_id, user_id, org_id }
     POST   /api/chat-with-file/stream    + { file_id }
     POST   /api/files/upload             multipart: file, org_id
     GET    /api/history/{thread_id}      -> { thread_id, messages:[{role,content,reasoning}] }
     GET    /api/threads?org_id&user_id   -> [{ thread_id, created_at, last_message }]
     DELETE /api/threads/{thread_id}
     GET    /api/sandbox/status           -> { available, active_count }
     GET    /api/sandbox/download?org_id&path
     GET    /api/identity                 -> { org_id, user_id }

     SSE frames are `data: {...}\n\n` with type/fields:
       token{content} · reasoning_token{content} · tool_call{name}
       tool_result{output} · error{message} · done{reply,reasoning}

     thread_id MUST look like "org__user__suffix": routes._parse_thread_owner
     splits on "__" (maxsplit 2) to scope the thread list per org/user.
     ============================================================ */
  /* ============================================================
     IDENTITY — 身份由后端签发（GET /api/identity），浏览器只负责原样回传。

     以前这里自己造 user_id 并把 org_id 写死成 'default-org'，而 org_id 是
     沙箱容器的隔离键，后果是全网访客共用同一个容器、互相能看见对方的文件。
     现在后端用签名 cookie 保存匿名身份（app/identity.py），org_id 不再由
     浏览器决定；这个模块只做两件事：启动时把身份取回来，以及在后端不可用
     时退化为一个临时身份（此时所有请求都会被后端按身份覆写，不会越权）。
     ============================================================ */
  const ID_KEY = 'alinchat.identity.v1';
  const ID = { org_id: '', user_id: '' };

  const newThreadId = () => ID.org_id + '__' + ID.user_id + '__' + threadSuffix();

  async function loadIdentity(){
    try{
      const r = await fetch('/api/identity', { credentials: 'same-origin' });
      if(r.ok){
        const id = await r.json();
        if(id && id.org_id && id.user_id){
          ID.org_id = id.org_id;
          ID.user_id = id.user_id;
          try{ localStorage.setItem(ID_KEY, JSON.stringify(ID)); }catch(e){}
          return ID;
        }
      }
    }catch(e){ /* 后端不可达：下面退化为临时身份 */ }

    /* 兜底：后端拿不到身份时（离线预览、mock 后端）沿用浏览器本地身份，
       但绝不使用 'default-org' —— 那个值在后端会被判为历史遗留、当场换新身份，
       在这里继续用它只会让 thread_id 与真实身份对不上。 */
    try{
      const raw = JSON.parse(localStorage.getItem(ID_KEY) || 'null');
      if(raw && raw.user_id && raw.org_id && raw.org_id !== 'default-org'){
        ID.org_id = raw.org_id;
        ID.user_id = raw.user_id;
        return ID;
      }
    }catch(e){ /* storage unavailable */ }

    const rnd = (window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID().replace(/-/g, '')
      : String(Math.random()).slice(2) + String(Date.now())
    ).slice(0, 12);
    ID.org_id = 'o' + rnd;
    ID.user_id = 'u' + rnd;
    try{ localStorage.setItem(ID_KEY, JSON.stringify(ID)); }catch(e){}
    return ID;
  }

  const API = {
    chatStream:       '/api/chat/stream',
    chatWithFile:     '/api/chat-with-file/stream',
    upload:           '/api/files/upload',
    history:  id   => '/api/history/' + encodeURIComponent(id),
    threads:  ()   => '/api/threads?' + new URLSearchParams({ org_id: ID.org_id, user_id: ID.user_id }),
    thread:   id   => '/api/threads/' + encodeURIComponent(id),
    sandbox:          '/api/sandbox/status'
  };

  /* 后端运行时开关（GET /api/config）。code_execution 默认 false：
     本部署不执行代码，前端据此隐藏沙箱状态、避免显示成「故障」。 */
  const RUNTIME = { code_execution: null, assistant_name: '小助手' };
  async function loadRuntimeConfig(){
    try{
      const r = await fetch('/api/config', { credentials: 'same-origin' });
      if(r.ok){
        const c = await r.json();
        if(c && typeof c.code_execution === 'boolean') RUNTIME.code_execution = c.code_execution;
        if(c && c.assistant_name) RUNTIME.assistant_name = c.assistant_name;
      }
    }catch(e){ /* 拿不到就按「有沙箱」渲染，不影响对话 */ }
    return RUNTIME;
  }

  /* ============================================================
     账号状态

     **档位由服务端判定**（游客 / 会员），前端只负责呈现——本地没有任何办法
     把自己变成会员。游客额度很小（见 app/limits.py），撞到额度时后端会返回
     一条引导注册的提示，这里把它变成"打开注册弹窗"的动作。
     ============================================================ */
  const ACCOUNT = { registered: false, tier: 'guest', username: null };

  const isGuest = () => !ACCOUNT.registered;

  async function loadAccountState(){
    try{
      const r = await fetch('/api/auth/me', { credentials: 'same-origin' });
      if(r.ok){
        const d = await r.json();
        ACCOUNT.registered = !!d.registered;
        ACCOUNT.tier = d.tier || 'guest';
        ACCOUNT.username = (d.user && d.user.username) || null;
      }
    }catch(e){ /* 未登录或后端不可用：按游客处理 */ }
    renderAccount();
    return ACCOUNT;
  }

  function renderAccount(){
    if(acctLabel){
      acctLabel.textContent = ACCOUNT.registered ? (ACCOUNT.username || '已登录') : '登录 / 注册';
    }
    if(acctOpen) acctOpen.classList.toggle('is-member', ACCOUNT.registered);
    if(acctLogout) acctLogout.hidden = !ACCOUNT.registered;
    if(quotaLabel) quotaLabel.textContent = ACCOUNT.registered ? '今日额度' : '体验额度';
    if(quotaBar) quotaBar.classList.toggle('is-guest', isGuest());
    applyGuestGating();
  }

  /* ---------- tiny persistent caches (no backend endpoint for these) ---------- */
  const store = {
    get(key, fallback){
      try{ const v = JSON.parse(localStorage.getItem(key) || 'null'); return v === null ? fallback : v; }
      catch(e){ return fallback; }
    },
    set(key, value){
      try{ localStorage.setItem(key, JSON.stringify(value)); }
      catch(e){ /* quota / private mode: degrade to in-memory only */ }
    }
  };
  /* localStorage 键。这些是**浏览器本地**的缓存键，不进服务端，所以跟着
     产品名一起改是安全的（代价仅是旧的本地缓存失效一次：会话计数与标题重建）。
     与 app/identity.py 里的 cookie 名不同——那个改了会让在线用户换身份。 */
  const K_COUNTS = 'alinchat.threadCounts.v1';   /* thread_id -> message count */
  const K_TITLES = 'alinchat.threadTitles.v1';   /* thread_id -> title         */
  const K_DOCS   = 'alinchat.docs.v1';           /* [{file_id, filename, at}]  */
  const K_ACTIVE = 'alinchat.activeThread.v1';

  const state = {
    threadId:    null,
    messages:    [],   /* { role, content, reasoning? } */
    sessions:    [],   /* { id, title, message_count }  */
    streaming:   false,
    queueFiles:  [],   /* staged in the upload modal    */
    attachments: [],   /* uploaded, waiting to ride the next question */
    docs:        store.get(K_DOCS, [])
  };

  const counts = store.get(K_COUNTS, {});
  const titles = store.get(K_TITLES, {});
  const countOf = id => (typeof counts[id] === 'number' ? counts[id] : null);
  const titleOf = id => titles[id] || '';
  function rememberThread(id, count, title){
    if(id){
      if(typeof count === 'number'){ counts[id] = count; store.set(K_COUNTS, counts); }
      if(title){ titles[id] = title; store.set(K_TITLES, titles); }
    }
  }
  function forgetThread(id){
    if(!id) return;
    delete counts[id]; delete titles[id];
    store.set(K_COUNTS, counts); store.set(K_TITLES, titles);
  }
  const saveActive = id => store.set(K_ACTIVE, id || null);

  /* ============================================================
     ICONS — inline cut-out SVGs.
     Every glyph is drawn as a filled silhouette with a thick
     ink outline, so it reads as a shape scissored out of paper
     rather than a hairline UI icon. No CDN dependency.
     ============================================================ */
  const ICON = {
    "x":"M18 6 6 18M6 6l12 12",
    "hexagon":"M21 16.05v-8.1a2 2 0 0 0-1-1.73l-7-4.04a2 2 0 0 0-2 0l-7 4.04a2 2 0 0 0-1 1.73v8.1a2 2 0 0 0 1 1.73l7 4.04a2 2 0 0 0 2 0l7-4.04a2 2 0 0 0 1-1.73Z",
    "plus":"M12 5v14M5 12h14",
    "library":"M16 6 8 6M16 12 8 12M16 18 8 18",
    "database":"M21 5c0 1.66-4.03 3-9 3S3 6.66 3 5s4.03-3 9-3 9 1.34 9 3ZM3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3",
    "upload":"M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12",
    "panel-left":"M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z M9 3v18",
    "square-pen":"M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7M18.4 2.6a2 2 0 0 1 2.8 2.8L12 14.6 8 15.6l1-4Z",
    "arrow-up":"M12 19V5M5 12l7-7 7 7",
    "info":"M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20ZM12 16v-4M12 8h.01",
    "cloud-upload":"M12 13v8M8 17l4-4 4 4M20 16.6A5 5 0 0 0 18 7h-1.3A8 8 0 1 0 4 15.3",
    "folder-open":"M6 20h12a2 2 0 0 0 1.9-1.4l2.1-7A1 1 0 0 0 21 10h-7a2 2 0 0 0-1.9 1.4L10.9 14A2 2 0 0 1 9 15.4H3.6A1 1 0 0 0 2.6 16.7L4.1 18.6A2 2 0 0 0 6 20ZM4 10V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.6.8l1 1.4a2 2 0 0 0 1.6.8H18a2 2 0 0 1 2 2v2",
    "rocket":"M5 13c-1.5 1.5-2 5-2 5s3.5-.5 5-2c.9-.9.9-2.3 0-3.1-.8-.9-2.2-.9-3 .1ZM12.5 15.5 8.5 11.5c.7-4.7 4-8.5 8-9.5 2-1 4.5-1 4.5-1s0 2.5-1 4.5c-1 4-4.8 7.3-9.5 8ZM15 9h.01",
    "file-search":"M15 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6ZM15 2v4h4M9.5 15.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM11.5 15.5 13.5 17.5",
    "list-checks":"M3 6l2 2 4-4M3 14l2 2 4-4M13 7h8M13 15h8M13 19h8",
    "scan-text":"M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2M7 8h8M7 12h10M7 16h6",
    "pen-line":"M4 20h4L20 8a2.5 2.5 0 0 0-3.5-3.5L4 16ZM3 22h10",
    "code":"M9 8 5 12l4 4M15 8l4 4-4 4",
    "graduation-cap":"M12 3 2 8l10 5 10-5ZM6 11v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5",
    "git-branch":"M6 3v12M18 9v.01M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM18 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM6 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM15 6h1a2 2 0 0 1 2 2v1",
    "user":"M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z",
    "sparkles":"M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9ZM19 15l.9 2.4L22 18.3l-2.1.9L19 21.5l-.9-2.3L16 18.3l2.1-.9ZM5 16l.7 1.8L7.5 18.5l-1.8.7L5 21l-.7-1.8L2.5 18.5l1.8-.7Z",
    "trash-2":"M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6",
    "triangle-alert":"M12 3 2 20h20ZM12 9v5M12 17h.01",
    "file-text":"M15 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6ZM15 2v4h4M9 13h6M9 17h4",
    "file-type-2":"M15 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6ZM15 2v4h4M9 13h6M9 17h3",
    "file-code":"M15 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6ZM15 2v4h4M10 12l-2 2 2 2M14 12l2 2-2 2",
    "file":"M15 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6ZM15 2v4h4M9 13h6M9 17h4",
    "paperclip":"M21.4 11.05 12.25 20.2a5.5 5.5 0 0 1-7.78-7.78l9.19-9.19a3.67 3.67 0 0 1 5.19 5.19l-9.2 9.19a1.83 1.83 0 0 1-2.59-2.59l8.48-8.49",
    "check":"M20 6 9 17l-5-5"
  };

  function svgFor(name){
    const d = ICON[name];
    if(!d) return '';
    return '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true" focusable="false">' +
      '<path d="' + d + '" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }
  /* swap every [data-icon] placeholder for its cut-out shape */
  const icons = () => {
    $$('[data-icon]').forEach(el=>{
      const s = svgFor(el.getAttribute('data-icon'));
      if(s) el.outerHTML = s;
    });
  };

  /* ---------- helpers ---------- */
  const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const pad2 = n => String(n).padStart(2,'0');
  const isNearBottom = () => stage.scrollHeight - stage.scrollTop - stage.clientHeight < 150;
  const scrollBottom = (force) => { if(force || isNearBottom()) stage.scrollTop = stage.scrollHeight; };

  function toastShow(msg, isErr){
    toastMsg.textContent = msg;
    toast.classList.toggle('err', !!isErr);
    toast.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(()=> toast.classList.remove('show'), 2800);
  }

  /* the wordmark settles like type being pressed into paper */
  function scrambleText(el, finalText, dur){
    if(!el) return;
    if(REDUCED){ el.textContent = finalText; return; }
    dur = dur || 900;
    const glyphs = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/\\<>*#%&";
    const start = performance.now();
    const len = finalText.length;
    (function frame(now){
      const p = Math.min((now - start) / dur, 1);
      let out = '';
      for(let i=0;i<len;i++){
        out += (i < p*len) ? finalText[i] : glyphs[(Math.random()*glyphs.length)|0];
      }
      el.textContent = out;
      if(p < 1) requestAnimationFrame(frame); else el.textContent = finalText;
    })(performance.now());
  }

  /* ---------- API plumbing ---------- */
  async function api(path, opts){
    const res = await fetch(path, Object.assign({ headers:{ 'Content-Type':'application/json' } }, opts));
    const data = await res.json().catch(()=> ({}));
    /* FastAPI raises HTTPException -> { detail }, not { error } */
    if(!res.ok){
      const err = new Error(data.detail || data.error || ('HTTP ' + res.status));
      /* 把状态码带上：调用方要靠它区分「过期会话（403，可自愈）」和真失败。
         只靠文案匹配太脆，措辞一改就失效。 */
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ============================================================
     SESSIONS / THREADS
     ============================================================ */
  const threadSuffix = () => {
    const rnd = (window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID().replace(/-/g, '')
      : String(Date.now().toString(36)) + String(Math.random()).slice(2, 8)
    );
    return rnd.slice(0, 8);
  };

  function shortId(id){
    if(!id) return '';
    const parts = String(id).split('__');
    return '#' + (parts[parts.length - 1] || id).slice(0, 8);
  }

  async function loadThreads(){
    try{
      const rows = await api(API.threads());
      state.sessions = (Array.isArray(rows) ? rows : []).map(r => ({
        id: r.thread_id,
        title: (r.last_message || '').trim(),
        message_count: countOf(r.thread_id)
      }));
    }catch(e){
      /* non-fatal: keep whatever list we already have */
    }
    renderSessions();
  }

  /* the active thread lives only in the browser until its first message
     is checkpointed, so it is merged in optimistically */
  function sessionItems(){
    const out = [];
    const seen = new Set();
    if(state.threadId && (state.messages.length || state.streaming)){
      out.push({
        id: state.threadId,
        title: titleOf(state.threadId) || '新会话',
        message_count: state.messages.length || countOf(state.threadId)
      });
      seen.add(state.threadId);
    }
    for(const s of state.sessions){
      if(seen.has(s.id)) continue;
      seen.add(s.id);
      out.push(s);
    }
    return out;
  }

  /* ============================================================
     过期会话自愈

     服务端的归属校验会拒掉「不属于当前身份」的会话。最典型的来源不是攻击，
     而是**身份迁移**：早期版本里 org_id 由前端写死（default-org），后来改成
     服务端签名，于是浏览器 localStorage 里那些 default-org__ 开头的旧会话
     在服务端已作废——而 activeThread 是持久化的，刷新后还会去恢复它，
     表现就是"打开页面就报无权访问、消息也发不出去"。

     这类错误必须被识别并**自动换成新会话**，否则用户会一直卡在错误上。
     ============================================================ */
  function isStaleSessionError(err){
    if(!err) return false;
    const m = String(err.message || '');
    return err.status === 403 || /无权访问|不属于当前身份|无法识别的会话标识/.test(m);
  }

  function dropThreadLocally(id){
    if(id){
      state.sessions = (state.sessions || []).filter(x => x.id !== id);
      forgetThread(id);
    }
    state.threadId = null;
    state.messages = [];
    saveActive(null);
  }

  function recoverFromStaleSession(id){
    dropThreadLocally(id);
    toastShow('该会话属于旧版本（身份迁移前），已作废；已为你新建会话。');
    applyBlank();
  }

  async function openSession(id){
    try{
      const data = await api(API.history(id));
      state.threadId = id;
      saveActive(id);
      state.messages = (data.messages || [])
        .filter(m => m.role === 'user' || m.role === 'assistant')
        .map(m => ({ role: m.role, content: m.content || '', reasoning: m.reasoning || null }));
      rememberThread(id, state.messages.length);
      const title = titleOf(id) || firstUserLine(state.messages) || '选择一个会话';
      crumbTitle.textContent = title;
      crumbMeta.textContent = META_DEFAULT + ' · ' + shortId(id);
      renderThread();
      renderSessions();
    }catch(e){
      if(isStaleSessionError(e)){ recoverFromStaleSession(id); return; }
      /* 游客权限被拒（例如上传、或游客额度相关）时不要弹红错误，
         直接给注册入口——后端文案里已经写清了原因。 */
      if(/注册/.test(e.message || '')){ toastShow(e.message); if(isGuest()) openAuth('register'); return; }
      toastShow(e.message || '加载会话失败', true);
    }
  }

  async function deleteSession(id, ev){
    if(ev) ev.stopPropagation();
    try{
      await api(API.thread(id), { method:'DELETE' });
      state.sessions = state.sessions.filter(x => x.id !== id);
      forgetThread(id);
      if(state.threadId === id) applyBlank(); else renderSessions();
    }catch(e){ toastShow(e.message || '删除失败', true); }
  }

  function firstUserLine(messages){
    const m = (messages || []).find(x => x.role === 'user');
    if(!m) return '';
    const t = String(m.content || '').replace(/\s+/g, ' ').trim();
    return t.length > 30 ? t.slice(0, 30) + '…' : t;
  }

  function applyBlank(){
    state.threadId = null;
    state.messages = [];
    saveActive(null);
    crumbTitle.textContent = '新会话';
    crumbMeta.textContent = META_DEFAULT;
    renderThread();
    renderSessions();
    input.focus();
    if(window.innerWidth < 900) setRail(false);
  }

  function newChat(){ applyBlank(); }

  async function ensureThread(){
    if(state.threadId) return state.threadId;
    state.threadId = newThreadId();
    saveActive(state.threadId);
    return state.threadId;
  }

  /* ---------- render: sessions ---------- */
  function renderSessions(){
    const items = sessionItems();
    sessionCount.textContent = items.length ? pad2(items.length) : '';
    sessionList.innerHTML = '';
    if(!items.length){
      const d = document.createElement('div');
      d.className = 'rail-empty';
      d.textContent = '还没有会话\n点击上方「新会话」开始';
      sessionList.appendChild(d);
    } else {
      items.forEach((s,i)=>{
        const it = document.createElement('button');
        it.type = 'button';
        it.className = 'session-item' + (s.id === state.threadId ? ' active' : '');
        it.style.animationDelay = (i * 0.035) + 's';
        /* ThreadInfo carries no message count, so it is tracked locally;
           without one, the thread's own suffix is more useful than a dash */
        const cnt = typeof s.message_count === 'number' ? (pad2(s.message_count) + ' MSG') : shortId(s.id);
        it.innerHTML =
          '<span class="idx">' + pad2(i + 1) + '</span>' +
          '<span class="meta"><span class="t">' + escapeHtml(s.title || '未命名会话') + '</span>' +
          '<span class="s">' + cnt + '</span></span>' +
          '<span class="del" role="button" aria-label="删除会话"><i data-icon="trash-2"></i></span>';
        it.addEventListener('click', ()=> openSession(s.id));
        it.querySelector('.del').addEventListener('click', (ev)=> deleteSession(s.id, ev));
        sessionList.appendChild(it);
      });
    }
    icons();
  }

  /* ---------- render: hero ---------- */
  /* 示例问题按新的能力定位排：先是「这能干什么」，再各覆盖一类真实用途
     （写作、代码讲解、分析、学习）。点一下就直接问，比写一段能力介绍有用。 */
  const HERO_CHIPS = [
    ['你能做什么？', 'file-search'],
    ['帮我把这段话改得更正式一些', 'pen-line'],
    ['这段 Python 报错了，帮我看看为什么', 'code'],
    ['把这份材料总结成三个要点', 'list-checks'],
    ['给我讲讲复利是怎么算的', 'graduation-cap']
  ];

  function renderHero(){
    const chips = HERO_CHIPS.map(([q, icon]) =>
      '<button class="chip" data-q="' + escapeHtml(q) + '"><i data-icon="' + icon + '"></i>' + escapeHtml(q) + '</button>'
    ).join('');
    stage.innerHTML =
      '<div class="hero">' +
        '<div class="eyebrow"><span class="rule"></span><span id="heroEyebrow">ALIN CHAT ASSISTANT · DIALOGUE ENGINE</span></div>' +
        '<h1><span class="line" data-text="每个问题"></span><span class="line" data-text="都有回应"></span></h1>' +
        '<p>会聊，也讲得清楚。问答、写作、翻译、编程、分析都在这一个对话框里——' +
        '贴代码进来它会<strong>逐段讲给你听</strong>，而不是只丢一句结论。</p>' +
        '<div class="chips">' + chips + '</div>' +
      '</div>';
    splitHeadline();
    scrambleText($('#heroEyebrow'), 'ALIN CHAT ASSISTANT · DIALOGUE ENGINE', 800);
    $$('.chip').forEach(ch => ch.addEventListener('click', ()=>{ setInput(ch.dataset.q); autoGrow(); input.focus(); }));
    icons();
  }

  /* each character is scissored out individually and settles into line */
  function splitHeadline(){
    let k = 0;
    $$('#stage .hero h1 .line').forEach((line, li)=>{
      const text = line.getAttribute('data-text') || line.textContent;
      line.textContent = '';
      [...text].forEach((c, i)=>{
        const ch = document.createElement('span');
        ch.className = 'ch' + (li === 1 && i === text.length - 1 ? ' accent' : '');
        ch.style.animationDelay = (0.12 + k * 0.045) + 's';
        ch.textContent = c;
        line.appendChild(ch);
        k++;
      });
    });
  }

  /* ---------- render: thread ---------- */
  function ghostWord(){
    const gw = document.createElement('div');
    gw.className = 'ghost-word';
    gw.setAttribute('aria-hidden','true');
    ['A','L','I','N'].forEach(c=>{
      const sp = document.createElement('span');
      sp.textContent = c;
      gw.appendChild(sp);
    });
    return gw;
  }

  function threadShell(){
    const t = document.createElement('div');
    t.className = 'thread';
    const spine = document.createElement('div');
    spine.className = 'spine'; spine.textContent = 'DIALOGUE · LIVE';
    t.appendChild(spine);
    return t;
  }

  function renderThread(){
    stage.innerHTML = '';
    /* the brand watermark is built as cut letters so the
       parallax handler can drift them as one lockup */
    stage.appendChild(ghostWord());

    if(!state.messages.length){ renderHero(); return; }

    const t = threadShell();
    state.messages.forEach((m, i)=> t.appendChild(
      makeMessage(m.role, m.content, i, {
        reasoning: m.reasoning,
        attach: m.attach
      })
    ));
    stage.appendChild(t);
    requestAnimationFrame(()=>{ stage.scrollTop = stage.scrollHeight; });
    icons();
  }

  function makeMessage(role, text, index, opts){
    opts = opts || {};
    const m = document.createElement('div');
    m.className = 'msg ' + (role === 'user' ? 'msg-user' : 'msg-ai');

    const gutter = document.createElement('div');
    gutter.className = 'gutter';
    gutter.innerHTML = '<span class="num">' + pad2((index || 0) + 1) + '</span>' +
      '<span class="badge"><i data-icon="' + (role === 'user' ? 'user' : 'sparkles') + '"></i></span>';

    const content = document.createElement('div');
    content.className = 'content';
    const who = document.createElement('div');
    who.className = 'who';
    who.innerHTML = role === 'user'
      ? '<span class="role">operator</span><span class="name">You</span>'
      : '<span class="name">' + escapeHtml(RUNTIME.assistant_name) + '</span><span class="role">assistant</span>';
    content.appendChild(who);

    if(role !== 'user'){
      const trail = makeTrail({ reasoning: opts.reasoning, tools: opts.tools, open: false, tag: '思考记录' });
      if(trail) content.appendChild(trail);
    }

    const body = document.createElement('div');
    body.className = 'body';
    body.textContent = text || '';
    content.appendChild(body);

    if(opts.attach){
      const note = document.createElement('div');
      note.className = 'attach-note';
      note.innerHTML = '<i data-icon="paperclip"></i><span></span>';
      note.querySelector('span').textContent = opts.attach;
      content.appendChild(note);
    }

    m.appendChild(gutter);
    m.appendChild(content);
    return m;
  }

  /* ---------- reasoning / tool trail ---------- */
  /* One builder for both replayed history and live streaming, so the
     `tool_call` / `tool_result` / `reasoning_token` events have a home. */
  function makeTrail(opts){
    opts = opts || {};
    const hasReason = !!(opts.reasoning && String(opts.reasoning).trim());
    const hasTools  = !!(opts.tools && opts.tools.length);
    if(!hasReason && !hasTools && !opts.live) return null;

    const el = document.createElement('div');
    el.className = 'trail';

    const reason = document.createElement('details');
    reason.className = 'reason';
    reason.open = opts.open !== false;
    reason.hidden = !hasReason;   /* live: revealed by the first reasoning token */
    reason.innerHTML = '<summary><i data-icon="sparkles"></i><span>思考过程</span>' +
      '<span class="rtag">' + escapeHtml(opts.tag || (opts.live ? 'LIVE' : '思考记录')) + '</span></summary>';
    const rbody = document.createElement('div');
    rbody.className = 'rbody';
    if(hasReason) rbody.textContent = opts.reasoning;
    reason.appendChild(rbody);

    const tools = document.createElement('div');
    tools.className = 'tools';
    if(hasTools){
      opts.tools.forEach(t=>{
        const row = document.createElement('div');
        row.className = 'tool';
        row.innerHTML = '<span class="tn"><i data-icon="git-branch"></i>' + escapeHtml(t.name || 'tool') + '</span>';
        if(t.output){
          const pre = document.createElement('pre');
          pre.className = 'tout';
          pre.textContent = t.output;
          row.appendChild(pre);
        }
        tools.appendChild(row);
      });
    }

    el.appendChild(reason);
    el.appendChild(tools);
    el._refs = { reason, rbody, tools, tag: reason.querySelector('.rtag') };
    return el;
  }

  function pushToolCall(toolsEl, name){
    const row = document.createElement('div');
    row.className = 'tool running';
    row.innerHTML = '<span class="tn"><i data-icon="git-branch"></i>' + escapeHtml(name || 'tool') +
      '</span><span class="tt">运行中…</span>';
    toolsEl.appendChild(row);
    icons();
    scrollBottom();
  }

  function pushToolResult(toolsEl, output){
    /* fill the most recent call that is still waiting on a result */
    const running = toolsEl.querySelectorAll('.tool.running');
    let row = running.length ? running[running.length - 1] : null;
    if(!row){
      row = document.createElement('div');
      row.className = 'tool';
      row.innerHTML = '<span class="tn"><i data-icon="git-branch"></i>tool</span>';
      toolsEl.appendChild(row);
    }
    row.classList.remove('running');
    const tt = row.querySelector('.tt');
    if(tt) tt.remove();
    const pre = document.createElement('pre');
    pre.className = 'tout';
    pre.textContent = output || '';
    row.appendChild(pre);
    icons();
    scrollBottom();
  }

  /* ---------- streaming ---------- */
  function sseFrameHandler(frame, sink){
    let payload = '';
    for(const rawLine of frame.split('\n')){
      const line = rawLine.replace(/\r$/, '');
      if(line.startsWith('data:')) payload += line.slice(5).replace(/^\s/, '');
    }
    if(!payload) return;
    if(payload === '[DONE]'){ sink.finished = true; return; }
    let d;
    try{ d = JSON.parse(payload); }catch(_){ return; }
    if(!d || !d.type) return;

    switch(d.type){
      case 'reasoning_token':
        if(d.content) sink.onReasoning(d.content);
        break;
      case 'token':
        if(d.content) sink.onToken(d.content);
        break;
      case 'tool_call':
        sink.onToolCall(d.name);
        break;
      case 'tool_result':
        sink.onToolResult(d.output);
        break;
      case 'error':
        sink.error = new Error(d.message || '生成出错');
        break;
      case 'budget':
        /* 后端在额度耗尽、截断生成时发来的提示。不是错误：已生成的内容有效，
           只是这一轮不完整。单独记一笔，让调用方决定怎么呈现。 */
        sink.budget = d;
        break;
      case 'done':
        sink.final = d;
        sink.finished = true;
        break;
      default:
        break;
    }
  }

  /* 过期的 activeThread 会让**每一条**消息都 403（thread_id 从它拼出来），
     所以这里自愈：清掉旧会话、新建一个、把这一轮重发一次，而不是把
     「无权访问」糊在用户脸上让他自己想办法。 */
  async function streamChat(message, attachment, retried){
    try{
      return await streamChatOnce(message, attachment);
    }catch(err){
      if(isStaleSessionError(err) && !retried){
        const staleId = state.threadId;
        dropThreadLocally(staleId);
        /* 乐观气泡已经在 streamChatOnce 里落到 DOM 上了，重试前先清掉，
           否则重发会多出一条重复的用户消息。 */
        const nodes = stage.querySelectorAll('.thread .msg');
        for(let i = nodes.length - 1; i >= 0 && i >= nodes.length - 2; i--) nodes[i].remove();
        toastShow('上次的会话来自旧版本，已作废；已用新会话重新发送。');
        return await streamChat(message, attachment, true);
      }
      throw err;
    }
  }

  async function streamChatOnce(message, attachment){
    const threadId = await ensureThread();
    const title = message.replace(/\s+/g, ' ').trim().slice(0, 26) + (message.length > 26 ? '…' : '');
    crumbTitle.textContent = title;
    crumbMeta.textContent = 'DIALOGUE · LIVE · ' + shortId(threadId);
    rememberThread(threadId, state.messages.length + 2, title);

    stage.querySelector('.hero')?.remove();
    let thread = stage.querySelector('.thread');
    if(!thread){ thread = threadShell(); stage.appendChild(thread); }

    const userMsg = makeMessage('user', message, state.messages.length, {
      attach: attachment ? attachment.filename : null
    });
    thread.appendChild(userMsg);
    state.messages.push({ role:'user', content: message, attach: attachment ? attachment.filename : null });

    const aiIndex = state.messages.length;
    const aiNode = makeMessage('ai', '', aiIndex);
    aiNode.classList.add('live');
    thread.appendChild(aiNode);
    icons();

    const contentEl = aiNode.querySelector('.content');
    const body = aiNode.querySelector('.body');
    const think = document.createElement('div');
    think.className = 'think';
    think.innerHTML = '<span class="orb"></span><span>正在处理</span><span class="scan"></span>';
    contentEl.insertBefore(think, body);

    /* live trail: reasoning tokens + tool activity land here */
    const trail = makeTrail({ live: true, open: true, tag: 'LIVE' });
    contentEl.insertBefore(trail, body);
    const refs = trail._refs;

    state.streaming = true;
    sendBtn.classList.add('sending');
    body.textContent = '';
    body.innerHTML = '<span class="caret"></span>';
    scrollBottom(true);

    let acc = '';
    let reasonAcc = '';
    let firstToken = true;
    let streamErr = null;

    const sink = {
      finished: false,
      final: null,
      error: null,
      onReasoning(text){
        reasonAcc += text;
        refs.reason.hidden = false;
        refs.rbody.textContent = reasonAcc;
        scrollBottom();
      },
      onToken(text){
        if(firstToken){ think.remove(); firstToken = false; }
        acc += text;
        body.textContent = acc;
        body.insertAdjacentHTML('beforeend', '<span class="caret"></span>');
        scrollBottom();
      },
      onToolCall(name){ pushToolCall(refs.tools, name); },
      onToolResult(out){ pushToolResult(refs.tools, out); }
    };

    try{
      const payload = { message, thread_id: threadId, user_id: ID.user_id, org_id: ID.org_id };
      const url = attachment ? API.chatWithFile : API.chatStream;
      if(attachment) payload.file_id = attachment.file_id;

      const res = await fetch(url, {
        method:'POST',
        headers:{ 'Content-Type':'application/json' },
        body: JSON.stringify(payload)
      });

      if(!res.ok || !res.body){
        const detail = await res.json().catch(()=> ({}));
        const err = new Error(detail.detail || detail.error || ('请求失败 HTTP ' + res.status));
        err.status = res.status;   /* 让上层能识别「过期会话」并自愈 */
        throw err;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while(true){
        const step = await reader.read();
        if(step.done) break;
        buf += decoder.decode(step.value, { stream:true });
        let idx;
        while((idx = buf.indexOf('\n\n')) !== -1){
          sseFrameHandler(buf.slice(0, idx), sink);
          buf = buf.slice(idx + 2);
        }
      }
      if(buf.trim()) sseFrameHandler(buf, sink);
      if(sink.error) throw sink.error;

      if(sink.final && typeof sink.final.reply === 'string' && sink.final.reply.length){
        acc = sink.final.reply;
      }
      if(sink.final && sink.final.reasoning && !reasonAcc){
        reasonAcc = sink.final.reasoning;
        refs.reason.hidden = false;
        refs.rbody.textContent = reasonAcc;
      }
      if(sink.budget){
        /* 额度耗尽导致本轮被截断：内容仍然有效，所以不当作错误。
           游客撞到额度是最值得好好说话的一次——多半就是这个时刻决定他会不会注册，
           所以除了提示，直接把注册弹窗准备好（不自动弹，避免打断阅读）。 */
        const msg = sink.budget.message || '今日额度已用完';
        toastShow(msg, true);
        if(isGuest()) setTimeout(()=> openAuth('register'), 400);
      }
      finalize();
      refreshUsage();
    }catch(err){
      streamErr = err;
      think.remove();
      if(!acc){
        body.innerHTML = '<i data-icon="triangle-alert"></i> ' + escapeHtml(err.message || '生成失败，请重试');
        body.classList.add('is-error');
      }
      toastShow(err.message || '生成失败', true);
      finalize(true);
      /* 过期会话交给外层 streamChat 自愈（换新会话重发）。这里必须**重新抛出**，
         否则 streamChatOnce 一律吞掉错误，自愈路径永远不会被触发。 */
      if(isStaleSessionError(err)) throw err;
    }finally{
      state.streaming = false;
      sendBtn.classList.remove('sending');
      syncSendState();

      /* the attachment was consumed by this turn */
      if(attachment && !streamErr){
        state.attachments = state.attachments.filter(a => a.file_id !== attachment.file_id);
        renderAttachments();
      }
      rememberThread(threadId, state.messages.length);
      loadThreads();
    }

    function finalize(isErr){
      if(think && think.parentNode) think.remove();
      const caret = body.querySelector('.caret');
      if(caret) caret.remove();
      aiNode.classList.remove('live');

      /* settle the trail: collapse it, or drop it when nothing happened */
      const hasReason = !!reasonAcc.trim();
      const hasTools = refs.tools.childElementCount > 0;
      if(!hasReason && !hasTools){
        trail.remove();
      } else {
        refs.reason.hidden = !hasReason;
        refs.reason.open = false;
        if(refs.tag) refs.tag.textContent = '思考记录';
        refs.tools.querySelectorAll('.tool.running').forEach(r=>{
          r.classList.remove('running');
          const tt = r.querySelector('.tt');
          if(tt) tt.textContent = '完成';
        });
      }

      if(!isErr && acc && !aiNode._done){
        aiNode._done = true;
        state.messages.push({
          role:'assistant',
          content: acc,
          reasoning: hasReason ? reasonAcc : null,
          attach: attachment ? attachment.filename : null
        });
      }
      scrollBottom();
    }
  }

  /* ---------- composer ---------- */
  function setInput(v){ input.value = v; syncSendState(); }
  function autoGrow(){
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';
    charCount.textContent = input.value.length;
  }
  function syncSendState(){
    const has = input.value.trim().length > 0;
    sendBtn.classList.toggle('idle', !has);
  }
  async function sendMessage(){
    const text = input.value.trim();
    if(!text || state.streaming) return;
    const attachment = state.attachments[0] || null;
    if(state.attachments.length > 1){
      toastShow('本轮携带「' + attachment.filename + '」，其余 ' + (state.attachments.length - 1) + ' 个留待下一轮');
    }
    input.value = '';
    autoGrow();
    syncSendState();
    await streamChat(text, attachment);
  }

  /* ---------- rail ---------- */
  function setRail(open){ document.body.classList.toggle('rail-open', open); }

  /* ---------- ghost word parallax ---------- */
  function initParallax(){
    if(REDUCED) return;
    const gw = () => document.querySelector('.ghost-word');
    let px = 0, py = 0, tx = 0, ty = 0;
    window.addEventListener('pointermove', e=>{
      tx = (e.clientX / innerWidth - 0.5) * -46;
      ty = (e.clientY / innerHeight - 0.5) * -30;
    });
    (function loop(){
      px += (tx - px) * 0.06; py += (ty - py) * 0.06;
      const el = gw();
      if(el) el.style.transform = 'translate(calc(-50% + ' + px + 'px), calc(-50% + ' + py + 'px))';
      requestAnimationFrame(loop);
    })();
  }

  /* ============================================================
     ATTACHMENTS — POST /api/files/upload keeps the parsed file
     server-side under file_id; the next question rides
     /api/chat-with-file/stream with that id (one per turn —
     the endpoint accepts a single file_id).

     上传入口就在输入框上方：按钮常驻，已附带的文件渲染成它右边的 chip。
     ============================================================ */
  function renderAttachments(){
    if(!attachList) return;
    attachList.innerHTML = '';
    const list = state.attachments;

    if(!list.length){
      const hint = document.createElement('span');
      hint.className = 'attach-hint';
      hint.textContent = '可上传 PDF / DOCX / XLSX / CSV / MD / TXT，解析后随下一轮提问发送';
      attachList.appendChild(hint);
      updateKbCount();
      return;
    }

    if(list.length > 1){
      const more = document.createElement('span');
      more.className = 'attach-hint';
      more.textContent = '每轮携带 1 个';
      attachList.appendChild(more);
    }

    list.forEach(a=>{
      const chip = document.createElement('span');
      chip.className = 'attach';
      const meta = a.row_count ? (a.row_count + ' 行') : (a.file_type || 'file');
      chip.innerHTML =
        '<span class="an"></span>' +
        '<span class="am">' + escapeHtml(meta) + '</span>' +
        '<button class="ax" type="button" aria-label="移除附件"><i data-icon="x"></i></button>';
      chip.querySelector('.an').textContent = a.filename;
      chip.querySelector('.ax').addEventListener('click', ()=>{
        state.attachments = state.attachments.filter(x => x.file_id !== a.file_id);
        renderAttachments();
      });
      attachList.appendChild(chip);
    });
    icons();
    updateKbCount();
  }

  function updateKbCount(){
    const n = state.docs.length;
    kbCount.textContent = n ? ('已上传 ' + n + ' 个') : '0 个文件';
  }

  /* ---------- upload module ---------- */
  /* 与 tools/file_handler.py 的 SUPPORTED_EXTENSIONS 保持一致：
     旧版 .doc/.xls/.ppt 是 OLE 二进制，后端会明确拒绝，所以这里也不放行。 */
  const SUPPORTED = ['pdf','docx','xlsx','xlsm','md','markdown','txt','text','log','csv','tsv','json','jsonl','ndjson'];
  function fmtSize(b){
    if(b < 1024) return b + ' B';
    if(b < 1048576) return (b/1024).toFixed(1) + ' KB';
    return (b/1048576).toFixed(1) + ' MB';
  }
  function extOf(name){ const m = String(name).match(/\.([A-Za-z0-9]+)$/); return (m ? m[1] : 'file').toLowerCase(); }
  function iconFor(ext){
    if(ext === 'pdf') return 'file-text';
    if(ext === 'docx') return 'file-type-2';
    if(ext === 'xlsx' || ext === 'xlsm') return 'list-checks';
    if(ext === 'md' || ext === 'markdown') return 'file-code';
    return 'file';
  }
  function openUpload(){ uploadModal.hidden = false; requestAnimationFrame(()=> uploadModal.classList.add('open')); }
  function closeUpload(){
    uploadModal.classList.remove('open');
    setTimeout(()=>{
      uploadModal.hidden = true;
      /* files that made it into the attachment bar no longer need staging */
      if(state.queueFiles.some(f => f.status === 'ok')){
        state.queueFiles = state.queueFiles.filter(f => f.status !== 'ok');
        renderFiles();
      }
    }, 380);
  }
  function addFiles(list){
    let rejected = 0;
    [...list].forEach(f=>{
      const ext = extOf(f.name);
      if(!SUPPORTED.includes(ext)){ rejected++; return; }
      state.queueFiles.push({ file:f, name:f.name, size:f.size, ext, status:'idle' });
    });
    if(rejected) toastShow('已跳过 ' + rejected + ' 个不支持的文件类型', true);
    renderFiles();
  }
  function removeFile(i){
    const f = state.queueFiles[i];
    if(!f || f.status === 'up') return;
    /* removing an already-parsed row also drops its pending attachment */
    if(f.status === 'ok' && f.file_id){
      state.attachments = state.attachments.filter(a => a.file_id !== f.file_id);
      renderAttachments();
    }
    state.queueFiles.splice(i,1);
    renderFiles();
  }
  function statusText(f){
    if(f.status === 'up')  return '上传中…';
    if(f.status === 'ok')  return '已解析';
    if(f.status === 'err') return f.error || '失败';
    return null;
  }
  function renderFiles(){
    const q = state.queueFiles;
    fileList.innerHTML = '';
    if(!q.length){
      const e = document.createElement('div');
      e.className = 'file-list-empty'; e.textContent = '尚未添加文件';
      fileList.appendChild(e);
    } else {
      q.forEach((f,i)=>{
        const row = document.createElement('div');
        row.className = 'file-row' + (f.status ? ' is-' + f.status : '');
        const st = statusText(f);
        const a = f.analysis;
        const sub = a
          ? [a.file_type, (a.row_count ? a.row_count + ' 行' : null), (a.columns && a.columns.length ? a.columns.length + ' 列' : null)]
              .filter(Boolean).join(' · ')
          : fmtSize(f.size);
        row.innerHTML =
          '<span class="fic"><i data-icon="' + (f.status === 'ok' ? 'check' : iconFor(f.ext)) + '"></i></span>' +
          '<span class="fmeta"><span class="fname"></span>' +
          '<span class="fsub"><span class="badge">' + f.ext.toUpperCase() + '</span><span>' + escapeHtml(sub) + '</span>' +
          (st ? '<span class="fst">' + escapeHtml(st) + '</span>' : '') + '</span></span>' +
          '<button class="fdel" type="button" aria-label="移除文件"><i data-icon="x"></i></button>';
        row.querySelector('.fname').textContent = f.name;
        row.querySelector('.fdel').addEventListener('click', ()=> removeFile(i));
        fileList.appendChild(row);
      });
    }
    icons();
    uploadCount.textContent = q.length;
    uploadGo.disabled = !q.length || q.every(f => f.status === 'up');
    const pending = q.filter(f => f.status !== 'ok').length;
    fileHint.textContent = q.length
      ? (pending ? (pending + ' 个文件待上传') : '解析完成，已加入待发送附件')
      : '支持 PDF / DOCX / XLSX / CSV / MD / TXT';
  }

  async function doUpload(){
    const q = state.queueFiles;
    if(!q.length) return;
    const todo = q.filter(f => f.status !== 'ok');
    if(!todo.length){ toastShow('文件已在待发送附件中'); closeUpload(); return; }

    uploadGo.disabled = true;
    let ok = 0, bad = 0;
    for(const f of todo){
      f.status = 'up';
      renderFiles();
      try{
        const fd = new FormData();
        fd.append('file', f.file, f.name);
        fd.append('org_id', ID.org_id);      /* must match the org that asks later */
        const res = await fetch(API.upload, { method:'POST', body: fd });
        const data = await res.json().catch(()=> ({}));
        if(!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));

        f.status = 'ok';
        f.file_id = data.file_id;
        f.analysis = data;
        state.attachments.push({
          file_id: data.file_id,
          filename: data.filename || f.name,
          file_type: data.file_type,
          row_count: data.row_count,
          columns: data.columns || [],
          preview: data.preview,
          analysis_type: data.analysis_type
        });
        state.docs.push({ file_id: data.file_id, filename: data.filename || f.name, at: Date.now() });
        store.set(K_DOCS, state.docs);
        ok++;
      }catch(err){
        f.status = 'err';
        f.error = err.message || '上传失败';
        bad++;
      }
      renderFiles();
    }
    renderAttachments();
    updateKbCount();
    uploadGo.disabled = !state.queueFiles.length;
    if(bad) toastShow(ok + ' 个成功 / ' + bad + ' 个失败', true);
    else toastShow(ok + ' 个文件已就绪，将随下一轮提问发送');
  }

  /* ---------- sandbox status ---------- */
  /* 后端在不可用时给出 code + reason：连不上 / 密钥不匹配 / 创建超时……
     以前只有一句「无沙箱」，401 得去翻服务端日志才知道是密钥两边不一致。 */
  const SANDBOX_LABEL = {
    not_initialized: '沙箱未初始化',
    not_probed:      '沙箱未探测',
    unreachable:     '沙箱未启动',
    auth_mismatch:   '沙箱密钥不匹配',
    create_timeout:  '沙箱镜像未就绪',
    create_failed:   '沙箱创建失败',
    http_error:      '沙箱接口异常'
  };
  async function refreshSandboxStatus(){
    if(!engineState) return;
    /* 代码执行关闭的部署（默认）：不显示沙箱状态，否则会看到「沙箱未启动」
       而误以为是故障。此时显示的是对话模式的定位。 */
    if(RUNTIME.code_execution === false){
      engineState.classList.remove('degraded');
      engineState.innerHTML = '<span class="dot"></span>对话模式';
      engineState.title = '本部署不执行代码：贴代码进来，我会直接讲给你听。';
      engineState.style.cursor = 'help';
      return;
    }
    try{
      const s = await api(API.sandbox);
      const on = !!(s && s.available);
      engineState.classList.toggle('degraded', !on);
      const label = on ? ('sandbox · ' + (s.active_count || 0)) : (SANDBOX_LABEL[s.code] || '对话模式 · 无沙箱');
      engineState.innerHTML = '<span class="dot"></span>' + label;
      engineState.title = s && s.reason ? s.reason : '';
      engineState.style.cursor = (s && s.reason) ? 'help' : '';
    }catch(e){
      engineState.classList.add('degraded');
      engineState.innerHTML = '<span class="dot"></span>offline';
      engineState.title = e.message || '';
    }
  }

  /* 额度条。后端按「签名身份 + 档位」计量 token，用完了后续请求会 429，
     所以这里提前把余量摆出来，免得用户毫无预兆地撞上拒绝。
     游客档很小（2 万），基本几条就到顶——所以顺带做注册引导。 */
  const quotaBar = $('#quotaBar');
  const quotaValue = $('#quotaValue');
  const quotaFill = $('#quotaFill');
  const quotaLabel = $('#quotaLabel');
  function fmtTokens(n){
    if(!n) return '0';
    if(n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if(n >= 1000) return Math.round(n / 1000) + 'k';
    return String(n);
  }
  async function refreshUsage(){
    if(!quotaBar) return;
    try{
      const u = await api('/api/usage');
      if(!u || !u.tokens_budget){ quotaBar.hidden = true; return; }   /* 0 = 不限 */
      const used = u.tokens_used || 0;
      const budget = u.tokens_budget;
      const pct = Math.min(100, Math.round(used * 100 / budget));
      quotaBar.hidden = false;
      quotaValue.textContent = fmtTokens(used) + ' / ' + fmtTokens(budget);
      quotaFill.style.width = pct + '%';
      quotaBar.classList.toggle('is-low', pct >= 80);
      /* 档位以后端返回的为准：它才是真正在计量的那一方 */
      if(u.tier) ACCOUNT.tier = u.tier;
      quotaBar.title = (ACCOUNT.registered ? '今日' : '游客体验')
        + '已用 ' + used + ' tokens（上限 ' + budget + '）';
      /* 游客额度快见底时把注册入口点亮，别等他撞到 429 才知道有这回事 */
      if(isGuest() && pct >= 70){
        quotaBar.classList.add('is-low');
        quotaBar.title += ' —— 注册后可获得完整额度';
      }
    }catch(e){
      quotaBar.hidden = true;
    }
  }

  /* ---------- auth modal ---------- */
  let authMode = 'login';

  function setAuthMode(mode){
    authMode = mode === 'register' ? 'register' : 'login';
    const reg = authMode === 'register';
    if(tabLogin) tabLogin.classList.toggle('is-on', !reg);
    if(tabRegister) tabRegister.classList.toggle('is-on', reg);
    if(authTitle) authTitle.textContent = reg ? '注册账号' : '登录';
    if(authSub) authSub.textContent = reg
      ? '注册后额度大幅提高，历史会话绑定账号，换设备也能看到'
      : '游客只能体验很少的额度；登录后恢复完整额度';
    if(authGoLabel) authGoLabel.textContent = reg ? '注册并登录' : '登录';
    if(authPass) authPass.setAttribute('autocomplete', reg ? 'new-password' : 'current-password');
    if(authNote){
      authNote.textContent = ACCOUNT.registered
        ? ('当前账号：' + (ACCOUNT.username || ''))
        : '游客体验额度：每天约 2 万 token、每分钟 3 次';
    }
    hideAuthError();
  }

  function showAuthError(msg){
    if(!authError) return;
    authError.textContent = msg || '';
    authError.hidden = !msg;
  }
  function hideAuthError(){ showAuthError(''); }

  function openAuth(mode){
    if(!authModal) return;
    setAuthMode(mode || (ACCOUNT.registered ? 'login' : 'register'));
    authModal.hidden = false;
    requestAnimationFrame(()=> authModal.classList.add('open'));
    document.body.classList.add('modal-open');
    setTimeout(()=> { if(authUser) authUser.focus(); }, 30);
  }
  function closeAuth(){
    if(!authModal) return;
    authModal.classList.remove('open');
    authModal.hidden = true;
    document.body.classList.remove('modal-open');
    hideAuthError();
    if(authPass) authPass.value = '';
  }

  async function submitAuth(ev){
    if(ev) ev.preventDefault();
    if(!authUser || !authPass) return;
    const username = authUser.value.trim();
    const password = authPass.value;
    if(!username || !password){ showAuthError('请填写用户名和密码。'); return; }

    const url = authMode === 'register' ? '/api/auth/register' : '/api/auth/login';
    authGo.disabled = true;
    hideAuthError();
    try{
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ username, password })
      });
      const data = await res.json().catch(()=> ({}));
      if(!res.ok){
        showAuthError(data.detail || ('操作失败 HTTP ' + res.status));
        return;
      }
      ACCOUNT.registered = true;
      ACCOUNT.tier = data.tier || 'member';
      ACCOUNT.username = (data.user && data.user.username) || username;
      renderAccount();
      closeAuth();
      hideAuthError();
      toastShow(authMode === 'register' ? '注册成功，已登录' : '已登录');
      /* 登录会换档位（也是换额度），要拉一次用量让进度条立刻反映新额度 */
      refreshUsage();
      /* 历史是按 user_id 存的：登录后命名空间变了，重新拉会话列表 */
      await loadThreads();
      applyBlank();
    }catch(e){
      showAuthError('网络错误：' + (e.message || '请稍后重试'));
    }finally{
      authGo.disabled = false;
    }
  }

  async function logout(){
    try{
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
    }catch(e){ /* 清本地状态即可 */ }
    ACCOUNT.registered = false;
    ACCOUNT.tier = 'guest';
    ACCOUNT.username = null;
    renderAccount();
    toastShow('已退出登录');
    refreshUsage();
    await loadThreads();
    applyBlank();
  }

  /* 游客不能用上传：文件解析一次性吃掉的上下文远超游客额度（2 万 token），
     放行只会得到"传了文件却分析不出来"。后端也会拒（_require_member），
     前端这里只是别让按钮看起来能用。 */
  function applyGuestGating(){
    const guest = isGuest();
    if(uploadBtn){
      uploadBtn.disabled = guest;
      uploadBtn.title = guest ? '上传文件需要注册账号' : '';
      uploadBtn.classList.toggle('is-locked', guest);
    }
    if(attachBar) attachBar.classList.toggle('is-locked', guest);
  }

  /* ---------- wire up ---------- */
  input.addEventListener('input', autoGrow);
  input.addEventListener('focus', ()=> composer.classList.add('focus'));
  input.addEventListener('blur',  ()=> composer.classList.remove('focus'));
  input.addEventListener('keydown', e=>{
    if(e.isComposing || e.keyCode === 229) return;   /* let the IME finish */
    if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); }
  });
  sendBtn.addEventListener('click', sendMessage);
  newBtn.addEventListener('click', newChat);
  newTop.addEventListener('click', newChat);
  menuBtn.addEventListener('click', ()=> setRail(true));
  railClose.addEventListener('click', ()=> setRail(false));
  scrim.addEventListener('click', ()=> setRail(false));

  uploadBtn.addEventListener('click', ()=>{
    /* 游客点了上传：直接引导注册，而不是让他传完文件再被后端拒 */
    if(isGuest()){
      toastShow('上传文件需要注册账号，注册是免费的');
      openAuth('register');
      return;
    }
    openUpload();
  });
  uploadClose.addEventListener('click', closeUpload);
  uploadModal.addEventListener('click', e=>{ if(e.target === uploadModal) closeUpload(); });
  document.addEventListener('keydown', e=>{ if(e.key === 'Escape' && !uploadModal.hidden) closeUpload(); });
  dropzone.addEventListener('click', ()=> fileInput.click());
  dropBtn.addEventListener('click', e=>{ e.stopPropagation(); fileInput.click(); });
  fileInput.addEventListener('change', ()=>{ addFiles(fileInput.files); fileInput.value = ''; });
  ['dragover','dragenter'].forEach(ev => dropzone.addEventListener(ev, e=>{ e.preventDefault(); dropzone.classList.add('over'); }));
  ['dragleave','dragend'].forEach(ev => dropzone.addEventListener(ev, ()=> dropzone.classList.remove('over')));
  dropzone.addEventListener('drop', e=>{ e.preventDefault(); dropzone.classList.remove('over'); addFiles(e.dataTransfer.files); });
  uploadGo.addEventListener('click', doUpload);

  /* ---------- auth wiring ---------- */
  if(acctOpen) acctOpen.addEventListener('click', ()=>{
    if(ACCOUNT.registered){ openAuth('login'); return; }
    openAuth('register');
  });
  if(acctLogout) acctLogout.addEventListener('click', (ev)=>{ ev.stopPropagation(); logout(); });
  if(authClose) authClose.addEventListener('click', closeAuth);
  if(authModal) authModal.addEventListener('click', ev=>{ if(ev.target === authModal) closeAuth(); });
  if(tabLogin) tabLogin.addEventListener('click', ()=> setAuthMode('login'));
  if(tabRegister) tabRegister.addEventListener('click', ()=> setAuthMode('register'));
  if(authForm) authForm.addEventListener('submit', submitAuth);
  if(authGo) authGo.addEventListener('click', submitAuth);
  document.addEventListener('keydown', ev=>{
    if(ev.key === 'Escape' && authModal && authModal.classList.contains('open')) closeAuth();
  });

  /* ---------- boot ---------- */
  async function restoreActiveThread(){
    const id = store.get(K_ACTIVE, null);
    if(!id) return false;
    state.threadId = id;
    try{
      const data = await api(API.history(id));
      state.messages = (data.messages || [])
        .filter(m => m.role === 'user' || m.role === 'assistant')
        .map(m => ({ role: m.role, content: m.content || '', reasoning: m.reasoning || null }));
    }catch(e){
      state.messages = [];
      /* 身份迁移后的旧会话：直接丢掉本地记录并提示一次，别让它每轮开屏都来一次 403。
         其他错误保持原样静默——恢复失败不算致命。 */
      if(isStaleSessionError(e)){
        dropThreadLocally(id);
        toastShow('上次的会话来自旧版本，已作废；已为你新建会话。');
        return false;
      }
    }
    if(!state.messages.length){ state.threadId = null; saveActive(null); return false; }
    rememberThread(id, state.messages.length);
    crumbTitle.textContent = titleOf(id) || firstUserLine(state.messages) || '会话';
    crumbMeta.textContent = META_DEFAULT + ' · ' + shortId(id);
    renderThread();
    return true;
  }

  async function boot(){
    /* 身份必须最先取：thread_id 由 org__user__后缀 拼成，而下面每一步
       （恢复会话、列会话、发消息）都依赖它。
       账号状态要紧跟其后：它决定额度档位，也决定上传按钮可不可用。 */
    await loadIdentity();
    await loadRuntimeConfig();
    await loadAccountState();

    initParallax();
    icons();
    renderFiles();
    renderAttachments();
    updateKbCount();
    syncSendState();
    applyGuestGating();
    /* 用 DOM 里已有的字做「乱码归位」动画：改产品名只需要动 index.html 一处，
       不会再出现「标题换了、动画里还是旧名字」这种漏改。 */
    scrambleText(wordmark, wordmark.dataset.scramble || wordmark.textContent, 1100);

    const restored = await restoreActiveThread();
    if(!restored) renderThread();

    await loadThreads();
    refreshSandboxStatus();
    refreshUsage();
    if(window.innerWidth >= 900) input.focus();
  }
  boot();
})();
