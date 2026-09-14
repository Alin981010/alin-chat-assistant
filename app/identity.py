"""匿名身份：服务端签发的 HMAC 令牌。

## 为什么需要这个模块

这个应用没有任何登录，但 **org_id 是授权键**：

- `SandboxManager.get_backend(org_id)` 按 org 缓存容器（app/sandbox.py），
  一个 org 一个沙箱，sandbox 里的文件靠它隔离；
- `/api/sandbox/download?org_id&path` 按 org 决定去哪个容器取文件。

在引入本模块之前，`org_id` 由前端 localStorage 生成、且被写死为
``default-org``（static/js/app.js）。两个后果：

1. **所有用户共用一个容器**，彼此的文件在 sandbox 里直接可见；
2. 授权键可被请求方任意填写，等于没有隔离——改一个字符串就能读别人的沙箱。

这里不引入登录系统，只做一件事：把匿名身份改成**服务端签发、带 HMAC 签名**。
签名保证 ``org_id`` / ``user_id`` 不能被客户端伪造，代价是零用户表、零登录页。

## 令牌格式

``<base64url(payload_json)>.<base64url(hmac_sha256(secret, payload_b64))>``

payload 是 ``{"o": org_id, "u": user_id, "v": 1}``。校验时用
``hmac.compare_digest`` 做常数时间比较，避免时序侧信道。

## 密钥来源

``APP_SECRET_KEY`` 环境变量优先；未配置时由 ``DB_URI`` 派生（仅为了让单机部署
开箱可用），并在启动时打一条 warning。多实例部署必须显式配置，否则各实例
派生出相同密钥倒是能互通，但**一旦改库口令，所有用户身份会失效**。
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_VERSION = 1

#: cookie 名与下面的密钥派生前缀**故意保留旧字串**（alinagent）。
#: 它们不是品牌展示，而是持久化标识：改了 cookie 名会让在线用户当场换身份，
#: 改了派生前缀会让所有人「今天的额度」清零（用量记录挂在 org 上）。
#: 产品更名时不动它们，是刻意的取舍。
COOKIE_NAME = "alinagent_id"
COOKIE_MAX_AGE = 400 * 24 * 3600  # 约 13 个月，与浏览器 cookie 上限对齐

#: 合法 org_id / user_id 的字符集与长度。收紧到 ASCII 字母数字与 - _ ，
#: 是因为这两个值会进入容器缓存键、文件路径拼接与 thread_id，放宽不划算。
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

#: 历史遗留值：旧前端硬编码的 default-org。它不可能是本模块签发的，
#: 遇到就当作「未认证」重新签发，从而让所有用户自然迁出共享容器。
LEGACY_ORG_IDS = frozenset({"default-org", "default", ""})


def _load_secret() -> bytes:
    """解析签名密钥：环境变量优先，否则由 DB_URI 派生。"""
    raw = (os.getenv("APP_SECRET_KEY") or "").strip()
    if raw:
        return raw.encode("utf-8")

    try:
        from .config import DB_URI
    except Exception:  # pragma: no cover - 仅在导入期极端异常时触发
        DB_URI = "alinagent-fallback"

    logger.warning(
        "APP_SECRET_KEY 未配置，签名密钥由 DB_URI 派生。单机够用，"
        "但多实例部署或轮换数据库口令时必须显式配置 APP_SECRET_KEY，否则用户身份会失效。"
    )
    # 前缀里的 "alinagent" 是历史字串，**不要跟着品牌一起改**：改了等于换密钥，
    # 所有未配置 APP_SECRET_KEY 的部署会让在线用户当场换身份、当日额度清零。
    return hashlib.sha256(f"alinagent-identity-v1::{DB_URI}".encode("utf-8")).digest()


_SECRET: Optional[bytes] = None


def _secret() -> bytes:
    """懒加载密钥，便于测试在设置环境变量后再触发。"""
    global _SECRET
    if _SECRET is None:
        _SECRET = _load_secret()
    return _SECRET


def reset_secret_cache() -> None:
    """清掉密钥缓存（供测试在改环境变量后调用）。"""
    global _SECRET
    _SECRET = None


@dataclass(frozen=True)
class Identity:
    """一个已通过签名校验的匿名身份。

    ``user_id`` 有两个来源：

    - **游客**：服务端随机生成的 ``u…``，随 cookie 走，换浏览器就换身份；
    - **已登录**：真实账号的 ``user_id``（见 ``app/auth.py`` 的 ``User.user_id``）。

    ``tier`` 决定额度档位（见 ``app/budget.py``）：``guest`` 或 ``member``。
    它由服务端在鉴权时判定，**不接受客户端声明**——否则谁都能自称会员。
    """

    org_id: str
    user_id: str
    tier: str = "guest"

    @property
    def is_member(self) -> bool:
        return self.tier == "member"

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"Identity(org={self.org_id[:8]}…, user={self.user_id[:8]}…, tier={self.tier})"


def is_valid_id(value: Optional[str]) -> bool:
    """判断 org_id / user_id 是否合法。"""
    return bool(value) and bool(ID_PATTERN.match(value or ""))


def new_id(prefix: str) -> str:
    """生成一个新的随机 id（org 或 user 通用）。"""
    return prefix + secrets.token_hex(12)


def new_identity() -> Identity:
    """签发一个全新**游客**身份。"""
    return Identity(org_id=new_id("o"), user_id=new_id("u"), tier="guest")


def member_identity(user_id: str, org_id: Optional[str] = None) -> Identity:
    """构造一个**已登录**身份。

    ``org_id`` 由调用方传入已有的匿名 org（这样同一浏览器登录前后共用一台沙箱，
    老会话也不会因为登录而全变成"别人的"）；不传则按 user_id 派生一个稳定值，
    保证同一账号在不同设备上拿到同一个沙箱命名空间。
    """
    return Identity(org_id=org_id or ("o" + user_id[1:]), user_id=user_id, tier="member")



def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_token(identity: Identity) -> str:
    """为一个身份签发令牌。``t`` 是额度档位（guest/member）。

    注意：**登录状态不写在这里**。这个 cookie 只表示"这台浏览器是谁"，
    登录与否由 ``app/auth.py`` 的会话 cookie 决定——两件事分开，登出才能只清一个。
    """
    payload = {
        "o": identity.org_id,
        "u": identity.user_id,
        "t": identity.tier,
        "v": TOKEN_VERSION,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: Optional[str]) -> Optional[Identity]:
    """校验令牌；任何一项不通过都返回 ``None``（不抛异常）。

    拒绝的情形：格式不对、签名不匹配、payload 不是 JSON、版本不符、
    org/user 不满足 ID_PATTERN。调用方拿到 ``None`` 一律当「新访客」处理。
    """
    if not token or "." not in token:
        return None

    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None

    try:
        expected = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    except Exception:
        return None

    if not hmac.compare_digest(sig, expected):
        return None

    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
        return None

    org_id = payload.get("o")
    user_id = payload.get("u")
    if not is_valid_id(org_id) or not is_valid_id(user_id):
        return None

    # 档位只接受这两个值；cookie 里带了别的一律按游客处理（不报错、不提升权限）。
    tier = payload.get("t")
    if tier not in ("guest", "member"):
        tier = "guest"

    return Identity(org_id=org_id, user_id=user_id, tier=tier)


def is_legacy_org(org_id: Optional[str]) -> bool:
    """判断一个 org_id 是否是历史遗留的共享值。"""
    return org_id is None or org_id in LEGACY_ORG_IDS


# ---------------------------------------------------------------------------
# 请求级身份
#
# 中间件把校验过的 Identity 放进 contextvar，路由处理函数通过
# ``get_current_identity()`` 取。用 contextvar 而不是把身份穿进每个函数签名，
# 是为了不影响那些已经深层调用沙箱的代码路径（与 app/sandbox 里
# _org_id_var 的做法一致）。
#
# 注意：``/api/chat/stream`` 返回的是 StreamingResponse，生成器在**同一个
# 任务上下文**里迭代（见 app/routes.py 里关于 anyio 拷贝 contextvars 的注释），
# 所以这里不需要额外传递。
# ---------------------------------------------------------------------------

_identity_var: contextvars.ContextVar[Optional[Identity]] = contextvars.ContextVar(
    "request_identity", default=None
)

#: 当前请求的登录用户（``app/auth.py`` 的 ``User``）。
#: 单独一个 contextvar 而不是挂在 Identity 上：Identity 是**签名 cookie 的内容**，
#: 而登录状态来自另一个 cookie，两者生命周期不同（登出只清后者）。
_user_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "request_user", default=None
)


def set_current_user(user: Optional[Any]):
    """设置当前请求的登录用户（中间件调用）。"""
    return _user_var.set(user)


def reset_current_user(token) -> None:
    _user_var.reset(token)


def get_current_user() -> Optional[Any]:
    """取当前登录用户；游客为 ``None``。"""
    return _user_var.get()


def set_current_identity(identity: Optional[Identity]):
    """设置当前请求身份。"""
    return _identity_var.set(identity)


def reset_current_identity(token) -> None:
    """恢复 contextvar 到设置前的值。"""
    _identity_var.reset(token)


def get_current_identity() -> Optional[Identity]:
    """取当前已校验的身份；未经过中间件时为 ``None``。"""
    return _identity_var.get()


def _cookie_kwargs() -> dict:
    """构造身份 cookie 的属性。

    ``httponly=False``：前端要读 org_id 来拼 thread_id（``org__user__后缀``）
    和沙箱下载链接，所以这个 cookie 必须对 JS 可见。可读不等于可伪造——
    改坏了签名校验会失败，请求方只会被换成一个全新身份，拿不到别人的容器。

    登录会话 cookie（``app/auth.py`` 的 ``SESSION_COOKIE_NAME``）则相反，
    是 ``httponly=True``：前端不需要读它，也就没必要让脚本能碰到。
    """
    return {
        "key": COOKIE_NAME,
        "httponly": False,
        "samesite": "lax",
        "secure": cookie_secure(),
        "max_age": COOKIE_MAX_AGE,
        "path": "/",
    }


def cookie_secure() -> bool:
    """cookie 是否只在 HTTPS 下发送。

    注意：当前部署若是 ``http://<IP>:8088`` 明文访问，这里**必须**保持 false，
    置 true 会让 cookie 根本发不出去（表现是"登录了但下一个请求又不认识我"）。
    """
    return (os.getenv("APP_COOKIE_SECURE") or "").strip().lower() in {"1", "true", "yes", "on"}


async def identity_middleware(request: Request, call_next):
    """给每个请求挂上「你是谁 + 你是游客还是会员」。

    三层优先级：

    1. **登录会话 cookie**（``app/auth.py``）有效 → ``member`` 身份，
       ``org_id`` 沿用浏览器原有的匿名 org（登录不该把沙箱和已建会话挪走）；
    2. 否则用身份 cookie / ``X-Alin-Agent-Id`` 里的匿名身份 → ``guest``；
    3. 都没有或签名不通过 → 视为新访客，现场签发并在响应里 Set-Cookie。

    **档位由这里判定，不接受客户端声明**——请求里无论写什么都不会把自己变成会员。
    同样地，本中间件不拒绝任何请求：它只回答"你是谁"，
    "这个身份能做什么"由各路由与 ``app/budget.py`` 决定。
    """
    from .auth import SESSION_COOKIE_NAME, get_user_store, verify_session

    cookie_identity = verify_token(request.cookies.get(COOKIE_NAME))
    if cookie_identity is None:
        cookie_identity = verify_token(request.headers.get("X-Alin-Agent-Id"))

    # 登录状态：有效会话 cookie → 会员档，并沿用原来的 org
    user = None
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        try:
            user = verify_session(session_token, get_user_store())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"会话校验异常（按未登录处理）：{e}")

    issued = cookie_identity is None
    if cookie_identity is None:
        cookie_identity = new_identity()

    if user is not None:
        identity = member_identity(user.user_id, org_id=cookie_identity.org_id)
    else:
        identity = cookie_identity

    token = set_current_identity(identity)
    user_token = set_current_user(user)
    request.state.identity = identity
    request.state.user = user
    try:
        response = await call_next(request)
    finally:
        reset_current_user(user_token)
        reset_current_identity(token)

    # 身份 cookie 变了就要重发：新访客要签发，登录/登出会改档位也要刷新，
    # 否则 cookie 里还是旧档位，与内存里的身份不一致。
    if issued or (cookie_identity != identity and user is not None):
        response.set_cookie(value=issue_token(identity), **_cookie_kwargs())
    return response


def _allowed_origins() -> list[str]:
    """解析 CORS 白名单。

    默认 ``*``：面向内网/单机的既有行为不变。但 ``allow_origins=["*"]`` 与
    ``allow_credentials=True`` 在浏览器里本就是无效组合，所以凭据一律关闭。
    生产部署应把 ``APP_ALLOWED_ORIGINS`` 设成真实站点域名（逗号分隔）。
    """
    raw = (os.getenv("APP_ALLOWED_ORIGINS") or "").strip()
    if not raw:
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]
