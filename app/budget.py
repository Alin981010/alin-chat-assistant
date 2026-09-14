"""用量预算与限流：把「一直对话烧 token」这件事按住。

## 为什么单独一个模块

token 只在一条路径上产生：``/api/chat/stream`` 与 ``/api/chat-with-file/stream``
驱动的 agent 循环。而 agent 循环的开销有三个乘数，任何一个失控都能把额度烧光：

1. **轮次**——每发一条消息都会把整段历史再喂给模型一次，对话越长单条越贵；
2. **工具循环**——DeepAgents 默认 ``recursion_limit=9999``（deepagents/graph.py），
   一次提问理论上可以驱动成百上千次模型调用；
3. **单次输出**——不设 ``max_tokens`` 时，模型可以一路生成到上下文上限。

所以这里的四层分别对应：**窗口限流**（防刷）、**每日额度**（防总量失控）、
**单次输入/输出上限**（防单条爆炸）、**循环轮次上限**（防工具打转）。

## 计数口径

优先用模型返回的真实 usage（LangChain 的 ``usage_metadata``）；拿不到时用
字符数估算（``len/2``，对中英文混排偏保守）。估算只用于「提前拒绝明显超标的
请求」，实时累计以真实 usage 为准。

## 存储

内存计数 + 可选落库。应用是刻意单 worker 的（agent / store / 沙箱缓存都挂在
进程内全局变量上），所以内存计数在本进程内自洽；落库是为了让**重启不清零**，
否则刷满额度后重启服务就能重置，等于没限。

## 能被绕过的部分，说清楚

限流键是**签名身份**，而身份由 cookie 承载：访客清掉 cookie 就会拿到一个全新
身份，额度随之重置。所以每用户额度只是「提高滥用成本」，真正兜底的是按 IP 的
粗粒度窗口（``CHAT_MAX_REQUESTS_PER_MIN_PER_IP``）。两者都要开。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 限额默认值见 app/limits.py；实际取值由 app/config.py 读环境变量后传入。
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """粗估 token：中文约 1 字 1 token，英文约 4 字符 1 token，取 len/2 折中。"""
    return max(1, len(text) // 2)


@dataclass
class _DayUsage:
    """一天内的累计用量。"""

    day: str
    tokens: int = 0
    requests: int = 0
    warned: bool = False
    #: 每分钟窗口的请求时间戳
    window: List[float] = field(default_factory=list)


class BudgetExceeded(Exception):
    """额度或频率超限。带 Retry-After，便于直接回 429。"""

    def __init__(self, message: str, retry_after: int = 60, code: str = "rate_limited"):
        super().__init__(message)
        self.message = message
        self.retry_after = max(1, int(retry_after))
        self.code = code


@dataclass(frozen=True)
class QuotaSpec:
    """一档额度。游客与会员各一份，由 ``app/limits.py`` 提供默认值。"""

    tier: str
    daily_token_budget: int
    rpm_per_user: int
    max_input_tokens: int

    @property
    def is_member(self) -> bool:
        return self.tier == "member"


def _quota_specs() -> Dict[str, QuotaSpec]:
    """从配置构造两档额度。

    每次调用都读配置（而不是缓存），这样测试改配置后立刻生效；
    开销只是几次属性读取。
    """
    from .config import (
        CHAT_DAILY_TOKEN_BUDGET,
        CHAT_MAX_INPUT_TOKENS,
        CHAT_RPM_PER_USER,
        GUEST_DAILY_TOKEN_BUDGET,
        GUEST_MAX_INPUT_TOKENS,
        GUEST_RPM_PER_USER,
    )

    return {
        "guest": QuotaSpec(
            tier="guest",
            daily_token_budget=GUEST_DAILY_TOKEN_BUDGET,
            rpm_per_user=GUEST_RPM_PER_USER,
            max_input_tokens=GUEST_MAX_INPUT_TOKENS,
        ),
        "member": QuotaSpec(
            tier="member",
            daily_token_budget=CHAT_DAILY_TOKEN_BUDGET,
            rpm_per_user=CHAT_RPM_PER_USER,
            max_input_tokens=CHAT_MAX_INPUT_TOKENS,
        ),
    }


def quota_for(tier: Optional[str]) -> QuotaSpec:
    """按档位取额度。**认不出的档位一律按游客**——失败要往严的方向倒。"""
    specs = _quota_specs()
    return specs["member"] if tier == "member" else specs["guest"]


class TokenBudget:
    """按身份 + IP 的窗口限流与每日 token 额度，**按档位（游客/会员）分别计算**。

    档位不是构造参数，而是**每个请求**从上下文里的签名身份取（``app/identity.py``
    的 ``Identity.tier``）。这样同一个进程同时服务两种人，谁也不会花掉谁的额度。

    计数 key 一律带档位前缀（``member:o123``），即使某个 org_id 恰好两档都出现，
    也各记各的。

    线程安全：所有读改写都持有同一把锁。计数很轻（一次请求几条记录），
    不值得为它引入更细的并发结构。
    """

    def __init__(
        self,
        *,
        per_user_rpm: Optional[int] = None,
        per_ip_rpm: int = 30,
        daily_token_budget: Optional[int] = None,
        max_input_tokens: Optional[int] = None,
        guest_daily_token_budget_per_ip: int = 60_000,
    ) -> None:
        # 三个"每档不同"的限额默认是 None = **用该档在 app/limits.py 里的值**。
        # 这一点很重要：如果这里给个具体默认值（比如 200 万），它会同时覆盖
        # 游客档，把"游客额度大幅调低"这个需求悄悄抹平——实测踩过。
        # 传具体值则等于全局覆盖（受限部署可以借它统一收紧；测试也用它）。
        self.per_user_rpm = per_user_rpm
        self.per_ip_rpm = per_ip_rpm
        self.daily_token_budget = daily_token_budget
        self.max_input_tokens = max_input_tokens
        self.guest_daily_token_budget_per_ip = guest_daily_token_budget_per_ip
        self._lock = threading.Lock()
        self._usage: Dict[str, _DayUsage] = {}
        self._ip_hits: Dict[str, List[float]] = {}
        #: 可选的落库回调，签名 (key, day, tokens, requests) -> None
        self._persist: Optional[Any] = None

    # -- 档位 --------------------------------------------------------------

    def spec_for(self, tier: Optional[str]) -> QuotaSpec:
        """取某档的额度。实例属性为 ``None`` 时用该档自己的默认值。

        于是"游客 2 万 / 会员 200 万"这个差别来自 ``app/limits.py``；
        需要全局收紧（或测试里临时压额度）时，传一个具体值即可覆盖两档。
        """
        spec = quota_for(tier)
        return QuotaSpec(
            tier=spec.tier,
            daily_token_budget=(
                spec.daily_token_budget if self.daily_token_budget is None else self.daily_token_budget
            ),
            rpm_per_user=(
                spec.rpm_per_user if self.per_user_rpm is None else self.per_user_rpm
            ),
            max_input_tokens=(
                spec.max_input_tokens if self.max_input_tokens is None else self.max_input_tokens
            ),
        )

    @staticmethod
    def _key(tier: str, org_id: str) -> str:
        return f"{tier}:{org_id}"

    @staticmethod
    def _current_tier() -> str:
        """当前请求的档位。取不到身份时按游客算（宁可少给，不可多给）。"""
        from .identity import get_current_identity

        identity = get_current_identity()
        return identity.tier if identity is not None else "guest"

    # -- 落库（可选） ------------------------------------------------------

    def attach_store(self, persist) -> None:
        """挂上持久化回调。

        回调抛异常不能影响对话，所以调用点全部包了 try。语义是「尽力而为」：
        落库失败时内存计数照常生效，只是重启后会丢。
        """
        self._persist = persist

    def _persist_safe(self, key: str, usage: _DayUsage) -> None:
        if self._persist is None:
            return
        try:
            self._persist(key, usage.day, usage.tokens, usage.requests)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"用量落库失败（不影响本次限流）：{e}")

    def restore(self, org_id: str, day: Optional[str], tokens: Any, requests: Any) -> bool:
        """从持久化记录恢复某身份当天的用量；不是今天的记录直接丢弃。

        key 与 ``check_preflight`` 一致（带档位前缀）。落库时写入的也是这个 key
        （见 ``app/routes.py`` 的 persist 回调），两边必须对齐，否则重启后
        "恢复了但没生效"。

        返回是否真的恢复了（调用方据此打日志）。恢复时**不带窗口**——重启前的
        "这一分钟问了几次"没有保留价值，带上反而会让用户重启后莫名被限。
        """
        if day != self._today():
            return False
        try:
            restored_tokens = max(0, int(tokens or 0))
            restored_requests = max(0, int(requests or 0))
        except (TypeError, ValueError):
            return False

        key = self._key(self._current_tier(), org_id)
        with self._lock:
            self._usage[key] = _DayUsage(
                day=day,
                tokens=restored_tokens,
                requests=restored_requests,
            )
        return True

    # -- 内部 -------------------------------------------------------------

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _entry(self, key: str) -> _DayUsage:
        day = self._today()
        entry = self._usage.get(key)
        if entry is None or entry.day != day:
            entry = _DayUsage(day=day)
            self._usage[key] = entry
        return entry

    def _prune_window(self, hits: List[float], now: float) -> None:
        cutoff = now - 60.0
        hits[:] = [t for t in hits if t > cutoff]

    # -- 对外 -------------------------------------------------------------

    def check_preflight(
        self,
        *,
        org_id: str,
        ip: Optional[str],
        message: str,
        extra_chars: int = 0,
    ) -> int:
        """在开始调用模型之前做全部检查，返回本次输入的估算 token 数。

        顺序刻意如此：先看额度（已被用尽的访客不必再刷窗口计数），再看频率，
        最后看单次输入大小。任何一项不过就抛 ``BudgetExceeded``。

        档位从当前请求的签名身份取；游客与会员各自一套额度、各自计数。
        """
        tier = self._current_tier()
        spec = self.spec_for(tier)
        key = self._key(tier, org_id)
        estimated_input = estimate_tokens(message) + max(0, extra_chars) // 2
        now = time.time()

        with self._lock:
            entry = self._entry(key)

            if spec.daily_token_budget > 0 and entry.tokens >= spec.daily_token_budget:
                if spec.is_member:
                    message_text = "今日额度已用完，请明天再来。"
                else:
                    # 游客撞额度是最值得好好说话的一次——他很可能正是目标用户。
                    message_text = (
                        "游客体验额度已用完。注册一个账号即可获得完整额度（每天 "
                        f"{self._member_budget_hint()} tokens），并保留历史会话。"
                    )
                raise BudgetExceeded(
                    message_text,
                    retry_after=self._seconds_until_tomorrow(now),
                    code="daily_budget_exhausted",
                )

            # 游客的每 IP 每日总量：按身份的额度清 cookie 就能重置，这一层不能。
            if not spec.is_member and ip and self.guest_daily_token_budget_per_ip > 0:
                ip_entry = self._entry(self._ip_token_key(ip))
                if ip_entry.tokens >= self.guest_daily_token_budget_per_ip:
                    raise BudgetExceeded(
                        "当前网络今日的游客额度已用完。注册账号即可继续使用。",
                        retry_after=self._seconds_until_tomorrow(now),
                        code="guest_ip_budget_exhausted",
                    )

            self._prune_window(entry.window, now)
            if spec.rpm_per_user > 0 and len(entry.window) >= spec.rpm_per_user:
                retry_after = int(entry.window[0] + 60.0 - now) + 1
                if spec.is_member:
                    message_text = f"提问过于频繁，请 {max(1, retry_after)} 秒后再试。"
                else:
                    message_text = (
                        f"游客提问频率受限，请 {max(1, retry_after)} 秒后再试；"
                        "注册后每分钟可用次数更多。"
                    )
                raise BudgetExceeded(
                    message_text,
                    retry_after=retry_after,
                    code="user_rate_limited",
                )

            if ip and self.per_ip_rpm > 0:
                ip_hits = self._ip_hits.setdefault(ip, [])
                self._prune_window(ip_hits, now)
                if len(ip_hits) >= self.per_ip_rpm:
                    retry_after = int(ip_hits[0] + 60.0 - now) + 1
                    raise BudgetExceeded(
                        f"当前网络提问过于频繁，请 {max(1, retry_after)} 秒后再试。",
                        retry_after=retry_after,
                        code="ip_rate_limited",
                    )

            if spec.max_input_tokens > 0 and estimated_input > spec.max_input_tokens:
                if spec.is_member:
                    message_text = (
                        f"单次消息过长（约 {estimated_input} tokens，上限 {spec.max_input_tokens}）。"
                        "请拆成几次提问，或改用上传文件的方式。"
                    )
                else:
                    message_text = (
                        f"游客单条消息字数受限（约 {estimated_input} tokens，上限 "
                        f"{spec.max_input_tokens}）。注册后单条可发 6 万 tokens，并支持上传文件。"
                    )
                raise BudgetExceeded(
                    message_text,
                    retry_after=60,
                    code="input_too_large",
                )

            # 全部通过：登记本次请求
            entry.window.append(now)
            entry.requests += 1
            if ip:
                self._ip_hits.setdefault(ip, []).append(now)
            self._persist_safe(key, entry)

        return estimated_input

    def _member_budget_hint(self) -> str:
        try:
            return f"{quota_for('member').daily_token_budget:,}"
        except Exception:  # noqa: BLE001
            return "200,000"

    @staticmethod
    def _ip_token_key(ip: str) -> str:
        """游客每 IP 每日总量的计数 key（与身份计数区分开）。"""
        return f"guest_ip:{ip}"

    def add_tokens(self, org_id: str, tokens: int, *, ip: Optional[str] = None) -> int:
        """累加真实用量，返回该身份今日累计 token。

        游客同时累加"每 IP 总量"，这样清 cookie 换身份也绕不过总量上限。
        """
        if tokens <= 0:
            return 0
        tier = self._current_tier()
        key = self._key(tier, org_id)
        with self._lock:
            entry = self._entry(key)
            entry.tokens += int(tokens)
            total = entry.tokens
            self._persist_safe(key, entry)

            if tier != "member" and ip and self.guest_daily_token_budget_per_ip > 0:
                ip_entry = self._entry(self._ip_token_key(ip))
                ip_entry.tokens += int(tokens)
                self._persist_safe(self._ip_token_key(ip), ip_entry)
        return total

    def exceeded_midway(self, org_id: str, *, ip: Optional[str] = None) -> bool:
        """流式过程中检查额度是否已耗尽（用于在下一轮模型调用前截断）。"""
        tier = self._current_tier()
        spec = self.spec_for(tier)
        if spec.daily_token_budget <= 0:
            return False
        key = self._key(tier, org_id)
        with self._lock:
            entry = self._entry(key)
            if entry.tokens < spec.daily_token_budget:
                return False
            if not entry.warned:
                entry.warned = True
                logger.warning(f"{tier} org={org_id} 今日 token 额度已用尽，已中断本轮生成。")
            return True

    def snapshot(self, org_id: str) -> Dict[str, Any]:
        """给接口/前端展示的用量快照（按当前请求的档位）。

        前端靠 ``tier`` 决定是显示"游客体验额度"还是"今日额度"，
        靠 ``tokens_budget`` 画进度条；两者都必须是服务端说了算。
        """
        tier = self._current_tier()
        spec = self.spec_for(tier)
        key = self._key(tier, org_id)
        with self._lock:
            entry = self._entry(key)
            return {
                "day": entry.day,
                "tier": spec.tier,
                "tokens_used": entry.tokens,
                "tokens_budget": spec.daily_token_budget,
                "requests_today": entry.requests,
                "requests_per_min_limit": spec.rpm_per_user,
                "max_input_tokens": spec.max_input_tokens,
            }

    @staticmethod
    def _seconds_until_tomorrow(now: float) -> int:
        local = time.localtime(now)
        seconds_today = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
        return max(60, 86400 - seconds_today)


def extract_tokens_from_chunk(chunk: Any) -> int:
    """从一条流式 chunk 里取 token 用量；取不到返回 0。

    LangChain 不同集成把用量放在两个地方：

    - ``usage_metadata``：标准字段，``{"input_tokens","output_tokens","total_tokens"}``，
      流式下通常是**累计值**，所以取 ``total_tokens`` 直接覆盖，不要累加；
    - ``response_metadata["token_usage"]``：OpenAI 风格，部分集成只有这个。

    这里返回「本 chunk 报告的总量」，由调用方用 max() 维护单调递增的累计值。
    """
    total = 0
    usage = getattr(chunk, "usage_metadata", None)
    if isinstance(usage, dict):
        for field_name in ("total_tokens", "input_tokens", "output_tokens"):
            value = usage.get(field_name)
            if isinstance(value, int) and value > total:
                total = value

    meta = getattr(chunk, "response_metadata", None)
    if isinstance(meta, dict):
        token_usage = meta.get("token_usage") or meta.get("usage")
        if isinstance(token_usage, dict):
            value = token_usage.get("total_tokens")
            if not isinstance(value, int):
                value = (token_usage.get("prompt_tokens") or 0) + (token_usage.get("completion_tokens") or 0)
            if isinstance(value, int) and value > total:
                total = value
    return total


def client_ip(request: Any) -> Optional[str]:
    """取客户端 IP。

    ``X-Forwarded-For`` 只有在**前面确实有反向代理**时才可采信（否则访客可以
    随便伪造，把自己伪装成无数个 IP 来绕过按 IP 的限流）。后端没有代理配置项，
    所以这里保守处理：仅当请求来自本机/私网地址时才采信该头。

    另外 XFF 是「客户端, 代理1, 代理2」的形式，取**最后一段**（离我们最近的
    那跳由可信代理写入），而不是第一段（客户端可伪造）。
    """
    try:
        peer = request.client.host if request.client else None
    except Exception:  # noqa: BLE001
        peer = None

    if peer and _is_private(peer):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if hops:
                return hops[-1]
    return peer


def _is_private(address: str) -> bool:
    if address in {"127.0.0.1", "::1", "localhost"}:
        return True
    if address.startswith("10.") or address.startswith("192.168."):
        return True
    if address.startswith("172."):
        try:
            second = int(address.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


#: 全局单例。单 worker 前提下，进程内一份即可。
_budget: Optional[TokenBudget] = None
_budget_lock = threading.Lock()


def get_budget() -> TokenBudget:
    """取全局预算实例，首次调用时按配置构造。"""
    global _budget
    if _budget is None:
        with _budget_lock:
            if _budget is None:
                from .config import (
                    CHAT_DAILY_TOKEN_BUDGET,
                    CHAT_MAX_INPUT_TOKENS,
                    CHAT_RPM_PER_IP,
                    CHAT_RPM_PER_USER,
                    GUEST_DAILY_TOKEN_BUDGET,
                    GUEST_DAILY_TOKEN_BUDGET_PER_IP,
                    GUEST_MAX_INPUT_TOKENS,
                    GUEST_RPM_PER_USER,
                )
                from .limits import AGENT_MAX_OUTPUT_TOKENS, AGENT_RECURSION_LIMIT

                # **不要**把 CHAT_*（会员档）传给构造函数：那三个参数是"覆盖两档"
                # 的语义，传进去会把游客档也一起改成会员值——"游客额度大幅调低"
                # 就静默失效了。实测在线上踩到：游客的 /api/usage 显示 200 万/天。
                # 每档的额度由 quota_for() 从 limits/config 取，这里只传全局项。
                _budget = TokenBudget(
                    per_ip_rpm=CHAT_RPM_PER_IP,
                    guest_daily_token_budget_per_ip=GUEST_DAILY_TOKEN_BUDGET_PER_IP,
                )
                logger.info(
                    "TokenBudget 已初始化："
                    f"会员 {CHAT_DAILY_TOKEN_BUDGET:,} tokens/天、{CHAT_RPM_PER_USER} 次/分、"
                    f"单条 {CHAT_MAX_INPUT_TOKENS:,} tokens；"
                    f"游客 {GUEST_DAILY_TOKEN_BUDGET:,} tokens/天、{GUEST_RPM_PER_USER} 次/分、"
                    f"单条 {GUEST_MAX_INPUT_TOKENS} tokens、"
                    f"每 IP 游客总量 {GUEST_DAILY_TOKEN_BUDGET_PER_IP:,}；"
                    f"单次输出上限 {AGENT_MAX_OUTPUT_TOKENS} tokens，"
                    f"agent 轮次上限 {AGENT_RECURSION_LIMIT}"
                )
    return _budget


def _reset_budget_for_tests() -> None:
    """测试用：清掉单例。"""
    global _budget
    _budget = None
