from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from .agent_setup import create_agent, _ensure_agents_memory, _remove_per_user_agents_memory
from .config import DB_URI, ENABLE_CODE_EXECUTION, SANDBOX_IMAGE
from .identity import _allowed_origins, identity_middleware
from .routes import router, set_globals
from .sandbox import init_sandbox_manager

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
INDEX_HTML = STATIC_DIR / "index.html"

_store: Any = None
_checkpointer: Any = None
_agent: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store, _checkpointer, _agent
    logger.info("Initializing database connections...")

    from contextlib import ExitStack
    from langgraph.store.postgres import PostgresStore
    from langgraph.checkpoint.postgres import PostgresSaver

    stack = ExitStack()
    try:
        _store = stack.enter_context(PostgresStore.from_conn_string(DB_URI))
        _checkpointer = stack.enter_context(PostgresSaver.from_conn_string(DB_URI))

        _store.setup()
        _checkpointer.setup()

        # 沙箱只在开启代码执行时才探测/预热。默认的通用对话模式不需要容器，
        # 跳过它同时省掉三件事：docker pull 十几 GB、启动探测超时、
        # 以及「沙箱不可用」这一类排查噪音。
        if ENABLE_CODE_EXECUTION:
            init_sandbox_manager(SANDBOX_IMAGE)
        else:
            logger.info("代码执行已关闭：不初始化沙箱（对话模式无需容器）。")

        _agent = create_agent(_checkpointer, _store)
        set_globals(_store, _checkpointer, _agent)

        # AGENTS.md 是全局配置：启动时确保唯一一份存在，并清理历史遗留的按用户副本
        try:
            _ensure_agents_memory(_store)
            _remove_per_user_agents_memory(_store)
        except Exception as e:
            logger.warning(f"Failed to seed AGENTS.md at startup: {e}", exc_info=True)

        logger.info("Application started successfully.")
        yield
    except Exception as e:
        logger.error(f"Failed to initialize: {e}", exc_info=True)
        stack.close()
        from .sandbox import get_sandbox_manager
        mgr = get_sandbox_manager()
        if mgr:
            try:
                mgr.stop()
            except Exception:
                pass
        raise

    logger.info("Shutting down...")
    stack.close()
    from .sandbox import get_sandbox_manager
    mgr = get_sandbox_manager()
    if mgr:
        try:
            mgr.stop()
        except Exception as e:
            logger.error(f"Error stopping sandbox manager: {e}")


app = FastAPI(title="阿林对话助手 · Alin Chat Assistant", lifespan=lifespan)

# 身份中间件必须在 CORS 之前注册：Starlette 的中间件是后注册的先执行（洋葱模型），
# 而 CORS 需要在最外层，才能把预检请求和错误响应也盖上 Access-Control-* 头。
app.middleware("http")(identity_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    # 匿名身份走 cookie 传递，但这里刻意不开 allow_credentials：
    # 浏览器规范禁止 allow_origins=["*"] 与 credentials 同时生效，
    # 且本应用没有需要跨站携带的登录态。跨站调用请用 X-Alin-Agent-Id 头。
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

# 前端静态资源。index.html 引用的是 /static/css/app.css 与 /static/js/app.js，
# 整个 static/ 挂在 /static 下一个前缀，以后新增 img/、fonts/ 之类的目录不必再动后端。
# 路径与 /api、/ 均不重叠，挂载顺序无影响。
# 目录缺失时只告警不挂载：只部署 API 的场景仍能正常启动，/ 会退回 JSON 健康检查。
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    logger.warning(f"Static directory not found: {STATIC_DIR} — 前端资源不会被托管。")


@app.get("/")
async def root():
    if INDEX_HTML.exists():
        return FileResponse(str(INDEX_HTML))
    return {"status": "ok", "message": "Alin Chat Assistant API is running"}


@app.get("/health")
async def health():
    """给容器探针/负载均衡用的轻量健康检查，不碰数据库、不建沙箱。"""
    from .sandbox import get_sandbox_manager

    manager = get_sandbox_manager()
    return {
        "status": "ok",
        "agent_ready": _agent is not None,
        "sandbox": manager.availability_code if manager else "not_initialized",
    }
