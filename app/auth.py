"""用户账号与登录会话。

## 为什么是「用户名 + 密码」而不是邮箱

邮箱注册要配套验证邮件与找回密码，没有 SMTP 的情况下那两步都是摆设——用户填了
邮箱却发现收不到信，比不提供邮箱更糟。用户名 + 密码零外部依赖，能立刻用。
以后要加邮箱，只是在 ``User`` 上多一个可选字段的事。

## 密码怎么存

PBKDF2-HMAC-SHA256 + 每用户随机盐，迭代次数见 ``config.PBKDF2_ITERATIONS``
（默认 60 万，OWASP 对 PBKDF2-SHA256 的现行建议量级）。**刻意用标准库 ``hashlib``
实现**，不引 bcrypt/argon2：少一个需要编译的依赖，部署更省事；PBKDF2 在 60 万
迭代下对"偷到库再离线爆破"这个威胁模型是够的。

存储格式（单字段自描述，换算法时老记录仍可校验）：

    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

校验用 ``hmac.compare_digest`` 做常数时间比较；用户不存在时也走一次假校验，
避免"响应快慢"泄露某个用户名是否存在。

## 会话

签名令牌（与 ``app/identity.py`` 的匿名身份同一套 HMAC 思路），载荷是
``{"u": user_id, "e": session_epoch, "x": 过期时间戳}``。**无状态**：不存会话表，
登出就是清 cookie。``session_epoch`` 给的是"一键踢下线"的余地——把它加一，
该用户所有已签发的令牌立刻失效。

## 存储

沿用项目现有的 ``PostgresStore``（长期记忆用的就是它），额外在内存里放一份索引，
避免每次请求都读库。应用是刻意单 worker 的，所以内存索引与库不会分叉；
写入路径上加了锁，并发注册同名用户会被第二个人撞成 409。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: PostgresStore 里的命名空间与 key（每个用户一条记录）
AUTH_NAMESPACE = ("users",)
AUTH_INDEX_KEY = "/index.json"

#: 登录会话的 cookie 名。与匿名身份 cookie（``alinagent_id``）分开：
#: 登出只清这一个，浏览器身份与已建会话都留着。
SESSION_COOKIE_NAME = "alinchat_session"

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_ALGO = "pbkdf2_sha256"


class AuthError(Exception):
    """账号相关的可预期错误。``code`` 给接口层映射成 HTTP 状态码。"""

    def __init__(self, message: str, code: str = "auth_error"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    password_hash: str
    created_at: float
    session_epoch: int = 1
    disabled: bool = False

    def public(self) -> Dict[str, Any]:
        """对外可见的字段——**绝不包含 password_hash**。"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------

def hash_password(password: str, *, iterations: Optional[int] = None) -> str:
    """把明文密码变成可存储的字符串。"""
    if iterations is None:
        from .config import PBKDF2_ITERATIONS

        iterations = PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。格式不认识 / 记录损坏时返回 ``False``，不抛异常。"""
    if not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != _ALGO:
        return False
    try:
        iterations = int(parts[1])
        salt = _unb64(parts[2])
        expected = _unb64(parts[3])
    except (ValueError, TypeError):
        return False
    if iterations <= 0 or not salt or not expected:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# 用户名 / 密码规则
# ---------------------------------------------------------------------------

def normalize_username(username: str) -> str:
    """归一化用户名：去空白 + 转小写。**唯一性按归一化后的值判定**。

    这样 ``Alin`` 与 ``alin`` 不会变成两个账号——否则用户很容易注册出自己都分不清的
    两个身份，也会给"冒充"留口子。
    """
    return (username or "").strip().lower()


def validate_credentials(username: str, password: str) -> str:
    """校验用户名与密码，返回归一化后的用户名；不合法抛 ``AuthError``。"""
    from .config import PASSWORD_MIN_LEN, USERNAME_MAX_LEN, USERNAME_MIN_LEN

    normalized = normalize_username(username)
    if not normalized:
        raise AuthError("请填写用户名。", "invalid_username")
    if not (USERNAME_MIN_LEN <= len(normalized) <= USERNAME_MAX_LEN):
        raise AuthError(f"用户名长度需在 {USERNAME_MIN_LEN}–{USERNAME_MAX_LEN} 个字符之间。", "invalid_username")
    if not USERNAME_PATTERN.match(normalized):
        raise AuthError("用户名只能包含字母、数字、下划线和连字符。", "invalid_username")
    if password is None or len(password) < PASSWORD_MIN_LEN:
        raise AuthError(f"密码至少 {PASSWORD_MIN_LEN} 位。", "invalid_password")
    if len(password) > 256:
        raise AuthError("密码过长（最多 256 位）。", "invalid_password")
    return normalized


# ---------------------------------------------------------------------------
# 用户仓库
# ---------------------------------------------------------------------------

class UserStore:
    """用户的读写。内存索引 + PostgresStore 持久化。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: Dict[str, User] = {}
        self._by_name: Dict[str, str] = {}   # 归一化用户名 -> user_id
        self._store: Optional[Any] = None

    # -- 接入持久化 --------------------------------------------------------

    def attach(self, store: Any) -> int:
        """挂上 PostgresStore 并把已有用户读进内存；返回载入条数。"""
        self._store = store
        users = self._load_all(store)
        with self._lock:
            self._by_id.clear()
            self._by_name.clear()
            for user in users:
                self._by_id[user.user_id] = user
                self._by_name[user.username] = user.user_id
        if users:
            logger.info(f"载入 {len(users)} 个账号。")
        return len(users)

    def _load_all(self, store: Any) -> List[User]:
        """读索引再逐条取用户。

        索引的存在是必要的：PostgresStore 没有"按命名空间列出全部 key"的便宜办法，
        所以另存一份 ``/index.json`` 记住有哪些用户。索引缺失时退化为空表
        （用户记录本身还在库里，只是需要手工重建索引）。
        """
        try:
            index_record = store.get(AUTH_NAMESPACE, AUTH_INDEX_KEY)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取账号索引失败（按无账号启动）：{e}")
            return []

        value = getattr(index_record, "value", None) if index_record else None
        ids = (value or {}).get("user_ids") if isinstance(value, dict) else None
        if not isinstance(ids, list):
            return []

        users: List[User] = []
        for user_id in ids:
            if not isinstance(user_id, str):
                continue
            try:
                record = store.get(AUTH_NAMESPACE, f"/u_{user_id}.json")
            except Exception:  # noqa: BLE001
                continue
            raw = getattr(record, "value", None) if record else None
            user = _user_from_dict(raw)
            if user is not None:
                users.append(user)
        return users

    def _persist(self, user: User) -> None:
        if self._store is None:
            return
        try:
            self._store.put(AUTH_NAMESPACE, f"/u_{user.user_id}.json", _user_to_dict(user))
            self._store.put(
                AUTH_NAMESPACE,
                AUTH_INDEX_KEY,
                {"user_ids": sorted(self._by_id.keys())},
            )
        except Exception as e:  # noqa: BLE001
            # 落库失败不能把注册/登录搞崩——内存里那份仍然可用，只是重启会丢。
            logger.error(f"账号落库失败（内存中仍可用，重启会丢）：{e}")

    # -- 对外 -------------------------------------------------------------

    def create(self, username: str, password: str) -> User:
        """注册。用户名已被占用时抛 ``AuthError('username_taken')``。"""
        normalized = validate_credentials(username, password)
        with self._lock:
            if normalized in self._by_name:
                raise AuthError("这个用户名已经被注册了。", "username_taken")
            user = User(
                user_id="u" + secrets.token_hex(8),
                username=normalized,
                password_hash=hash_password(password),
                created_at=time.time(),
            )
            self._by_id[user.user_id] = user
            self._by_name[normalized] = user.user_id
            self._persist(user)
        logger.info(f"新账号注册：{user.username}（{user.user_id}）")
        return user

    def authenticate(self, username: str, password: str) -> User:
        """登录校验。用户名不存在或密码错误都抛同一个错误，不区分。"""
        normalized = normalize_username(username)
        with self._lock:
            user_id = self._by_name.get(normalized)
            user = self._by_id.get(user_id) if user_id else None

        if user is None:
            # 用户不存在时也做一次等价耗时的假校验：否则响应时间会泄露
            # "这个用户名是否存在"，等于给撞库提供了筛选器。
            verify_password(password or "", _dummy_hash())
            raise AuthError("用户名或密码不正确。", "bad_credentials")

        if not verify_password(password or "", user.password_hash):
            raise AuthError("用户名或密码不正确。", "bad_credentials")

        if user.disabled:
            raise AuthError("该账号已被停用。", "account_disabled")
        return user

    def get(self, user_id: Optional[str]) -> Optional[User]:
        if not user_id:
            return None
        with self._lock:
            return self._by_id.get(user_id)

    def bump_session_epoch(self, user_id: str) -> Optional[User]:
        """让该用户所有已签发的会话立刻失效（改密码/踢下线时用）。"""
        with self._lock:
            user = self._by_id.get(user_id)
            if user is None:
                return None
            updated = User(
                user_id=user.user_id,
                username=user.username,
                password_hash=user.password_hash,
                created_at=user.created_at,
                session_epoch=user.session_epoch + 1,
                disabled=user.disabled,
            )
            self._by_id[user.user_id] = updated
            self._persist(updated)
        return updated

    def count(self) -> int:
        with self._lock:
            return len(self._by_id)


def _user_to_dict(user: User) -> Dict[str, Any]:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "password_hash": user.password_hash,
        "created_at": user.created_at,
        "session_epoch": user.session_epoch,
        "disabled": user.disabled,
    }


def _user_from_dict(raw: Any) -> Optional[User]:
    if not isinstance(raw, dict):
        return None
    user_id = raw.get("user_id")
    username = raw.get("username")
    if not isinstance(user_id, str) or not isinstance(username, str):
        return None
    return User(
        user_id=user_id,
        username=username,
        password_hash=str(raw.get("password_hash") or ""),
        created_at=float(raw.get("created_at") or 0.0),
        session_epoch=int(raw.get("session_epoch") or 1),
        disabled=bool(raw.get("disabled")),
    )


#: 给"用户不存在"路径用的假哈希，避免时序泄露。
#: 惰性生成：模块导入时算一次 60 万次 PBKDF2 会拖慢启动，而这条路径平时用不到。
_DUMMY_HASH_CACHE: Optional[str] = None


def _dummy_hash() -> str:
    global _DUMMY_HASH_CACHE
    if _DUMMY_HASH_CACHE is None:
        _DUMMY_HASH_CACHE = hash_password("dummy-password-for-timing-equalization")
    return _DUMMY_HASH_CACHE


# ---------------------------------------------------------------------------
# 会话令牌
# ---------------------------------------------------------------------------

def issue_session(user: User, *, ttl_seconds: Optional[int] = None) -> str:
    """签发会话令牌。"""
    from .config import SESSION_TTL_SECONDS

    ttl = SESSION_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    payload = {"u": user.user_id, "e": user.session_epoch, "x": int(time.time()) + int(ttl)}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64(hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_session(token: Optional[str], store: "UserStore") -> Optional[User]:
    """校验会话令牌并取回用户。任何一步不过都返回 ``None``。"""
    if not token or "." not in token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None

    expected = _b64(hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None

    expires_at = payload.get("x")
    if not isinstance(expires_at, int) or expires_at < int(time.time()):
        return None

    user = store.get(payload.get("u") if isinstance(payload.get("u"), str) else None)
    if user is None or user.disabled:
        return None
    # epoch 变了 = 该用户所有旧令牌作废
    if int(payload.get("e") or 0) != user.session_epoch:
        return None
    return user


_SESSION_SECRET: Optional[bytes] = None


def _session_secret() -> bytes:
    """会话签名密钥。

    与匿名身份**共用** ``APP_SECRET_KEY``（都由 ``app/identity`` 的密钥派生逻辑
    兜底），这样只需要配一个密钥；但派生前缀不同，两个令牌不能互相冒用。
    """
    global _SESSION_SECRET
    if _SESSION_SECRET is None:
        from .identity import _secret

        _SESSION_SECRET = hashlib.sha256(b"alinchat-session-v1::" + _secret()).digest()
    return _SESSION_SECRET


def reset_secret_cache() -> None:
    """测试用：清掉派生的会话密钥。"""
    global _SESSION_SECRET
    _SESSION_SECRET = None


#: 全局用户仓库。单 worker 前提下进程内一份即可。
_users = UserStore()


def get_user_store() -> UserStore:
    return _users


def session_cookie_kwargs() -> Dict[str, Any]:
    """登录会话 cookie 的属性。

    与身份 cookie 的区别：``httponly=True``（前端不需要读它；脚本读不到就少一条
    XSS 偷会话的路径），其余（secure / samesite / path）保持一致，
    这样 HTTPS 开关只在一个地方生效。
    """
    from .config import SESSION_TTL_SECONDS
    from .identity import cookie_secure

    return {
        "key": SESSION_COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": cookie_secure(),
        "max_age": SESSION_TTL_SECONDS,
        "path": "/",
    }
