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
