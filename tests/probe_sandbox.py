"""临时探针：在不打印密钥的前提下，检查 OpenSandbox server 是否可达、鉴权是否通过、镜像/容器现状。

用法：.venv\\Scripts\\python.exe tests\\probe_sandbox.py
"""

import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

DOMAIN = os.getenv("OPEN_SANDBOX_DOMAIN", "localhost:8080")
KEY = os.getenv("OPEN_SANDBOX_API_KEY", "") or ""
IMAGE = os.getenv("SANDBOX_IMAGE", "")
PROXY = os.getenv("SANDBOX_USE_SERVER_PROXY", "")

base = DOMAIN if DOMAIN.startswith("http") else "http://" + DOMAIN


def mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]} (len={len(value)})"


print(f"domain           = {DOMAIN}")
print(f"api_key          = {mask(KEY)}")
print(f"use_server_proxy = {PROXY or '<unset -> False>'}")
print(f"image            = {IMAGE}")
print("-" * 70)

with httpx.Client(timeout=15.0) as client:
    # 1. 健康检查（公开路由，不需要密钥）
    for path in ("/health", "/v1/health"):
        try:
            r = client.get(base + path)
            print(f"GET {path:16} -> {r.status_code} {r.text[:100]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"GET {path:16} -> 连接失败: {type(e).__name__}: {str(e)[:90]}")
            break

    # 2. 带密钥列沙箱：区分「连不上 / 401 / 能列」
    headers = {"OPEN-SANDBOX-API-KEY": KEY} if KEY else {}
    try:
        r = client.get(base + "/v1/sandboxes", headers=headers)
        print(f"GET /v1/sandboxes     -> {r.status_code}")
        if r.status_code == 401 or r.status_code == 403:
            print("   >>> 鉴权失败：.env 里的 OPEN_SANDBOX_API_KEY 与 server 的 [server].api_key 不一致")
        elif r.status_code == 200:
            data = r.json()
            items = data.get("items") or data.get("sandboxes") or (data if isinstance(data, list) else [])
            print(f"   >>> 鉴权通过，现有沙箱 {len(items)} 个")
            for it in items[:5]:
                if isinstance(it, dict):
                    print(f"       - id={it.get('id')} status={it.get('status')} image={(it.get('image') or {}).get('image') if isinstance(it.get('image'), dict) else it.get('image')}")
        else:
            print(f"   body: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"GET /v1/sandboxes     -> 连接失败: {type(e).__name__}: {str(e)[:120]}")
        print("   >>> server 没起来：请先 opensandbox-server")
