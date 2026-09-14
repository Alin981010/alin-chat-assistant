# Alin Chat Assistant 工作区指令

本文件在**每次新会话的第一次请求**时自动加载，用于让新会话不必重新摸索本项目的坑。

> **本仓库是公开的**（GitHub `Alin981010/alin-chat-assistant`，public；Gitee 镜像
> `xiang-wanlin1024/alin-chat-assistant`）。
> 因此本文件里不写任何服务器地址、密钥路径、账号等运行环境信息——
> 那些放在**机器全局**的 `~/.dsh/AGENTS.md`（不在任何 git 仓库里，
> 删项目、换工作区都不会丢）。密钥本身一律只存在于 `.env`，绝不入库。

---

## 本项目已知的坑（都真实踩过）

### 1. PostgreSQL 18 的卷路径变了
18 起数据目录按大版本号分子目录存放，官方镜像**要求把卷挂在 `/var/lib/postgresql`**。
沿用旧写法的 `/var/lib/postgresql/data` 会让容器无限重启，日志里报
`there appears to be PostgreSQL data in: /var/lib/postgresql/data`。

### 2. `start` / `restart` 不会重读 `.env`
环境变量只在容器**创建**时注入。改了 `.env` 必须 `docker compose up -d` 重建，
否则容器里还是旧值——表现为"我明明改了 key 怎么还报 401"。

### 3. `unless-stopped` 的语义
容器被**手动 stop** 过之后，服务器重启不会自动拉起它。这是 `unless-stopped`
的本意："除非你手动停过"。想让它无论如何都自启，得用 `restart: always`。
`docker compose down` 更彻底——它直接删掉容器，之后 `docker start` 会报
`No such container`。

### 4. PowerShell → ssh 的 CRLF 污染（高频）
把 PowerShell here-string 用管道喂给 `ssh ... 'bash -s'` 时，**脚本最后一行会被追加
一个 `\r`**。于是 `tail -n 6` 变成 `tail -n 6\r`、`systemctl is-active docker` 变成
查一个叫 `docker\r` 的假 unit，报 "invalid number of lines" / "Invalid unit name"。

修法（远端先剥 CR）：
```powershell
($script -replace "`r`n","`n") | ssh ... 'tr -d "\r" | bash -s'
```
同理，**经 stdin 传值也要在远端 `.strip()`**——否则值尾巴会带上 `\r`，写进 `.env`
后就是一个肉眼看不出来、但必然鉴权失败的字符。

### 5. 国内服务器的镜像源
`registry-1.docker.io` 直连超时。腾讯云**内网**源
`https://mirror.ccs.tencentyun.com`（解析到 169.254.0.51，仅腾讯云机器可用）要排在
`/etc/docker/daemon.json` 的 `registry-mirrors` **第一位**。ghcr.io 的 blob 会卡死，
所以 `Dockerfile` 里 uv 是从清华 PyPI 装的，没用官方推荐的
`COPY --from=ghcr.io/astral-sh/uv`。

### 6. 永远不要在真实 `.env` 旁边跑 `docker compose config`
它会**把 env_file 解析后的内容原样回显**，包括 API key。要校验语法就把输出丢进
`/dev/null`，或换个没有真实 `.env` 的目录。这个坑已经泄漏过一次真实 key。

### 7. 服务没有任何鉴权
接口全开放、CORS 是 `*`、容器里握着真实 API key。放行端口就等于把额度挂在公网上。
安全组来源应收紧到具体 IP `/32`，或前面加带 basic auth 的反代。

### 8. 匿名身份与授权（2026-09 起）
- 没有登录，但每个浏览器有服务端签发的 **HMAC 签名身份**（`app/identity.py`）。
  `org_id` 是沙箱容器的隔离键，不再由前端写死；请求里的 `org_id` 与签名不符一律
  403，`/api/sandbox/download` 的 `path` 必须落在 `/workspace` 下。
- 改 `APP_SECRET_KEY` 会让**所有访客身份失效**（文件还在，但会换容器）。不配置时
  由 `DB_URI` 派生，所以**改数据库口令等价于换签名密钥**。

### 9. token 消耗的防线与两档额度（`app/budget.py` + `app/limits.py`）

**分游客与注册用户两档**，档位由签名身份决定（客户端声明无效）：

| | 游客 | 注册用户 |
|---|---|---|
| 每天 token | 2 万 | 200 万 |
| 每分钟次数 | 3 | 8 |
| 单条上限 | 200 tokens | 6 万 tokens |
| 上传文件 | 拒绝（403） | 允许 |
| 每 IP 每天游客总量 | 6 万 | 不受此限 |

全局另有：每 IP 30 次/分、`max_tokens` 4096、`recursion_limit` 40。

- 计数键是 **`tier:org`**（如 `guest:o123`），两档各记各的账。改动 `_usage`
  相关代码时别忘了这个前缀——测试里踩过。
- `TokenBudget` 的三个"每档不同"的参数默认是 **None** = 用该档自己的默认值。
  **千万别给具体默认值**（比如 200 万），那会同时盖掉游客档，"游客额度调低"
  就悄悄失效了。
- 用量写进 Postgres 的 `usage_budget`，**重启不清零**；账号在 `users` 命名空间。
- 按身份的游客额度以 cookie 为载体，**清 cookie 即可重置**；真兜底是
  `GUEST_DAILY_TOKEN_BUDGET_PER_IP`。
- 回归用例：`python tests/test_auth.py`（74 条）、`tests/test_budget.py`（40 条）、
  `tests/test_isolation.py`（38 条）、`tests/test_general_tools.py`（44 条）。

### 10. 定位是通用对话助手，代码默认不执行（2026-09 起）
- `ENABLE_CODE_EXECUTION` 默认 **false**：agent 不挂 `execute`
  （由 deepagents 的 `_ToolExclusionMiddleware` 在模型调用与工具调用两处摘掉），
  写代码/调试/解释代码一律走对话——用户贴代码、助手讲代码。
- 关闭时**不初始化沙箱**（`app/__init__.py` 的 lifespan），省掉十几个 GB 的镜像拉取；
  大文件只给预览，`_prepare_message_context` 与 `_build_env_footer` 都会改走
  「没有执行能力」的文案——**改这两处时注意别让提示词答应一件做不到的事**。
- 通用工具在 `tools/general_tools.py`：`get_current_time`（离线，zoneinfo）、
  `get_weather`（Open-Meteo，无需 key，需外网）、`convert_units`（离线）。
- 系统提示词由 `build_agents_memory()` 拼装，助手名字走 `ASSISTANT_NAME`；
  改能力清单/代码约定就改 `AGENTS_MEMORY_BASE`。
- 新增前端可见开关时走 `GET /api/config`（目前有 `code_execution`、`assistant_name`）。

### 11. 行尾：仓库是 LF，工作区可能是 CRLF
开发机全局 `core.autocrlf=true`，所以**工作区是 CRLF、仓库存 LF**。两个真实后果：

- `git status` 会显示"已修改"而 `git diff` 是空的（纯行尾差异），别被它骗，
  也别急着提交；
- **用 GitHub Git Data API 补推文件是按工作区原始字节造 blob 的**，会把 CRLF 写进
  仓库（实测踩过：`app/routes.py` 变成 1246 行全 CRLF，与其余文件不一致，
  tree 哈希永远对不上本地）。走 API 推文件前，务必先把内容规范成 LF。

仓库已加 `.gitattributes`（`* text=auto eol=lf`）锁住这件事。

### 12. 前端回归怎么跑（没有 headless Chrome 时）

`tests/e2e.js` 需要 headless Chrome，受限环境里起不来（进程都不出现）。
「过期会话自愈」这条路径由 `tests/test_stale_session.js` 覆盖：它用 Node 的 `vm`
加载**真实的** `static/js/app.js`，只把 DOM 与 fetch 换成桩，验证
「旧会话 403 → 清本地记录 → 换新身份」以及「正常会话不被误清」。

`app.js` 结尾会立即调 `boot()`（async），断言前要让微任务跑完（见该文件里的
`runAppSettled`）——否则测到的是"还没开始"。


### 13. 用户系统与账号（2026-09 起）
- `app/auth.py`：用户名+密码注册登录，PBKDF2-HMAC-SHA256（60 万次迭代、每用户随机盐、
  标准库实现不引 bcrypt）。存储格式 `pbkdf2_sha256$迭代$盐$摘要`，自描述。
- **登录状态只认签名会话 cookie**（`HttpOnly`，30 天）。身份 cookie 里的 `tier`
  只接受 `guest`/`member`，其他值降级游客；请求体里声明档位无效。
- **登出必须同时把身份 cookie 降回 guest**：只删会话 cookie 的话，身份 cookie 里
  还写着 `tier=member`，用户清掉会话 cookie 就能白拿会员额度（实测踩过，
  `tests/test_auth.py` 有对应断言）。
- 账号存在 PostgresStore 的 `users` 命名空间（`/u_<id>.json` + `/index.json` 索引），
  启动时载入内存。**`/index.json` 丢了账号就读不出来**，别手工删它。
- 用户不存在时也跑一次等价耗时的假校验，避免用响应时间探测账号是否存在。

### 14. `[hidden]` 必须真的隐藏（CSS 坑）
`.modal-overlay` 是 `display:grid`，会盖掉浏览器默认的 `[hidden]{display:none}`。
结果：带 `hidden` 的弹窗其实仍铺满屏幕（只是 `opacity:0` 看不见），**拦截整页点击**。
`app.css` 顶部加了 `[hidden]{display:none !important}` 把它钉死——加新弹窗时
记得同时用 `hidden` 属性与 `.open` class（两者都控制显隐，缺一会出问题）。

---

## 代码侧

- 入口：`uvicorn app:app`（`main.py` 把地址硬编码成 127.0.0.1，容器里不要用它）
- 依赖：`uv sync --frozen --no-dev`，锁文件已校验；`python-pptx` 属 dev 组，
  真正生成 PPT 的是**沙箱里的**解释器，不需要装进应用镜像
- 单 worker 是刻意的：agent / store / 沙箱缓存都挂在进程内全局变量上
- 构建自检：`python -c "import app"` 会把 FastAPI/DeepAgents/LangGraph/psycopg
  整条链路拉起来，缺依赖直接构建失败
- 代码沙箱（OpenSandbox）**当前未部署**，且**默认不启用**：聊天、文件问答、
  通用工具、长期记忆都正常；要代码执行/大文件分析就设
  `ENABLE_CODE_EXECUTION=true`（需另起 OpenSandbox server，镜像十几个 GB，
  小规格机器不划算）。
