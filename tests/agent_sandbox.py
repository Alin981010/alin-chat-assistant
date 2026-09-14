"""临时验证：agent 是否真的通过沙箱完成大文件分析（完整闭环）。

routes → agent → 沙箱 backend → /workspace 里的文件 → 生成的脚本 → 结果 → 回复
"""

import io
import json
import os
import sys
import time

import httpx

BASE = os.getenv("BASE", "http://127.0.0.1:8088")
ORG = "default-org"
USER = "sandbox-e2e-user"

big = "id,payload,score\n" + "\n".join(f"{i},x{'y' * 30},{i % 97}" for i in range(1200))

with httpx.Client(base_url=BASE, timeout=60.0, ) as c:
    r = c.post("/api/files/upload", files={"file": ("sales.csv", io.BytesIO(big.encode()), "text/csv")},
               data={"org_id": ORG})
    r.raise_for_status()
    info = r.json()
    print(f"upload -> file_id={info['file_id']} rows={info['row_count']} analysis={info['analysis_type']}")
    assert info["analysis_type"] == "code_execution", info["analysis_type"]

    payload = {
        "message": "这个文件有多少行数据？score 列的平均值、最大值分别是多少？请给出具体数字。",
        "thread_id": f"{ORG}__{USER}__sbx1",
        "user_id": USER,
        "org_id": ORG,
        "file_id": info["file_id"],
    }

    tool_calls, tool_results, reason_chars, reply = [], [], 0, ""
    t0 = time.time()
    deadline = t0 + 300
    with httpx.Client(timeout=httpx.Timeout(300.0, read=300.0), ) as sc:
        with sc.stream("POST", BASE + "/api/chat-with-file/stream", json=payload) as resp:
            resp.raise_for_status()
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    for line in frame.split("\n"):
                        if not line.startswith("data:"):
                            continue
                        try:
                            d = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        t = d.get("type")
                        if t == "tool_call":
                            tool_calls.append(d.get("name"))
                            print(f"  [{time.time()-t0:5.1f}s] tool_call  {d.get('name')}")
                        elif t == "tool_result":
                            tool_results.append(d.get("output") or "")
                            print(f"  [{time.time()-t0:5.1f}s] tool_result {(d.get('output') or '')[:90]!r}")
                        elif t == "reasoning_token":
                            reason_chars += len(d.get("content") or "")
                        elif t == "token":
                            reply += d.get("content") or ""
                        elif t == "error":
                            print(f"  !! error: {d.get('message')}")
                        elif t == "done":
                            reply = d.get("reply") or reply
                if time.time() > deadline:
                    print("  !! 超时中断")
                    break

    print(f"\n耗时 {time.time()-t0:.1f}s · 工具调用 {tool_calls} · 推断字数 {reason_chars} · 回复 {len(reply)} 字")
    print("回复:", reply[:400].replace("\n", " "))

    ok_tools = any(n in ("execute", "write_file", "read_file", "edit_file") for n in tool_calls)
    print()
    print(("PASS  " if ok_tools else "FAIL  ") + f"agent 调用了沙箱工具 {tool_calls}")
    print(("PASS  " if "execute" in tool_calls else "FAIL  ") + "agent 执行了 sandbox execute")
    print(("PASS  " if len(reply) > 30 else "FAIL  ") + f"给出了文字回复（{len(reply)} 字）")
    sys.exit(0 if (ok_tools and len(reply) > 30) else 1)
