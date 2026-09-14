# 部署到服务器

把当前代码部署到自建服务器并验证可用的完整步骤。

> **连接信息（地址 / 端口 / 用户名 / 私钥路径）在机器全局的 `~/.dsh/AGENTS.md`。**
> 本文件会被提交到公开仓库，所以只写步骤，不写任何地址与密钥。
> 下文用 `$HOST` / `$USER` / `$KEY` 指代，取值从全局文件读。

---

## 0. 动手前必须知道的四件事

1. **改了 `.env` 必须 `docker compose up -d` 重建容器。**
   `start` / `restart` 不会重读 `.env`，表现为"我明明改了 key 还报 401"。

2. **镜像 / 容器 / 网络在 2026-09 改过名**：`alinagent:*` → `alin-chat-assistant:*`，
   网络 → `alin`。新 compose 启动会创建新容器，**旧的 `alinagent-app` /
   `alinagent-db` 会变成孤儿**，必须先 `down --remove-orphans` 清掉。

3. **数据卷名不变**（`alinagent_pgdata` = 部署目录名 + `pgdata`），
   所以 `down` **不带 `-v`** 就不会丢数据。**千万别顺手加 `-v`**，那是删库。

4. **`org_id` 现在由服务端签名**（`app/identity.py`）。直接 curl 打 `/api/chat`
   而没带合法 cookie 会被 **403**——见第 7 步的说明。

---

## 1. 本地自检：先构建，别把问题带上服务器

```powershell
cd E:\WorkSpace\alinAgent
docker build -t alin-chat-assistant:dev .
```

Dockerfile 里带 `RUN python -c "import app"` 自检，会把 FastAPI / DeepAgents /
LangGraph / psycopg 整条链路拉起来，依赖缺失或版本不匹配会**在这里直接失败**。

再跑一遍回归用例（改了代码就更要跑）：

```powershell
.\.venv\Scripts\python.exe tests\test_budget.py
.\.venv\Scripts\python.exe tests\test_isolation.py
.\.venv\Scripts\python.exe tests\test_general_tools.py
```

## 2. 打包（务必排除 `.env` / `.venv` / `.git` / `__pycache__`）

```powershell
$stage = Join-Path $env:TEMP 'deploy-stage'
$tgz   = Join-Path $env:TEMP 'alin-src.tar.gz'
Remove-Item $stage, $tgz -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
Copy-Item app,static,skills,tools,tests,main.py,pyproject.toml,uv.lock,README.md,
          Dockerfile,.dockerignore,docker-compose.yml,.env.example,DEPLOY.md,AGENTS.md `
          -Destination $stage -Recurse
Get-ChildItem $stage -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
tar -czf $tgz -C $stage .
```

**上面那个文件列表里绝不能出现 `.env`。** 服务器上那份 `.env` 是独立的、
手动维护的，本地这份含你自己的 key，传过去只会覆盖出事故。

## 3. 上传并解包到**新目录**

```powershell
scp -i $KEY -P 22 $tgz "${USER}@${HOST}:/home/$USER/"
ssh -i $KEY -p 22 -o BatchMode=yes -o ConnectTimeout=10 "$USER@$HOST" `
  'set -e; cd ~; rm -rf alinagent.new; mkdir alinagent.new; tar -xzf alin-src.tar.gz -C alinagent.new; ls alinagent.new'
```

解到 `alinagent.new` 而不是直接覆盖 `alinagent`：解包失败时线上目录不会半残。

> ssh 必须带 `-o BatchMode=yes`。非交互执行时，密码提示会把进程挂死。

## 4. 在服务器上构建镜像

```bash
cd ~/alinagent.new
docker build -t alin-chat-assistant:latest .
```

若卡在拉 `python:3.14-slim`：确认 `/etc/docker/daemon.json` 的 `registry-mirrors`
第一位是 `https://mirror.ccs.tencentyun.com`（腾讯云内网源，仅腾讯云机器可用）。
原文件备份在同目录 `daemon.json.bak.*`。

## 5. 对齐服务器上的 `.env`

服务器 `~/alinagent/.env` 至少要有下面这些。**`DB_URI` 不要写**——它由 compose
按容器网络注入，写死 `127.0.0.1` 会在容器里指向容器自己。

| 变量 | 必要性 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 必填 | 模型调用 |
| `POSTGRES_PASSWORD` | 必填 | compose 用它做变量替换并初始化数据库 |
| `APP_SECRET_KEY` | **对外部署必填** | 不配会由 `DB_URI` 派生（打 warning）。改它 = 所有访客身份失效 |
| `APP_ALLOWED_ORIGINS` | 对外部署建议填 | 逗号分隔的真实站点；留空 = 允许所有来源 |
| `APP_COOKIE_SECURE` | 上 HTTPS 后置 `true` | 否则 cookie 在 HTTP 下也会发送 |
| `ENABLE_CODE_EXECUTION` | 默认 `false` | 打开需另起 OpenSandbox（镜像十几个 GB） |
| `CHAT_DAILY_TOKEN_BUDGET` 等 | 有默认值 | 额度与限流，见 `.env.example` |

生成签名密钥：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**改完 `.env` 记得第 0 节第 1 条。**

### 怎么确认这几项真的生效（别只看 `.env` 写了）

写进 `.env` 不等于生效——环境变量只在容器**创建**时注入，`restart` 不会重读。
下面几条都能在服务器上直接跑，是各自的验收命令：

```bash
cd ~/alinagent

# 1) APP_SECRET_KEY：启动日志里不该再出现「未配置」这条 warning
docker compose logs app 2>&1 | grep -c "APP_SECRET_KEY 未配置"     # 期望 0

# 1b) 换密钥会让旧 cookie 全部作废：伪造签名的 cookie 应被换成全新随机身份
curl -s --cookie "alinagent_id=abc.def" localhost:8088/api/identity

# 1c) 密钥要能跨重启：同一个 cookie 在 restart 前后应拿到同一个 org
curl -s -c /tmp/c.txt -o /dev/null localhost:8088/api/identity
curl -s -b /tmp/c.txt localhost:8088/api/identity        # 记下 org
docker compose restart app && sleep 20
curl -s -b /tmp/c.txt localhost:8088/api/identity        # 应该还是同一个 org

# 2) APP_ALLOWED_ORIGINS：白名单内返回该头，陌生来源不返回（浏览器据此拦截）
curl -s -i -X OPTIONS localhost:8088/api/chat/stream \
  -H "Origin: http://<你的地址>:8088" -H "Access-Control-Request-Method: POST" \
  | grep -i access-control-allow-origin                   # 期望有
curl -s -i localhost:8088/api/config -H "Origin: https://evil.example.com" \
  | grep -ci access-control-allow-origin                  # 期望 0

# 3) 服务本身
curl -s localhost:8088/health
```

> **`APP_COOKIE_SECURE` 只有上了 HTTPS 才能置 true**。置 true 后 cookie 仅在 HTTPS
> 下发送，而现在用的是 `http://<IP>:8088` 明文访问，改了会导致身份无法保持。
> 等反向代理加了 TLS、改用 https 访问之后，再同时改这一项。

> **安全组收窄到某个 `/32` 之前，先确认那个 IP 就是你自己**：从服务器上看不出来；
> 在本地机器上查到的出口 IP 可能是代理/VPN 的地址，填错的结果是陌生人被挡住、
> 你自己也进不去。稳妥做法是先在控制台加一条新的放行规则、用自己的浏览器实测能通，
> 再删掉原来的 `0.0.0.0/0`。

## 6. 切换并启动

```bash
cd ~/alinagent
docker compose down --remove-orphans      # 清掉旧名的孤儿容器；不带 -v，卷保留

cd ~/alinagent.new
docker compose up -d --no-build           # 用第 4 步构建好的镜像
```

## 7. 验证——缺一步都不算完成

服务器内部：

```bash
cd ~/alinagent.new
docker compose ps                    # 两个都该是 healthy
curl -s localhost:8088/health        # {"status":"ok","agent_ready":true,...}
curl -s localhost:8088/api/config    # 确认 code_execution / assistant_name 符合预期
```

从**本机走公网**再验一次——这一步才会真正调用模型、验证 key 与落库：

```powershell
try {
  $r = Invoke-WebRequest -Uri "http://${HOST}:8088/health" -TimeoutSec 10 -UseBasicParsing
  "health: $($r.StatusCode) $($r.Content)"
} catch { "公网不通：$($_.Exception.Message)（多半是安全组没放行）" }
```

然后**用浏览器**打开 `http://$HOST:8088` 发一条消息。

> 为什么不直接 curl `/api/chat`：该接口现在要求合法的身份 cookie，`org_id` 与
> 服务端签名不符会返回 **403**。浏览器流程会自动拿到 cookie，是最省事的验证方式。
> 要在脚本里验证就先 GET `/` 取 cookie 再带着打。

## 8. 收尾：保留上一版以便回滚

```bash
cd ~
rm -rf alinagent.bak
mv alinagent alinagent.bak
mv alinagent.new alinagent
```

回滚就是把两个目录名换回来，再 `docker compose up -d --no-build`。

> **原地部署也必须留备份。** 若图省事直接把代码解压覆盖 `~/alinagent`（本文档第 3 步
> 的 `alinagent.new` 就是为了避免这个），请先手动归档：
> `tar -czf ~/alinagent-deployed-$(date +%Y%m%d).tar.gz --exclude=.env .`
> 否则一旦新版起不来，既没有旧目录可切换，也没有可回滚的源码。

---

## 9. 推送代码到远端（GitHub / Gitee）

两个远端都在用，名字不同：

| 远端 | 仓库 | 说明 |
|---|---|---|
| `github` | `Alin981010/alin-chat-assistant` | 主仓库 |
| `origin` | `xiang-wanlin1024/alin-chat-assistant` | Gitee 镜像 |

两个仓库名已于 2026-09 一并改掉（旧地址 `alin-agent` / `AlinAgent` 仍会 302 跳转，
所以老链接不会失效）。改名后记得同步本地 remote：

```powershell
git remote set-url origin https://gitee.com/xiang-wanlin1024/alin-chat-assistant.git
git remote set-url github https://github.com/Alin981010/alin-chat-assistant.git
git remote -v      # 核对
```

> **Gitee 改名只能走网页**：Gitee v5 API 不接受账号密码（回
> `401 Access token does not exist`），必须用私人令牌。网页 10 秒能改完，不值得为它存令牌。

**GitHub 的 git 通道会被网络阻断**（`github.com:443` 连不上，而
`api.github.com:443` 正常）。表现是 `git push github main` 长时间挂起后报
`Could not connect to server` 或 `Recv failure: Connection was reset`——**这不是配置问题，
别去改 remote、凭据或代理设置**。

处理顺序：

1. 先 `git push origin main`（Gitee 稳定可达），代码不会丢；
2. 隔几分钟重试 `git push github main`——实测是间歇性的，恢复了就能正常推；
3. 仍不通就走 **GitHub Git Data API**（`api.github.com` 可达）：用
   `POST /git/blobs` → `POST /git/trees` → `POST /git/commit` → `PATCH /git/refs/heads/main`
   把落后的文件补上去。

### 走 API 时有个 SHA 分叉的坑
API 造出来的 commit 与本地 commit **内容相同但 SHA 不同**，于是本地历史与远端分叉，
之后每次 `git push github` 都会被 `non-fast-forward` 拒掉，而且**不能**把远端引用指回
本地 SHA（那个 git 对象从来没推上去，GitHub 会回 `422 Object does not exist`）。

正确的收尾是**让本地对齐远端**：

```powershell
git diff --stat <本地HEAD> github/main   # 必须先确认为空（内容一致）
git fetch github main
git reset --hard github/main             # 只换 SHA，内容不变
git push --force-with-lease origin main  # Gitee 一并对齐
```

`--force-with-lease` 而不是 `--force`：万一远端有别人的提交会拒绝，不会误删。

---

## 故障对照表

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| 容器无限 `Restarting` | PG 18 卷路径挂错 | 挂 `/var/lib/postgresql`，不是 `/var/lib/postgresql/data` |
| 改了 key 仍 401 | 只 `start`/`restart`，没重建容器 | `docker compose up -d` |
| 401 且 key 看着完全正常 | key 尾部带了 `\r`（经 stdin 传值时） | 远端写入前 `.strip()` |
| 机器内正常、公网不通 | 安全组未放行 | 云控制台加入站规则（TCP 8088） |
| 构建卡在拉基础镜像 | 镜像源顺序不对 | 内网源排 `registry-mirrors` 第一位 |
| 请求返回 403 | `org_id` 与签名不符 | 走浏览器流程取合法 cookie |
| 请求返回 429 | 触发额度 / 限流 | 见 `AGENTS.md` 坑 9 |
| `docker start` 报 no such container | 之前用了 `down`（删了容器） | 用 `docker compose up -d` 重建 |
| 重启服务器后服务没起来 | `unless-stopped` 尊重手动 stop | 手动 start，或改 `restart: always` |
