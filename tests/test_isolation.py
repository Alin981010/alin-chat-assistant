"""身份隔离验证：跨用户越权必须被拒。

与同目录其他脚本一样，独立运行、不是 pytest：

    python tests/test_isolation.py

覆盖三块：

1. 令牌层——签名、篡改、格式；
2. 授权助手——_resolve_org / _resolve_user / _safe_download_path / _guard_thread_owner；
3. HTTP 层——不同访客拿到不同 org；拿别人的 org 请求一律 403/400。

不依赖 Postgres 与 OpenSandbox：直接构造 app 并 monkeypatch 掉沙箱调用，
只验证「谁能碰什么」这一层逻辑。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 固定密钥，避免测试之间因懒加载顺序不同而拿到不同 secret
os.environ.setdefault("APP_SECRET_KEY", "test-secret-do-not-use-in-production")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from app import identity as ident  # noqa: E402
from app import routes  # noqa: E402
from app.identity import (  # noqa: E402
    Identity,
    get_current_identity,
    issue_token,
    new_identity,
    set_current_identity,
    verify_token,
)

RESULTS = []


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail != "" else ""))


def expects_http(callable_, status: int) -> bool:
    """断言 callable_ 抛出指定状态码的 HTTPException。"""
    try:
        callable_()
    except HTTPException as e:
        return e.status_code == status
    return False


# ---------------------------------------------------------------------------
# 1. 令牌层
# ---------------------------------------------------------------------------

def test_tokens() -> None:
    me = new_identity()
    token = issue_token(me)
    back = verify_token(token)
    check("令牌往返：签发的身份能原样读回", back == me, back)

    check("令牌篡改：改一个字符即失效", verify_token(token[:-1] + ("a" if token[-1] != "a" else "b")) is None)

    forged = issue_token(Identity(org_id="o" + "a" * 23, user_id=me.user_id))
    check("伪造令牌：换了 payload 但签名不匹配", verify_token(forged.split(".")[0] + "." + token.split(".")[1]) is None)

    check("空令牌 / 乱码 / 缺分隔符都被拒",
          all(verify_token(v) is None for v in (None, "", "garbage", "a.b.c", "....")))

    check("历史遗留 org 值被判定为 legacy",
          ident.is_legacy_org("default-org") and ident.is_legacy_org(None) and ident.is_legacy_org("")
          and not ident.is_legacy_org("o0123456789abcdef"))

    check("非法格式的 id 不被接受",
          not ident.is_valid_id("short") and not ident.is_valid_id("has space in it")
          and not ident.is_valid_id("a" * 65) and ident.is_valid_id("o0123456789abcdef"))


# ---------------------------------------------------------------------------
# 2. 授权助手（直接调用，绕过 HTTP）
# ---------------------------------------------------------------------------

def test_helpers() -> None:
    me = Identity(org_id="o" + "1" * 23, user_id="u" + "2" * 23)
    other = Identity(org_id="o" + "3" * 23, user_id="u" + "4" * 23)
    tok = set_current_identity(me)
    try:
        check("org 一致时放行", routes._resolve_org(me.org_id) == me.org_id)
        check("org 为空时回落到身份值", routes._resolve_org(None) == me.org_id)
        check("org=default-org 被视为未认证并回落",
              routes._resolve_org("default-org") == me.org_id)
        check("拿别人的 org 请求 → 403", expects_http(lambda: routes._resolve_org(other.org_id), 403))

        check("user 一致时采信", routes._resolve_user(me.user_id) == me.user_id)
        check("user 不一致时忽略请求值", routes._resolve_user(other.user_id) == me.user_id)

        allowed = [
            "/workspace/report.pptx",
            "/workspace/a/b/c.csv",
            "/workspace/./x.txt",
        ]
        blocked = [
            "/etc/passwd",
            "/workspace/../../etc/shadow",
            "/workspace/../root/.aws/credentials",
            "workspace/rel.txt",
            "/workspaceX/evil.sh",
            "/root/.bashrc",
            "/workspace/\x00evil",
            "",
        ]
        check("合法 /workspace 路径被放行",
              all(routes._safe_download_path(p) is not None for p in allowed))
        check("越界路径全部被拒", all(routes._safe_download_path(p) is None for p in blocked),
              [p for p in blocked if routes._safe_download_path(p) is not None])

        own_thread = f"{me.org_id}__{me.user_id}__abcd1234"
        other_thread = f"{other.org_id}__{other.user_id}__abcd1234"
        same_org_peer = f"{me.org_id}__{other.user_id}__abcd1234"
        check("自己的会话放行", not expects_http(lambda: routes._guard_thread_owner(own_thread, me.user_id), 403))
        check("别人的 org 会话 → 403", expects_http(lambda: routes._guard_thread_owner(other_thread, me.user_id), 403))
        check("同 org 内别人的会话 → 403",
              expects_http(lambda: routes._guard_thread_owner(same_org_peer, me.user_id), 403))
        check("缺少分隔符的会话标识 → 403",
              expects_http(lambda: routes._guard_thread_owner("plainthread", me.user_id), 403))
    finally:
        ident.reset_current_identity(tok)


# ---------------------------------------------------------------------------
# 3. HTTP 层
# ---------------------------------------------------------------------------

def test_http() -> None:
    # 不跑 lifespan：那会去连 Postgres。直接把路由的全局状态铺好。
    routes._agent = object()

    calls = []

    class FakeSandbox:
        def execute(self, code, timeout=None):
            calls.append(("execute", code, timeout))
            return type("R", (), {"exit_code": 0, "output": "ok"})()

        def download_files(self, paths):
            calls.append(("download", tuple(paths)))
            resp = type("R", (), {"error": None, "content": b"ok"})()
            return [resp for _ in paths]

    def fake_run_in_sandbox(org_id, fn):
        calls.append(("run", org_id))
        return fn(FakeSandbox())

    routes.run_in_sandbox = fake_run_in_sandbox
    # 沙箱可用性在真实环境里由启动探测决定（此处没有 lifespan），
    # 显式置为可用，才能验证「越权先于可用性被拦」。
    routes._sandbox_available = lambda: True

    c1 = TestClient(app)
    c2 = TestClient(app)

    id1 = c1.get("/api/identity").json()
    id2 = c2.get("/api/identity").json()
    check("不同访客拿到不同 org_id", id1["org_id"] != id2["org_id"], f"{id1['org_id']} vs {id2['org_id']}")
    check("下发的 org_id 合法且不是 default-org",
          ident.is_valid_id(id1["org_id"]) and id1["org_id"] != "default-org")

    again = c1.get("/api/identity").json()
    check("同一 cookie 再次请求身份稳定", again == id1, again)

    # 伪造 cookie：签名不通过 → 换一个全新身份，而不是接受伪造值
    c3 = TestClient(app)
    c3.cookies.set(ident.COOKIE_NAME, issue_token(Identity(org_id="o" + "9" * 23, user_id="u" + "9" * 23))[:-2] + "xx")
    id3 = c3.get("/api/identity").json()
    check("篡改 cookie 不会得到伪造的 org_id", id3["org_id"] != "o" + "9" * 23, id3["org_id"])

    r = c1.get(f"/api/sandbox/download?org_id={id2['org_id']}&path=/workspace/x.pptx")
    check("用别人的 org 下载 → 403", r.status_code == 403, r.status_code)

    r = c1.get(f"/api/sandbox/download?org_id={id1['org_id']}&path=/etc/passwd")
    check("下载 /etc/passwd → 400", r.status_code == 400, r.status_code)

    r = c1.get(f"/api/sandbox/download?org_id={id1['org_id']}&path=/workspace/../../etc/shadow")
    check("下载路径含 .. → 400", r.status_code == 400, r.status_code)

    calls.clear()
    r = c1.get(f"/api/sandbox/download?org_id={id1['org_id']}&path=/workspace/real.pptx")
    check("合法下载被放行", r.status_code == 200, r.status_code)

    r = c1.get("/api/sandbox/download?path=/etc/passwd")
    check("不带 org_id 的越界下载同样被拒（回落身份后仍卡 path）",
          r.status_code == 400, r.status_code)

    peer_thread = f"{id1['org_id']}__{id2['user_id']}__deadbeef"
    r = c1.get(f"/api/history/{peer_thread}")
    check("读同 org 内别人的会话历史 → 403（未带 user_id 也必须被拦）",
          r.status_code == 403, r.status_code)

    r = c1.get(f"/api/history/{peer_thread}?user_id={id2['user_id']}")
    check("带别人的 user_id 读会话历史 → 403", r.status_code == 403, r.status_code)

    own_thread = f"{id1['org_id']}__{id1['user_id']}__cafe0001"
    r = c1.get(f"/api/history/{own_thread}")
    check("读自己的会话历史 → 200（放行不被误伤）", r.status_code == 200, r.status_code)

    r = c1.delete(f"/api/threads/{id2['org_id']}__{id2['user_id']}__deadbeef")
    check("删别人的会话 → 403", r.status_code == 403, r.status_code)

    r = c2.delete(f"/api/threads/{id1['org_id']}__{id1['user_id']}__deadbeef")
    check("删别人 org 的会话 → 403", r.status_code == 403, r.status_code)

    calls.clear()
    r = c1.post("/api/sandbox/execute", json={
        "org_id": id2["org_id"], "code": "echo hi", "timeout": 5,
    })
    check("用别人的 org 执行代码 → 403（且没有真的打到沙箱）",
          r.status_code == 403 and not any(c[0] == "run" for c in calls), r.status_code)

    calls.clear()
    r = c1.post("/api/sandbox/execute", json={
        "org_id": id1["org_id"], "code": "echo hi", "timeout": 10,
    })
    used_timeout = next((c[2] for c in calls if c[0] == "execute"), None)
    check("自己的 org 可以执行，且 timeout 被夹到上限内",
          r.status_code == 200 and used_timeout is not None
          and 1 <= used_timeout <= routes.EXECUTE_TIMEOUT_MAX,
          f"{r.status_code} / timeout={used_timeout}")

    calls.clear()
    r = c1.post("/api/sandbox/execute", json={
        "org_id": id1["org_id"], "code": "sleep 999", "timeout": 99999,
    })
    used_timeout = next((c[2] for c in calls if c[0] == "execute"), None)
    check("超大 timeout 被夹到 EXECUTE_TIMEOUT_MAX",
          used_timeout == routes.EXECUTE_TIMEOUT_MAX, used_timeout)

    routes.run_in_sandbox = __import__("app.sandbox", fromlist=["run_in_sandbox"]).run_in_sandbox
    routes._sandbox_available = __import__("app.sandbox", fromlist=["_sandbox_available"])._sandbox_available


def main() -> int:
    test_tokens()
    test_helpers()
    test_http()

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
