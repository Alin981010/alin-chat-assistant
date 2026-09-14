import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def _env_flag(name: str, default: bool = False) -> bool:
    """读取布尔开关：1/true/yes/y/on（大小写不敏感、允许首尾空格）为真，其余为假。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    """读取整数开关，非法值回落到默认值而不是抛异常。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(float(raw.strip()))
    except ValueError:
        return default
    return value if value > 0 else default


DB_URI = os.getenv("DB_URI", "postgresql://postgres:root@127.0.0.1:5432/testdb")

SANDBOX_IMAGE = os.getenv(
    "SANDBOX_IMAGE",
    "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.0.1",
)
SANDBOX_IDLE_TIMEOUT = 10 * 60
SANDBOX_CLEANUP_INTERVAL = 60

# 创建沙箱时等待其就绪的上限（秒）。SDK 默认只有 30 秒，而首次创建要拉镜像
# （code-interpreter 镜像十几个 GB，实测远超 30s），于是启动探测必然超时、
# 静默降级成"无沙箱"。预热过镜像后 180 秒绰绰有余。
SANDBOX_READY_TIMEOUT = _env_int("SANDBOX_READY_TIMEOUT", 180)

# OpenSandbox 客户端是否经 server 代理转发 execd 请求。
# False（默认，同 SDK 默认）= client 直连沙箱容器 IP，适合 server 与 client 同机/同网络；
# server 跑在 Docker 而 client 在宿主机、直连不到容器 IP 时置 True。
# 注意：SDK 只从环境变量读 OPEN_SANDBOX_DOMAIN / OPEN_SANDBOX_API_KEY，
# use_server_proxy 没有环境变量入口，必须由 app/sandbox.py 显式传进 ConnectionConfigSync。
SANDBOX_USE_SERVER_PROXY = _env_flag("SANDBOX_USE_SERVER_PROXY", False)

# ---------------------------------------------------------------------------
# 密钥命名的坑（务必留意，写错了只会得到一句没头没尾的 401）
#
#   本项目 / 客户端 SDK ：OPEN_SANDBOX_API_KEY        <- 下划线在 OPEN 之后
#   服务端进程          ：OPENSANDBOX_SERVER_API_KEY  <- 下划线在 OPENSANDBOX 之后
#   服务端配置文件      ：~/.sandbox.toml 的 [server].api_key
#
# 三者是同一个共享密钥，但前缀顺序相反。服务端读的是后两者（TOML 优先，环境变量可覆盖），
# 客户端读的是前者。这里显式取出客户端侧的值，而不是依赖 SDK 内部的 os.getenv 兜底，
# 便于启动时打印状态、以及在 401 时把原因说清楚（见 app/sandbox.py 的 _diagnose_sandbox_failure）。
# ---------------------------------------------------------------------------
SANDBOX_DOMAIN = os.getenv("OPEN_SANDBOX_DOMAIN", "localhost:8080").strip()
SANDBOX_API_KEY = os.getenv("OPEN_SANDBOX_API_KEY", "").strip()

PROJECT_ROOT = Path(__file__).parent.parent
LOCAL_SKILLS_DIR = PROJECT_ROOT / "skills"
PPT_SKILL_DIR = LOCAL_SKILLS_DIR / "simple-pptx-generator"
PPT_CORE_DEPS = ["python-pptx"]

MODEL_NAME = "deepseek:deepseek-flash"
