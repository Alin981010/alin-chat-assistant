from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from .agent_setup import create_agent, _ensure_agents_memory, _remove_per_user_agents_memory
from .config import DB_URI, SANDBOX_IMAGE
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

        init_sandbox_manager(SANDBOX_IMAGE)

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


app = FastAPI(title="阿林助手 · AlinAgent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
    return {"status": "ok", "message": "AlinAgent API is running"}


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
