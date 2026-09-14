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


class TokenBudget:
    """按身份 + IP 的窗口限流与每日 token 额度。

    线程安全：所有读改写都持有同一把锁。计数很轻（一次请求几条记录），
    不值得为它引入更细的并发结构。
    """

    def __init__(
        self,
        *,
        per_user_rpm: int = 8,
        per_ip_rpm: int = 30,
        daily_token_budget: int = 2_000_000,
        max_input_tokens: int = 60_000,
    ) -> None:
        self.per_user_rpm = per_user_rpm
        self.per_ip_rpm = per_ip_rpm
        self.daily_token_budget = daily_token_budget
        self.max_input_tokens = max_input_tokens
        self._lock = threading.Lock()
        self._usage: Dict[str, _DayUsage] = {}
        self._ip_hits: Dict[str, List[float]] = {}
        #: 可选的落库回调，签名 (user_id, day, tokens, requests) -> None
        self._persist: Optional[Any] = None

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
        """从持久化记录恢复某 org 当天的用量；不是今天的记录直接丢弃。

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

        with self._lock:
            self._usage[org_id] = _DayUsage(
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

        顺序刻意如此：先看额度（已被禁的人不必再刷窗口计数），再看频率，
        最后看单次输入大小。任何一项不过就抛 ``BudgetExceeded``。
        """
        estimated_input = estimate_tokens(message) + max(0, extra_chars) // 2
        now = time.time()

        with self._lock:
            entry = self._entry(org_id)

            if self.daily_token_budget > 0 and entry.tokens >= self.daily_token_budget:
                raise BudgetExceeded(
                    "今日额度已用完，请明天再来。",
                    retry_after=self._seconds_until_tomorrow(now),
                    code="daily_budget_exhausted",
                )

            self._prune_window(entry.window, now)
            if self.per_user_rpm > 0 and len(entry.window) >= self.per_user_rpm:
                retry_after = int(entry.window[0] + 60.0 - now) + 1
                raise BudgetExceeded(
                    f"提问过于频繁，请 {max(1, retry_after)} 秒后再试。",
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

            if self.max_input_tokens > 0 and estimated_input > self.max_input_tokens:
                raise BudgetExceeded(
                    f"单次消息过长（约 {estimated_input} tokens，上限 {self.max_input_tokens}）。"
                    "请拆成几次提问，或改用上传文件的方式。",
                    retry_after=60,
                    code="input_too_large",
                )

            # 全部通过：登记本次请求
            entry.window.append(now)
            entry.requests += 1
            if ip:
                self._ip_hits.setdefault(ip, []).append(now)
            self._persist_safe(org_id, entry)

        return estimated_input

    def add_tokens(self, org_id: str, tokens: int) -> int:
        """累加真实用量，返回该身份今日累计 token。"""
        if tokens <= 0:
            return 0
        with self._lock:
            entry = self._entry(org_id)
            entry.tokens += int(tokens)
            total = entry.tokens
            self._persist_safe(org_id, entry)
        return total

    def exceeded_midway(self, org_id: str) -> bool:
        """流式过程中检查额度是否已耗尽（用于在下一轮模型调用前截断）。"""
        if self.daily_token_budget <= 0:
            return False
        with self._lock:
            entry = self._entry(org_id)
            if entry.tokens < self.daily_token_budget:
                return False
            if not entry.warned:
                entry.warned = True
                logger.warning(f"org={org_id} 今日 token 额度已用尽，已中断本轮生成。")
            return True

    def snapshot(self, org_id: str) -> Dict[str, Any]:
        """给接口/前端展示的用量快照。"""
        with self._lock:
            entry = self._entry(org_id)
            return {
                "day": entry.day,
                "tokens_used": entry.tokens,
                "tokens_budget": self.daily_token_budget,
                "requests_today": entry.requests,
                "requests_per_min_limit": self.per_user_rpm,
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
                )
                from .limits import AGENT_MAX_OUTPUT_TOKENS, AGENT_RECURSION_LIMIT

                _budget = TokenBudget(
                    per_user_rpm=CHAT_RPM_PER_USER,
                    per_ip_rpm=CHAT_RPM_PER_IP,
                    daily_token_budget=CHAT_DAILY_TOKEN_BUDGET,
                    max_input_tokens=CHAT_MAX_INPUT_TOKENS,
                )
                logger.info(
                    "TokenBudget 已初始化："
                    f"每用户 {CHAT_RPM_PER_USER} 次/分，每 IP {CHAT_RPM_PER_IP} 次/分，"
                    f"每日 {CHAT_DAILY_TOKEN_BUDGET or '不限'} tokens，"
                    f"单次输入上限 {CHAT_MAX_INPUT_TOKENS} tokens，"
                    f"单次输出上限 {AGENT_MAX_OUTPUT_TOKENS} tokens，"
                    f"agent 轮次上限 {AGENT_RECURSION_LIMIT}"
                )
    return _budget


def _reset_budget_for_tests() -> None:
    """测试用：清掉单例。"""
    global _budget
    _budget = None
