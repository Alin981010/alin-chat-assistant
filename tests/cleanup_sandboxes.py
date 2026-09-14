"""临时脚本：列出并删除 OpenSandbox 上残留的沙箱容器（温柔关闭 app 时会自动清理，
强杀进程则会残留，靠 10 分钟 TTL 兜底）。密钥从 .env 读取，不打印。"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

base = os.getenv("OPEN_SANDBOX_DOMAIN", "localhost:8080")
base = base if base.startswith("http") else "http://" + base
headers = {"OPEN-SANDBOX-API-KEY": os.getenv("OPEN_SANDBOX_API_KEY", "")}

with httpx.Client(base_url=base, headers=headers, timeout=60.0) as c:
    r = c.get("/v1/sandboxes")
    r.raise_for_status()
    data = r.json()
    items = data.get("items") or data.get("sandboxes") or (data if isinstance(data, list) else [])
    print(f"残留沙箱 {len(items)} 个")
    for it in items:
        sid = it.get("id") if isinstance(it, dict) else None
        if not sid:
            continue
        d = c.delete(f"/v1/sandboxes/{sid}")
        print(f"  delete {sid} -> {d.status_code}")
    r = c.get("/v1/sandboxes")
    left = r.json()
    left_items = left.get("items") or left.get("sandboxes") or (left if isinstance(left, list) else [])
    print(f"清理后剩余 {len(left_items)} 个")
    sys.exit(0 if not left_items else 1)
