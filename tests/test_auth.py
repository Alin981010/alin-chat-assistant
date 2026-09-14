"""用户系统与额度分层验证。

    python tests/test_auth.py

四块：

1. 密码——PBKDF2 往返、错密码、坏记录、用户名/密码规则；
2. 会话令牌——签发/校验/过期/改 epoch 失效/换密钥失效；
3. 账号仓库——注册、重名、登录、大小写归一、落库与重载；
4. 额度分层与 HTTP——**游客额度必须远低于会员**，且档位不可被客户端声明。

不依赖 Postgres：持久化用假的 store 验证契约；HTTP 用 TestClient + monkeypatch。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-do-not-use-in-production")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from app import routes  # noqa: E402
from app.auth import (  # noqa: E402
    SESSION_COOKIE_NAME,
    AuthError,
    UserStore,
    hash_password,
    issue_session,
    normalize_username,
    validate_credentials,
    verify_password,
    verify_session,
)
from app.budget import BudgetExceeded, TokenBudget, quota_for  # noqa: E402
from app.identity import (  # noqa: E402
    member_identity,
    new_identity,
    reset_current_identity,
    set_current_identity,
)

RESULTS = []


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail != "" else ""))


# ---------------------------------------------------------------------------
# 1. 密码
# ---------------------------------------------------------------------------

def test_passwords() -> None:
    stored = hash_password("correct horse battery staple", iterations=1000)
    check("哈希格式自描述（算法$迭代$盐$摘要）",
          stored.startswith("pbkdf2_sha256$1000$") and stored.count("$") == 3, stored[:30])
    check("两次哈希不同（每次随机盐）",
          hash_password("same", iterations=1000) != hash_password("same", iterations=1000))
    check("正确密码通过", verify_password("correct horse battery staple", stored) is True)
    check("错误密码不通过", verify_password("wrong", stored) is False)
    check("空密码不通过", verify_password("", stored) is False)

    for bad in ("", "garbage", "pbkdf2_sha256$0$aa$bb", "pbkdf2_sha256$abc$aa$bb",
                "md5$1$aa$bb", "pbkdf2_sha256$1000$$", None, 123):
        check(f"坏记录不抛异常且拒绝：{bad!r}", verify_password("x", bad) is False)


def test_credential_rules() -> None:
    check("用户名归一化（去空白 + 小写）", normalize_username("  ALin_01 ") == "alin_01")
    check("合法凭据通过", validate_credentials("Alin_01", "12345678") == "alin_01")

    cases = [
        ("", "12345678", "invalid_username"),
        ("ab", "12345678", "invalid_username"),
        ("a" * 33, "12345678", "invalid_username"),
        ("has space", "12345678", "invalid_username"),
        ("中文名", "12345678", "invalid_username"),
        ("alin", "short", "invalid_password"),
        ("alin", "", "invalid_password"),
    ]
    for username, password, expected in cases:
        try:
            validate_credentials(username, password)
            check(f"规则应拒绝 {username!r}/{password!r}", False)
        except AuthError as e:
            check(f"规则拒绝 {username!r}/{password!r} → {expected}", e.code == expected, e.code)


# ---------------------------------------------------------------------------
# 2. 会话令牌
# ---------------------------------------------------------------------------

def test_sessions() -> None:
    store = UserStore()
    user = store.create("alin", "12345678")

    token = issue_session(user, ttl_seconds=60)
    check("会话令牌可校验并取回用户", verify_session(token, store) == user)
    check("篡改令牌失效", verify_session(token[:-1] + ("a" if token[-1] != "a" else "b"), store) is None)
    check("乱码 / 空令牌被拒", all(verify_session(v, store) is None for v in (None, "", "abc", "a.b.c")))

    expired = issue_session(user, ttl_seconds=-1)
    check("过期令牌被拒", verify_session(expired, store) is None)

    # epoch +1 = 该用户所有旧令牌立刻失效（"踢下线"）
    store.bump_session_epoch(user.user_id)
    check("改 session_epoch 后旧令牌失效", verify_session(token, store) is None)

    fresh_user = store.get(user.user_id)
    check("重新签发的令牌可用", verify_session(issue_session(fresh_user, ttl_seconds=60), store) == fresh_user)


# ---------------------------------------------------------------------------
# 3. 账号仓库
# ---------------------------------------------------------------------------

class FakeRecord:
    def __init__(self, value):
        self.value = value


class FakeStore:
    """只实现 put/get，够验证「落库 + 重载」这条契约。"""

    def __init__(self):
        self.data = {}

    def put(self, namespace, key, value):
        self.data[(tuple(namespace), key)] = value

    def get(self, namespace, key):
        value = self.data.get((tuple(namespace), key))
        return FakeRecord(value) if value is not None else None


def test_user_store() -> None:
    store = UserStore()
    user = store.create("Alin", "12345678")
    check("注册成功并归一化用户名", user.username == "alin" and user.user_id.startswith("u"), user)
    check("对外字段不含密码哈希", "password_hash" not in user.public(), user.public())

    try:
        store.create("ALIN", "87654321")
        check("重名（大小写不同）应被拒", False)
    except AuthError as e:
        check("重名（大小写不同）被拒", e.code == "username_taken", e.code)

    check("正确密码可登录", store.authenticate("alin", "12345678").user_id == user.user_id)
    check("大小写不敏感登录", store.authenticate("ALIN", "12345678").user_id == user.user_id)

    for bad_user, bad_pass in (("alin", "wrong-pass"), ("nobody", "12345678")):
        try:
            store.authenticate(bad_user, bad_pass)
            check(f"{bad_user} 错误凭据应被拒", False)
        except AuthError as e:
            check(f"{bad_user} 错误凭据被拒（不泄露账号是否存在）", e.code == "bad_credentials", e.code)

    # 落库 + 重载
    backing = FakeStore()
    s1 = UserStore()
    s1.attach(backing)
    s1.create("persisted", "12345678")
    s2 = UserStore()
    loaded = s2.attach(backing)
    check("重启后账号能从存储载入", loaded == 1 and s2.get(s1.authenticate("persisted", "12345678").user_id) is not None)
    check("载入后仍能登录", s2.authenticate("persisted", "12345678").username == "persisted")

    # 落库坏了也不能把注册搞崩
    class BrokenStore:
        def put(self, *a, **k): raise RuntimeError("db down")
        def get(self, *a, **k): raise RuntimeError("db down")

    s3 = UserStore()
    s3.attach(BrokenStore())
    u3 = s3.create("resilient", "12345678")
    check("落库异常时仍能注册（内存可用）", s3.authenticate("resilient", "12345678").user_id == u3.user_id)


# ---------------------------------------------------------------------------
# 4. 额度分层
# ---------------------------------------------------------------------------

def test_quota_tiers() -> None:
    guest = quota_for("guest")
    member = quota_for("member")

    check("游客每日额度远低于会员（至少差 10 倍）",
          member.daily_token_budget >= guest.daily_token_budget * 10,
          f"游客 {guest.daily_token_budget:,} vs 会员 {member.daily_token_budget:,}")
    check("游客每分钟次数更少", guest.rpm_per_user < member.rpm_per_user,
          f"{guest.rpm_per_user} vs {member.rpm_per_user}")
    check("游客单条消息更短", guest.max_input_tokens < member.max_input_tokens,
          f"{guest.max_input_tokens} vs {member.max_input_tokens}")
    check("认不出的档位按游客处理", quota_for("admin").tier == "guest" and quota_for(None).tier == "guest")

    budget = TokenBudget(guest_daily_token_budget_per_ip=1000)
    # 档位真的参与计算：同一实例在游客档与会员档下算出不同额度。
    # 注意不要只看 spec_for 的数值——实例属性会覆盖两档（那是给运维/测试用的
    # 全局开关），所以"两档不同"要看 tier 与默认额度，而不是覆盖后的值。
    guest_spec = budget.spec_for("guest")
    member_spec = budget.spec_for("member")
    check("spec_for 认出了档位", guest_spec.tier == "guest" and member_spec.tier == "member")
    check("两档的默认额度不同（来自 limits.py）",
          quota_for("guest").daily_token_budget != quota_for("member").daily_token_budget,
          f"{quota_for('guest').daily_token_budget:,} vs {quota_for('member').daily_token_budget:,}")

    # 端到端：游客档的额度真的会拒，会员档下同样用量不算超；
    # 而且两档的账是分开记的（member 档花掉的量不会记到 guest 账上）。
    probe = TokenBudget(guest_daily_token_budget_per_ip=0)
    over_guest = quota_for("guest").daily_token_budget + 1

    tok = set_current_identity(new_identity())
    probe.check_preflight(org_id="shared", ip=None, message="hi")
    probe.add_tokens("shared", over_guest)
    guest_blocked = False
    try:
        probe.check_preflight(org_id="shared", ip=None, message="hi")
    except BudgetExceeded as e:
        guest_blocked = e.code == "daily_budget_exhausted"
    reset_current_identity(tok)
    check("游客档下超过游客额度会被拒", guest_blocked)

    tok = set_current_identity(member_identity("u" + "e" * 16))
    member_ok = True
    try:
        # 同一个 org_id，但当前是会员档：游客账上的超额不该影响这里
        probe.check_preflight(org_id="shared", ip=None, message="hi")
    except BudgetExceeded:
        member_ok = False
    reset_current_identity(tok)
    check("两档的用量账目互相隔离（游客超了不影响会员）", member_ok)

    tok = set_current_identity(member_identity("u" + "f" * 16))
    probe.add_tokens("member_only", over_guest)
    still_ok = True
    try:
        probe.check_preflight(org_id="member_only", ip=None, message="hi")
    except BudgetExceeded:
        still_ok = False
    reset_current_identity(tok)
    check("会员档下同样的用量远未触顶", still_ok)

    # 档位来自上下文：游客身份触发的额度也是游客档
    tok = set_current_identity(new_identity())
    snap = budget.snapshot("o1")
    reset_current_identity(tok)
    check("游客身份的用量快照是 guest 档", snap["tier"] == "guest", snap)

    tok = set_current_identity(member_identity("u" + "b" * 16))
    snap2 = budget.snapshot("o2")
    reset_current_identity(tok)
    check("会员身份的用量快照是 member 档", snap2["tier"] == "member", snap2)

    # 游客的每 IP 每日总量：换身份（新 cookie）也绕不过
    ip_budget = TokenBudget(guest_daily_token_budget_per_ip=500)
    tok = set_current_identity(new_identity())
    try:
        ip_budget.check_preflight(org_id="o1", ip="1.2.3.4", message="hi")
        ip_budget.add_tokens("o1", 500, ip="1.2.3.4")
        # 换一个全新身份（等价于清 cookie），同 IP 仍应被拦
        tok2 = set_current_identity(new_identity())
        ip_budget.check_preflight(org_id="o2", ip="1.2.3.4", message="hi")
        check("游客换身份后同 IP 仍被总量拦住", False, "没有拦住")
    except BudgetExceeded as e:
        check("游客换身份后同 IP 仍被总量拦住", e.code == "guest_ip_budget_exhausted", e.code)
    finally:
        reset_current_identity(tok)

    check("游客额度用尽时的文案会引导注册",
          "注册" in _message_for(quota_for("guest")), _message_for(quota_for("guest")))
    check("会员额度用尽时的文案不提注册",
          "注册" not in _message_for(quota_for("member")))


def _message_for(spec) -> str:
    """触发一次「额度用尽」，把提示文案抓出来。"""
    b = TokenBudget()
    b.daily_token_budget = spec.daily_token_budget
    identity = new_identity() if spec.tier == "guest" else member_identity("u" + "c" * 16)
    tok = set_current_identity(identity)
    try:
        b.add_tokens("o1", spec.daily_token_budget + 1)
        try:
            b.check_preflight(org_id="o1", ip=None, message="hi")
            return ""
        except BudgetExceeded as e:
            return e.message
    finally:
        reset_current_identity(tok)


# ---------------------------------------------------------------------------
# 5. HTTP：注册 / 登录 / 登出 / 档位
# ---------------------------------------------------------------------------

def test_http_flow() -> None:
    routes._agent = object()
    store = routes.get_user_store()

    c = TestClient(app)
    me = c.get("/api/auth/me").json()
    check("未登录时 /api/auth/me 报游客", me.get("registered") is False and me.get("tier") == "guest", me)

    ident = c.get("/api/identity").json()
    check("/api/identity 带档位且为游客", ident.get("tier") == "guest", ident)

    # 注册
    r = c.post("/api/auth/register", json={"username": "TestUser01", "password": "12345678"})
    check("注册成功返回 201", r.status_code == 201, r.status_code)
    body = r.json()
    check("注册响应含用户信息且不含哈希",
          body["user"]["username"] == "testuser01" and "password_hash" not in json_dumps(body), body)
    check("注册后带上了会话 cookie", SESSION_COOKIE_NAME in c.cookies, list(c.cookies))

    me = c.get("/api/auth/me").json()
    check("注册后即登录（member 档）", me.get("registered") is True and me.get("tier") == "member", me)

    ident = c.get("/api/identity").json()
    check("身份档位随之变成 member", ident.get("tier") == "member", ident)

    usage = c.get("/api/usage").json()
    check("额度快照变成会员档", usage["tier"] == "member" and usage["tokens_budget"] >= 1_000_000, usage)

    # 重名与弱密码
    r = c.post("/api/auth/register", json={"username": "testuser01", "password": "12345678"})
    check("重名注册 → 409", r.status_code == 409, r.status_code)
    r = c.post("/api/auth/register", json={"username": "another", "password": "short"})
    check("弱密码 → 400", r.status_code == 400, r.status_code)

    # 登出
    r = c.post("/api/auth/logout")
    check("登出成功", r.status_code == 200, r.status_code)
    me = c.get("/api/auth/me").json()
    check("登出后回到游客档", me.get("registered") is False and me.get("tier") == "guest", me)

    # 登录
    r = c.post("/api/auth/login", json={"username": "TESTUSER01", "password": "12345678"})
    check("登录成功（用户名大小写不敏感）", r.status_code == 200 and r.json()["tier"] == "member", r.status_code)
    r = c.post("/api/auth/login", json={"username": "testuser01", "password": "wrong-password"})
    check("错误密码 → 401", r.status_code == 401, r.status_code)
    r = c.post("/api/auth/login", json={"username": "ghost", "password": "12345678"})
    check("不存在的账号 → 401（与错密码同一响应）", r.status_code == 401, r.status_code)

    # 游客不能用上传
    guest = TestClient(app)
    r = guest.post("/api/files/upload", files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")}, data={"org_id": ""})
    check("游客上传被拒 → 403 且提示注册",
          r.status_code == 403 and "注册" in r.json().get("detail", ""), r.json().get("detail"))


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def test_identity_cannot_be_forged() -> None:
    """档位在签名 cookie 里，客户端改不动；伪造一律降级为游客。"""
    from app.identity import issue_token, verify_token

    member = member_identity("u" + "d" * 16)
    token = issue_token(member)
    body, sig = token.rsplit(".", 1)
    import base64
    import json

    padded = body + "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    # 必须真的改成一个**不同的**值：member_identity 造出来本来就是 member，
    # 若这里再设成 "member"，body 一模一样，测的就不是"篡改被拒"了。
    payload["t"] = "superuser"
    forged_body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    check("篡改确实改变了 payload（否则这条测试无意义）", forged_body != body)
    forged = f"{forged_body}.{sig}"
    check("改了档位但签名不匹配 → 拒绝", verify_token(forged) is None, verify_token(forged))

    # 未知档位值一律按游客
    payload2 = json.loads(base64.urlsafe_b64decode(padded))
    payload2["t"] = "superuser"
    b2 = base64.urlsafe_b64encode(
        json.dumps(payload2, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    from app.identity import _secret
    import hashlib
    import hmac

    sig2 = base64.urlsafe_b64encode(hmac.new(_secret(), b2.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    got = verify_token(f"{b2}.{sig2}")
    check("合法签名但档位取值非法 → 降级为游客", got is not None and got.tier == "guest", got)


def test_session_cookie_behaviour() -> None:
    """会话 cookie 必须是 HttpOnly——前端不需要读它，脚本就别想碰到。"""
    from app.auth import session_cookie_kwargs

    kw = session_cookie_kwargs()
    check("会话 cookie 是 HttpOnly", kw["httponly"] is True, kw)
    check("会话 cookie 是 SameSite=lax", kw["samesite"] == "lax", kw)
    check("会话 cookie 路径为 /", kw["path"] == "/", kw)


def test_configured_budget_is_tiered() -> None:
    """**直接测生产用的那个预算单例**。

    为什么单独一条：其他用例都在自建 ``TokenBudget()`` 上验证分档，而真实的
    ``get_budget()`` 曾经把会员档的 ``CHAT_*`` 当构造参数传进去（那是"覆盖两档"
    的语义），结果线上游客的 ``/api/usage`` 显示 200 万/天——游客限制静默失效，
    而所有单元用例照样全绿。这条用例就是补上这个盲区。
    """
    from app.budget import get_budget
    from app.config import (
        CHAT_DAILY_TOKEN_BUDGET,
        GUEST_DAILY_TOKEN_BUDGET,
        GUEST_MAX_INPUT_TOKENS,
        GUEST_RPM_PER_USER,
    )

    b = get_budget()
    guest = b.spec_for("guest")
    member = b.spec_for("member")

    check("生产单例：游客档用的是游客配置",
          guest.daily_token_budget == GUEST_DAILY_TOKEN_BUDGET
          and guest.rpm_per_user == GUEST_RPM_PER_USER
          and guest.max_input_tokens == GUEST_MAX_INPUT_TOKENS,
          guest)
    check("生产单例：会员档用的是会员配置",
          member.daily_token_budget == CHAT_DAILY_TOKEN_BUDGET, member)
    check("生产单例：两档确实不同（不是被同一个覆盖值抹平）",
          guest.daily_token_budget < member.daily_token_budget
          and guest.rpm_per_user < member.rpm_per_user
          and guest.max_input_tokens < member.max_input_tokens,
          f"guest={guest} member={member}")

    # 游客的 /api/usage 必须反映游客额度——这是用户实际看到的那条路径
    tok = set_current_identity(new_identity())
    snap = b.snapshot("o_guest_probe")
    reset_current_identity(tok)
    check("游客的用量快照显示游客额度（线上踩过的那个洞）",
          snap["tier"] == "guest" and snap["tokens_budget"] == GUEST_DAILY_TOKEN_BUDGET,
          snap)


def main() -> int:
    test_passwords()
    test_credential_rules()
    test_sessions()
    test_user_store()
    test_quota_tiers()
    test_configured_budget_is_tiered()
    test_http_flow()
    test_identity_cannot_be_forged()
    test_session_cookie_behaviour()

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
