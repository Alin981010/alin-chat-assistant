"""临时验证：密钥命名坑的「可诊断性」修复。

覆盖 _diagnose_sandbox_failure 的四条分支 + /api/sandbox/status 的返回结构。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opensandbox.config.connection_sync import ConnectionConfigSync  # noqa: E402

from app.config import SANDBOX_API_KEY, SANDBOX_DOMAIN  # noqa: E402
from app.sandbox import _diagnose_sandbox_failure, mask_secret  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   [" + str(detail) + "]") if detail else ""))


class FakeReadyTimeout(Exception):
    pass


FakeReadyTimeout.__name__ = "SandboxReadyTimeoutException"


def cfg(domain=None, key=None):
    return ConnectionConfigSync(domain=domain or SANDBOX_DOMAIN, api_key=key, use_server_proxy=False)


print(f"客户端读到: domain={SANDBOX_DOMAIN} api_key={mask_secret(SANDBOX_API_KEY)}")
print("-" * 72)

# 1. 连不上
code, msg = _diagnose_sandbox_failure(cfg(domain="127.0.0.1:9"), RuntimeError("boom"))
check("server 未启动 -> unreachable", code == "unreachable", f"{code}: {msg[:70]}")

# 2. 密钥不匹配（server 在跑，客户端给错钥匙）
code, msg = _diagnose_sandbox_failure(cfg(key="definitely-wrong-key"), RuntimeError("401"))
check("密钥错误 -> auth_mismatch", code == "auth_mismatch", f"{code}: {msg[:60]}")
check("提示里点名客户端变量", "OPEN_SANDBOX_API_KEY" in msg)
check("提示里点名服务端变量", "OPENSANDBOX_SERVER_API_KEY" in msg)
check("提示里点名 TOML 字段", "[server].api_key" in msg)
check("提示里说明前缀顺序相反", "顺序与客户端相反" in msg or "相反" in msg, msg[-40:])
check("拒绝的密钥被掩码而非明文", "definitely-wrong-key" not in msg, msg[:70])

# 3. 未配置密钥
#    注意：api_key="" 并不等于「没有密钥」——SDK 里是 `self.api_key or os.getenv(...)`，
#    显式传空会回落到环境变量。要模拟「真没配」必须把环境变量也摘掉。
_saved = os.environ.pop("OPEN_SANDBOX_API_KEY", None)
try:
    code, msg = _diagnose_sandbox_failure(cfg(key=""), RuntimeError("401"))
finally:
    if _saved is not None:
        os.environ["OPEN_SANDBOX_API_KEY"] = _saved
check("空密钥 -> auth_mismatch 且区分「没配」", code == "auth_mismatch" and "没有配置密钥" in msg, msg[:50])

# 4. 密钥正确（预检应通过）-> 归因到创建环节
code, msg = _diagnose_sandbox_failure(cfg(key=SANDBOX_API_KEY), RuntimeError("docker exploded"))
check("密钥正确 -> 落到 create_failed", code == "create_failed", f"{code}: {msg[:60]}")

# 5. 密钥正确 + 超时型异常 -> create_timeout
code, msg = _diagnose_sandbox_failure(cfg(key=SANDBOX_API_KEY), FakeReadyTimeout("ready timed out"))
check("密钥正确 + 超时 -> create_timeout", code == "create_timeout", f"{code}: {msg[:60]}")
check("超时提示给出 docker pull 建议", "docker pull" in msg, msg[:80])

# 6. mask_secret
check("mask_secret 短值不泄露", mask_secret("abcd") == "****（4 位）", mask_secret("abcd"))
check("mask_secret 长值保留首尾", mask_secret("abcdefghij").startswith("ab") and mask_secret("abcdefghij").endswith("ij（10 位）"))

# 7. /api/sandbox/status 结构
from app import sandbox as sb  # noqa: E402

st = sb.sandbox_status()
check("sandbox_status() 含四个字段", set(st) == {"available", "active_count", "code", "reason"}, set(st))
if st["available"]:
    check("可用时 code=ok 且 reason 为空", st["code"] == "ok" and st["reason"] == "", st)
else:
    check("不可用时给出 code/reason", st["code"] != "ok" and bool(st["reason"]), st)

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
sys.exit(1 if FAIL else 0)
