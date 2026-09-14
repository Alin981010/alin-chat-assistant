import os
from pathlib import Path
from .limits import (
    AGENT_MAX_OUTPUT_TOKENS as _DEFAULT_MAX_OUTPUT,
    AGENT_RECURSION_LIMIT as _DEFAULT_RECURSION,
    CHAT_DAILY_TOKEN_BUDGET as _DEFAULT_DAILY_BUDGET,
    CHAT_MAX_INPUT_TOKENS as _DEFAULT_MAX_INPUT,
    CHAT_RPM_PER_IP as _DEFAULT_RPM_IP,
    CHAT_RPM_PER_USER as _DEFAULT_RPM_USER,
    EXECUTE_RPM_PER_ORG as _DEFAULT_EXECUTE_RPM,
    EXECUTE_TIMEOUT_MAX as _DEFAULT_EXECUTE_TIMEOUT,
)

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


def _env_int_allow_zero(name: str, default: int) -> int:
    """同 ``_env_int``，但显式允许 0。

    额度类配置里 0 是「不限」，而 ``_env_int`` 会把 0 当成非法值回落到默认值
    ——那会让「关掉每日额度」这个操作静默失效（用户以为关了，实际还在限）。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(float(raw.strip()))
    except ValueError:
        return default
    return value if value >= 0 else default


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

#: 助手自称的名字。系统提示词与前端文案都用它，改一处即可换品牌；
#: 部署时用 ASSISTANT_NAME 环境变量覆盖，不必改代码。
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "阿林对话助手").strip() or "阿林对话助手"

#: 是否把沙箱当「代码执行器」用。
#:
#: 默认 **关闭**：本项目的定位是通用对话助手，写代码/调试/解释代码一律通过
#: 对话完成——用户把代码贴进来，助手读它、讲它、改它，不去执行它。这样既没有
#: 容器成本，也没有「用户跑任意脚本」的滥用面。
#:
#: 置 true 才恢复原来的能力：文件落到容器 /workspace、execute 真跑命令、
#: 大文件分析走脚本。关掉时 agent 的 execute 工具会被摘掉，
#: 「我执行了」这种回复在物理上不可能发生。
ENABLE_CODE_EXECUTION = _env_flag("ENABLE_CODE_EXECUTION", False)

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

# ---------------------------------------------------------------------------
# 额度与限流
#
# 对外开放的部署里，token 是被「反复对话」烧掉的，所以默认值是**偏保守**的：
# 正常单用户不会一分钟问 8 次、一天烧 200 万 token。调大之前请先想清楚
# 「最坏情况下一天最多花多少钱」。
#
# 0 在这些项里统一表示「不限」。
# ---------------------------------------------------------------------------
from .limits import (
    AGENT_MAX_OUTPUT_TOKENS as _DEFAULT_MAX_OUTPUT,
    AGENT_RECURSION_LIMIT as _DEFAULT_RECURSION,
    CHAT_DAILY_TOKEN_BUDGET as _DEFAULT_DAILY_BUDGET,
    CHAT_MAX_INPUT_TOKENS as _DEFAULT_MAX_INPUT,
    CHAT_RPM_PER_IP as _DEFAULT_RPM_IP,
    CHAT_RPM_PER_USER as _DEFAULT_RPM_USER,
    EXECUTE_RPM_PER_ORG as _DEFAULT_EXECUTE_RPM,
    EXECUTE_TIMEOUT_MAX as _DEFAULT_EXECUTE_TIMEOUT,
)

from .limits import (
    AGENT_MAX_OUTPUT_TOKENS as _DEFAULT_MAX_OUTPUT,
    AGENT_RECURSION_LIMIT as _DEFAULT_RECURSION,
    CHAT_DAILY_TOKEN_BUDGET as _DEFAULT_DAILY_BUDGET,
    CHAT_MAX_INPUT_TOKENS as _DEFAULT_MAX_INPUT,
    CHAT_RPM_PER_IP as _DEFAULT_RPM_IP,
    CHAT_RPM_PER_USER as _DEFAULT_RPM_USER,
    EXECUTE_RPM_PER_ORG as _DEFAULT_EXECUTE_RPM,
    EXECUTE_TIMEOUT_MAX as _DEFAULT_EXECUTE_TIMEOUT,
)

CHAT_RPM_PER_USER = _env_int_allow_zero("CHAT_RPM_PER_USER", _DEFAULT_RPM_USER)
CHAT_RPM_PER_IP = _env_int_allow_zero("CHAT_RPM_PER_IP", _DEFAULT_RPM_IP)
CHAT_DAILY_TOKEN_BUDGET = _env_int_allow_zero("CHAT_DAILY_TOKEN_BUDGET", _DEFAULT_DAILY_BUDGET)
CHAT_MAX_INPUT_TOKENS = _env_int_allow_zero("CHAT_MAX_INPUT_TOKENS", _DEFAULT_MAX_INPUT)

#: 模型单次回复的输出上限（max_tokens）。上限越高，一次被刷走的钱越多。
AGENT_MAX_OUTPUT_TOKENS = _env_int("AGENT_MAX_OUTPUT_TOKENS", _DEFAULT_MAX_OUTPUT)

#: agent 单次提问的图步数上限。DeepAgents 默认 9999，等于不设限——
#: 工具一旦打转，一次提问就能烧掉几百次模型调用。
AGENT_RECURSION_LIMIT = _env_int("AGENT_RECURSION_LIMIT", _DEFAULT_RECURSION)

#: 沙箱 execute 的每 org 每分钟次数与单次时长上限。
EXECUTE_RPM_PER_ORG = _env_int("EXECUTE_RPM_PER_ORG", _DEFAULT_EXECUTE_RPM)
EXECUTE_TIMEOUT_MAX = _env_int("EXECUTE_TIMEOUT_MAX", _DEFAULT_EXECUTE_TIMEOUT)
