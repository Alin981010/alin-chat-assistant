# syntax=docker/dockerfile:1

# =============================================================================
# 阿林对话助手 · Alin Chat Assistant —— FastAPI + DeepAgents 对话服务
#
#   docker build -t alin-chat-assistant .
#   docker run --rm -p 8088:8088 --env-file .env alin-chat-assistant
#
# 运行时必须提供的外部依赖（都不在镜像里）：
#   * Postgres      —— DB_URI，存 LangGraph checkpoint 与长期记忆，启动即建表
#   * DeepSeek Key  —— DEEPSEEK_API_KEY
#   * OpenSandbox   —— 仅 ENABLE_CODE_EXECUTION=true 时需要（默认关闭，不探测）
# 密钥一律走 --env-file / -e，**绝不能**在构建期写进镜像（见 .dockerignore）。
# =============================================================================

# -----------------------------------------------------------------------------
# 阶段 1/2：builder —— 只负责把依赖装进 /opt/venv
#
# 依赖全部来自 uv.lock（已用 `uv lock --check` 校验与 pyproject.toml 一致），
# 所以这一步是字节级可复现的，不会因为上游发新版而改变镜像内容。
# pyproject.toml 里已把默认源指到清华镜像；换环境（如墙外）构建时改那一处即可。
# -----------------------------------------------------------------------------
FROM python:3.14-slim AS builder

# uv 从 PyPI 装，故意不用官方推荐的 COPY --from=ghcr.io/astral-sh/uv。
# 实测目标服务器（腾讯云上海）的情况：Docker Hub 直连超时，只能靠镜像源，
# 拉 python:3.14-slim 花了十几分钟；ghcr.io 的 blob 更是直接卡死。
# 而清华 PyPI 3 秒就通，uv 本身也发布在 PyPI 上 —— 少一个 registry 依赖，构建才稳。
# 换源：--build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG UV_VERSION=0.12.5
RUN pip install --no-cache-dir --index-url "${PIP_INDEX}" "uv==${UV_VERSION}" \
 && uv --version

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.14 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# 先只放清单文件：只要 uv.lock / pyproject.toml 没变，这层就命中缓存，
# 改业务代码不会重装依赖。
COPY pyproject.toml uv.lock ./

# --frozen     ：只按锁文件装，锁文件过期就直接失败，而不是悄悄升级依赖
# --no-dev     ：python-pptx 只给 tests/ 用，真正生成 PPT 的是沙箱里的解释器
# --no-install-project：本项目是 virtual 包（source = { virtual = "." }），没有自身可装
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# -----------------------------------------------------------------------------
# 阶段 2/2：runtime —— 运行时镜像，不含 uv、不含编译器
# -----------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

# PYTHONUNBUFFERED 对流式对话（SSE）是硬需求：不带这个，日志和响应会被块缓冲住
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8088

LABEL org.opencontainers.image.title="Alin Chat Assistant" \
      org.opencontainers.image.description="阿林对话助手 · 问答 / 写作 / 编程 / 分析" \
      org.opencontainers.image.source="https://github.com/Alin981010/alin-chat-assistant"

# 依赖装完就不再需要 root。10001 避开宿主常见的 1000，减少挂载卷时的权限冲突
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app \
            --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# .dockerignore 已排除 .venv / .git / .env / __pycache__ 等；app/ static/ skills/ tools/ 是运行时必需
COPY --chown=10001:10001 . .

# 构建期自检：import app 会拉起 FastAPI、DeepAgents、LangGraph、psycopg 全链路，
# 缺依赖或版本对不上会在这里直接构建失败，而不是等到线上启动才炸。
# lifespan（连库、建沙箱）不在 import 期执行，所以这一步不需要数据库。
RUN python -c "import app; print('import ok')"

USER 10001:10001

EXPOSE 8088

# /health 是 app/__init__.py 里专门给探针的轻量接口，不碰数据库、不建沙箱。
# 用 shell 形式是为了让 ${PORT} 生效；只用标准库，不为了探针往镜像里装 curl。
# 末尾的 2>/dev/null 是必要的：urllib 连不上时会往 stderr 打一整段 traceback，
# 全都会被记进 docker inspect 的健康检查日志里，把真正有用的信息淹掉。
# start-period 给得比较长：打开 ENABLE_CODE_EXECUTION 时 lifespan 会同步探测沙箱，
# SANDBOX_READY_TIMEOUT 默认 180s（首次拉 code-interpreter 镜像时确实用得上）。
HEALTHCHECK --interval=30s --timeout=10s --start-period=240s --retries=3 \
    CMD python -c "import os,sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8088')+'/health', timeout=5).status==200 else 1)" 2>/dev/null

# 用 uvicorn CLI 而不是 main.py：main.py 把地址硬编码成 127.0.0.1，容器里从外部
# 根本连不上。exec 让 uvicorn 接管 PID 1，SIGTERM 才能触发 lifespan 里的优雅关闭
# （关连接池、清理沙箱）。单 worker 是刻意的：agent / store / 沙箱缓存都挂在进程
# 内的全局变量上，多 worker 会各建一套沙箱管理器。
CMD ["sh", "-c", "exec uvicorn app:app --host \"${HOST:-0.0.0.0}\" --port \"${PORT:-8088}\""]
