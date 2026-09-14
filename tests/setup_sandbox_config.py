"""临时脚本：把 .env 里的 OPEN_SANDBOX_API_KEY 写进 ~/.sandbox.toml 的 [server] api_key。

不打印密钥本身，只打印掩码与校验结果。
"""

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

key = (os.getenv("OPEN_SANDBOX_API_KEY") or "").strip()
if not key:
    sys.exit("OPEN_SANDBOX_API_KEY 为空，先填 .env")
if key.lower() in {"xxxx", "your-secret-api-key", "changeme"}:
    sys.exit(f"OPEN_SANDBOX_API_KEY 看起来还是占位符：{key!r}")

cfg = Path(os.path.expanduser("~")) / ".sandbox.toml"
if not cfg.exists():
    sys.exit(f"找不到 {cfg}，先跑 opensandbox-server init-config")

text = cfg.read_text(encoding="utf-8")
before = text

# 1) 打开被注释掉的 api_key，并写入真实值
text = re.sub(
    r'^#\s*api_key\s*=\s*".*?"\s*$',
    "api_key = " + json.dumps(key),
    text,
    count=1,
    flags=re.M,
)
# 2) 如果已经有未注释的 api_key，直接替换
if text == before:
    text = re.sub(
        r'^api_key\s*=\s*".*?"\s*$',
        "api_key = " + json.dumps(key),
        text,
        count=1,
        flags=re.M,
    )
if text == before:
    sys.exit("没找到可替换的 [server] api_key 行，请手工确认 ~/.sandbox.toml")

cfg.write_text(text, encoding="utf-8")

# 校验：不依赖 toml 库，自己把值抠出来比对
m = re.search(r'^api_key\s*=\s*"(.*)"\s*$', text, flags=re.M)
got = m.group(1) if m else None
mask = lambda v: f"{v[:2]}{'*' * (len(v) - 4)}{v[-2:]} (len={len(v)})" if v and len(v) > 4 else "<short>"

print(f"config  : {cfg}")
print(f"api_key : {mask(got or '')}")
print(f"与 .env 一致: {got == key}")
print(f"host    : {re.search(r'^host = .*$', text, flags=re.M).group(0)}")
print(f"network : {re.search(r'^network_mode = .*$', text, flags=re.M).group(0)}")
print(f"runtime : {re.search(r'^type = .*$', text, flags=re.M).group(0)}")
print(f"execd   : {re.search(r'^execd_image = .*$', text, flags=re.M).group(0)}")
