"""额度与限流验证：让「一直被对话烧 token」这件事在回归时不会悄悄失效。

    python tests/test_budget.py

覆盖四层：

1. 窗口限流——按身份、按 IP，超限抛 429（带 Retry-After）；
2. 每日额度——累计到上限后拒绝、流式过程中能中途截断、按天重置；
3. 单次上限——输入过长直接拒；模型侧 max_tokens 与 recursion_limit 已生效；
4. 落库恢复——重启后当天用量不归零，隔天的记录不恢复。

不依赖 Postgres：持久化用一个假的 store 验证回调契约。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-do-not-use-in-production")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from app import limits  # noqa: E402
from app import routes  # noqa: E402
from app.budget import (  # noqa: E402
    BudgetExceeded,
    TokenBudget,
    client_ip,
    estimate_tokens,
    extract_tokens_from_chunk,
)

RESULTS = []


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail != "" else ""))


class FakeRequest:
    """只实现 budget.client_ip 用到的那几个属性。"""

    def __init__(self, host: str = "203.0.113.9", headers: dict | None = None, peer: str | None = None):
        self.headers = headers or {}
        self.client = type("C", (), {"host": peer or host})()


# ---------------------------------------------------------------------------
# 1. 窗口限流
# ---------------------------------------------------------------------------

def test_rate_limits() -> None:
    b = TokenBudget(per_user_rpm=3, per_ip_rpm=5, daily_token_budget=0, max_input_tokens=0)

    for i in range(3):
        b.check_preflight(org_id="o1", ip="1.1.1.1", message="hi")
    check("身份窗口：前 3 次放行", True)
    check("身份窗口：第 4 次被拒",
          _raises(lambda: b.check_preflight(org_id="o1", ip="1.1.1.1", message="hi"), "user_rate_limited"))

    # 另一个身份不受影响（各自计数）
    b.check_preflight(org_id="o2", ip="2.2.2.2", message="hi")
    check("身份窗口按身份独立计数", True)

    # IP 窗口：清 cookie 换身份也绕不过同 IP 的上限
    b2 = TokenBudget(per_user_rpm=100, per_ip_rpm=2, daily_token_budget=0, max_input_tokens=0)
    b2.check_preflight(org_id="a1", ip="9.9.9.9", message="x")
    b2.check_preflight(org_id="a2", ip="9.9.9.9", message="x")
    check("IP 窗口：换身份仍被同 IP 拦下",
          _raises(lambda: b2.check_preflight(org_id="a3", ip="9.9.9.9", message="x"), "ip_rate_limited"))

    # 窗口过期后恢复：把记录时间往前挪 61 秒
    # 计数键现在带档位前缀（``tier:org``），不再直接是 org_id——取键要走 _key()。
    b3 = TokenBudget(per_user_rpm=1, per_ip_rpm=0, daily_token_budget=0, max_input_tokens=0)
    b3.check_preflight(org_id="ok", ip=None, message="x")
    key = b3._key(b3._current_tier(), "ok")
    with b3._lock:
        b3._usage[key].window = [t - 61 for t in b3._usage[key].window]
    b3.check_preflight(org_id="ok", ip=None, message="x")
    check("窗口滑出后恢复放行", True)


def _raises(fn, code: str) -> bool:
    try:
        fn()
    except BudgetExceeded as e:
        return e.code == code
    return False


# ---------------------------------------------------------------------------
# 2. 每日额度
# ---------------------------------------------------------------------------

def test_daily_budget() -> None:
    b = TokenBudget(per_user_rpm=0, per_ip_rpm=0, daily_token_budget=1000, max_input_tokens=0)
    b.check_preflight(org_id="o1", ip=None, message="hi")
    check("额度未满时放行", True)

    b.add_tokens("o1", 1000)
    check("累计到上限后拒绝",
          _raises(lambda: b.check_preflight(org_id="o1", ip=None, message="hi"), "daily_budget_exhausted"))
    check("流式中途检测到额度耗尽", b.exceeded_midway("o1") is True)
    check("额度是独立的：另一个身份不受影响", b.exceeded_midway("o2") is False)

    snap = b.snapshot("o1")
    check("快照反映真实用量", snap["tokens_used"] == 1000 and snap["tokens_budget"] == 1000, snap)

    # 按天重置：把 day 改成昨天，再取一次应该归零
    b._usage[b._key(b._current_tier(), "o1")].day = "2000-01-01"
    b.check_preflight(org_id="o1", ip=None, message="hi")
    check("跨天自动归零（新的一天重新开始）", b.snapshot("o1")["tokens_used"] == 0)

    unlimited = TokenBudget(per_user_rpm=0, per_ip_rpm=0, daily_token_budget=0, max_input_tokens=0)
    unlimited.add_tokens("o1", 10 ** 9)
    unlimited.check_preflight(org_id="o1", ip=None, message="hi")
    check("CHAT_DAILY_TOKEN_BUDGET=0 表示不限", unlimited.exceeded_midway("o1") is False)


# ---------------------------------------------------------------------------
# 3. 单次上限与模型侧封顶
# ---------------------------------------------------------------------------

def test_single_request_and_model_caps() -> None:
    b = TokenBudget(per_user_rpm=0, per_ip_rpm=0, daily_token_budget=0, max_input_tokens=100)
    check("短消息放行", b.check_preflight(org_id="o1", ip=None, message="hi") <= 100)
    check("超长消息被拒",
          _raises(lambda: b.check_preflight(org_id="o1", ip=None, message="字" * 400), "input_too_large"))

    check("token 估算对中文合理", estimate_tokens("你好世界") == 2, estimate_tokens("你好世界"))

    from app.config import AGENT_MAX_OUTPUT_TOKENS, AGENT_RECURSION_LIMIT

    check("单次输出有上限（AGENT_MAX_OUTPUT_TOKENS > 0）", AGENT_MAX_OUTPUT_TOKENS > 0, AGENT_MAX_OUTPUT_TOKENS)
    check("agent 轮次有上限且远小于 DeepAgents 默认的 9999",
          0 < AGENT_RECURSION_LIMIT < 9999, AGENT_RECURSION_LIMIT)

    from app.agent_setup import build_model
    model = build_model()
    check("模型实例带上了 max_tokens",
          getattr(model, "max_tokens", None) == AGENT_MAX_OUTPUT_TOKENS,
          getattr(model, "max_tokens", None))


def test_usage_extraction() -> None:
    class Chunk:
        def __init__(self, usage=None, meta=None):
            self.usage_metadata = usage
            self.response_metadata = meta or {}

    check("从 usage_metadata 取 total_tokens",
          extract_tokens_from_chunk(Chunk({"total_tokens": 123})) == 123)
    check("从 response_metadata.token_usage 取",
          extract_tokens_from_chunk(Chunk(None, {"token_usage": {"total_tokens": 77}})) == 77)
    check("从 prompt+completion 求和",
          extract_tokens_from_chunk(Chunk(None, {"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}})) == 15)
    check("拿不到用量时返回 0（由调用方补估算）",
          extract_tokens_from_chunk(Chunk()) == 0)


# ---------------------------------------------------------------------------
# 4. 落库与恢复
# ---------------------------------------------------------------------------

def test_persistence() -> None:
    b = TokenBudget(per_user_rpm=0, per_ip_rpm=0, daily_token_budget=5000, max_input_tokens=0)
    saved = []
    b.attach_store(lambda key, day, tokens, requests: saved.append((key, day, tokens, requests)))

    b.check_preflight(org_id="o1", ip=None, message="hi")
    b.add_tokens("o1", 42)
    # 计数键现在带档位前缀（``tier:org``）：同一 org 在两档下各记各的，
    # 这样"游客额度"与"会员额度"不会互相污染。
    check("每次变更都会尝试落库（键带档位前缀）",
          len(saved) >= 2 and saved[-1][0].endswith(":o1"),
          saved[-1] if saved else None)

    # 落库失败不能影响对话
    b2 = TokenBudget(per_user_rpm=0, per_ip_rpm=0, daily_token_budget=5000, max_input_tokens=0)
    b2.attach_store(lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    b2.check_preflight(org_id="o1", ip=None, message="hi")
    b2.add_tokens("o1", 10)
    check("落库异常被吞掉，限流照常工作", b2.snapshot("o1")["tokens_used"] == 10)

    # 恢复：同一天 → 恢复；隔天 → 丢弃
    fresh = TokenBudget(per_user_rpm=0, per_ip_rpm=0, daily_token_budget=5000, max_input_tokens=0)
    today = fresh._today()
    check("恢复当天记录", fresh.restore("o1", today, 1234, 5) is True)
    check("恢复后用量正确", fresh.snapshot("o1")["tokens_used"] == 1234)
    check("恢复不带上窗口（重启后不该莫名被限）",
          fresh.snapshot("o1")["tokens_used"] == 1234 and _can_call(fresh))
    check("隔天的记录被丢弃", fresh.restore("o2", "2000-01-01", 9999, 9) is False)
    check("非法记录不炸", fresh.restore("o3", today, "abc", None) is False)


def _can_call(b: TokenBudget) -> bool:
    try:
        b.check_preflight(org_id="o1", ip=None, message="hi")
        return True
    except BudgetExceeded:
        return False


# ---------------------------------------------------------------------------
# 5. HTTP 层：429 + Retry-After，且不进入 agent
# ---------------------------------------------------------------------------

def test_http_429() -> None:
    routes._agent = object()

    budget = routes.get_budget()
    # 把限额压到 1 次/分，方便触发（并清空已有计数）
    old_user_rpm, old_ip_rpm = budget.per_user_rpm, budget.per_ip_rpm
    old_max_input = budget.max_input_tokens
    budget.per_user_rpm, budget.per_ip_rpm = 1, 0
    with budget._lock:
        budget._usage.clear()

    client_a = TestClient(app)
    ida = client_a.get("/api/identity").json()

    def send(c, message="你好"):
        return c.post("/api/chat/stream", json={
            "message": message,
            "thread_id": f"{ida['org_id']}__{ida['user_id']}__beef0001",
            "user_id": ida["user_id"],
            "org_id": ida["org_id"],
        })

    try:
        r1 = send(client_a)
        check("第一次对话进入处理流程（非 429）", r1.status_code != 429, r1.status_code)

        r2 = send(client_a)
        check("第二次立刻被限流 → 429", r2.status_code == 429, r2.status_code)
        check("429 带 Retry-After", r2.headers.get("retry-after") is not None, r2.headers.get("retry-after"))
        detail2 = r2.json().get("detail", "")
        check("429 的 detail 是给人看的中文", "频繁" in detail2 or "受限" in detail2, detail2)
        # TestClient 没带会话 cookie，所以走的是游客档——文案必须引导注册。
        # 这条断言把「游客体验不好就直接流失」这个产品要求钉住。
        check("游客被限流时的文案引导注册", "注册" in detail2, detail2)

        # 换个身份：额度互相独立
        client_b = TestClient(app)
        idb = client_b.get("/api/identity").json()
        r3 = client_b.post("/api/chat/stream", json={
            "message": "你好",
            "thread_id": f"{idb['org_id']}__{idb['user_id']}__beef0001",
            "user_id": idb["user_id"],
            "org_id": idb["org_id"],
        })
        check("另一个身份不受影响", r3.status_code != 429, r3.status_code)

        # 输入过长 → 429/400 之前就该被拦
        budget.per_user_rpm = 100
        budget.max_input_tokens = 50
        r4 = send(client_a, message="字" * 400)
        detail4 = r4.json().get("detail", "")
        check("超长消息被拒（拿不到额度就烧不了钱）",
              r4.status_code == 429 and ("过长" in detail4 or "受限" in detail4),
              detail4)
    finally:
        budget.per_user_rpm, budget.per_ip_rpm = old_user_rpm, old_ip_rpm
        budget.max_input_tokens = old_max_input
        with budget._lock:
            budget._usage.clear()


def test_usage_endpoint() -> None:
    c = TestClient(app)
    u = c.get("/api/usage")
    check("/api/usage 返回额度快照",
          u.status_code == 200 and "tokens_used" in u.json() and "tokens_budget" in u.json(),
          u.json())


def test_client_ip() -> None:
    check("无 XFF 时用直连地址",
          client_ip(FakeRequest(peer="203.0.113.9")) == "203.0.113.9")
    check("直连是私网时才采信 XFF 的最后一段",
          client_ip(FakeRequest(peer="172.17.0.2", headers={"x-forwarded-for": "1.2.3.4, 10.0.0.9"})) == "10.0.0.9")
    check("公网直连时忽略可伪造的 XFF",
          client_ip(FakeRequest(peer="203.0.113.9", headers={"x-forwarded-for": "1.2.3.4"})) == "203.0.113.9")


def main() -> int:
    test_rate_limits()
    test_daily_budget()
    test_single_request_and_model_caps()
    test_usage_extraction()
    test_persistence()
    test_http_429()
    test_usage_endpoint()
    test_client_ip()

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
