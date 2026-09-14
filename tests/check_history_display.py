"""回放展示体检：刷新页面后，前端从 /api/history + /api/threads 拿到的东西对不对。

覆盖三件事（都是「刷新后才看得到」的展示路径）：
  1. 思考过程在流式里推得出来（实时展示靠它）
  2. 思考过程落库了（刷新后仍能折叠展示）
  3. 用户气泡与会话标题里**不该**出现内部提示脚手架（【运行环境】/【已上传文件内容】…）

运行：先起 app，再 .venv\\Scripts\\python.exe tests\\check_history_display.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import time

import httpx

BASE = os.getenv("BASE", "http://127.0.0.1:8088")
ORG = "default-org"
USER = "display-check"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   [" + str(detail) + "]") if detail else ""))


def chat(message, thread_id, file_id=None):
    """跑一轮流式对话，返回 (reasoning 片段列表, done 里的 reasoning, 回复)。"""
    url = "/api/chat-with-file/stream" if file_id else "/api/chat/stream"
    payload = {"message": message, "thread_id": thread_id, "user_id": USER, "org_id": ORG}
    if file_id:
        payload["file_id"] = file_id

    chunks, done_reasoning, reply = [], None, ""
    with httpx.Client(timeout=httpx.Timeout(300.0, read=300.0)) as c:
        with c.stream("POST", BASE + url, json=payload) as resp:
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
                        if t == "reasoning_token":
                            chunks.append(d.get("content") or "")
                        elif t == "token":
                            reply += d.get("content") or ""
                        elif t == "done":
                            reply = d.get("reply") or reply
                            done_reasoning = d.get("reasoning")
    return chunks, done_reasoning, reply


def history(thread_id):
    return httpx.get(f"{BASE}/api/history/{thread_id}", timeout=60.0).json().get("messages", [])


def main():
    # ---------- 纯文本一轮 ----------
    t1 = f"{ORG}__{USER}__{int(time.time())}"
    chunks, done_reasoning, reply = chat("用一句话解释什么是递归，先想清楚再回答。", t1)

    check("流式里推了 reasoning_token（实时展示）", bool(chunks), f"{len(chunks)} 段")
    check("done 事件带 reasoning", bool(done_reasoning), (done_reasoning or "")[:50].replace("\n", " "))
    check("回复非空", len(reply) > 0, f"{len(reply)} 字")

    msgs = history(t1)
    users = [m for m in msgs if m["role"] == "user"]
    assistants = [m for m in msgs if m["role"] == "assistant"]
    check("历史里带 reasoning（刷新后仍可折叠展示）",
          any((m.get("reasoning") or "").strip() for m in assistants),
          f"{sum(1 for m in assistants if (m.get('reasoning') or '').strip())}/{len(assistants)} 条 assistant")
    check("用户气泡就是原话（没夹带【运行环境】）",
          users and users[0]["content"] == "用一句话解释什么是递归，先想清楚再回答。",
          repr(users[0]["content"])[:110] if users else "无 user 消息")
    check("assistant 正文没被污染", assistants and "【运行环境】" not in assistants[0]["content"])

    threads = httpx.get(f"{BASE}/api/threads", params={"org_id": ORG, "user_id": USER}, timeout=60.0).json()
    mine = next((x for x in threads if x["thread_id"] == t1), None)
    check("会话列表能找到该会话", mine is not None, len(threads))
    if mine:
        check("会话标题不含内部脚手架",
              "【运行环境】" not in (mine.get("last_message") or "")
              and "【已上传文件" not in (mine.get("last_message") or ""),
              repr((mine.get("last_message") or ""))[:110])
        check("会话标题以用户原话开头",
              (mine.get("last_message") or "").startswith("用一句话解释"), repr(mine.get("last_message"))[:60])

    # ---------- 带文件一轮（脚手架最长的场景） ----------
    raw = b"a,b\n1,2\n3,4\n"
    fid = httpx.post(BASE + "/api/files/upload",
                     files={"file": ("notes.csv", io.BytesIO(raw), "text/csv")},
                     data={"org_id": ORG}, timeout=60.0).json()["file_id"]
    t2 = f"{ORG}__{USER}__{int(time.time())}"
    chat("这个文件几行？", t2, file_id=fid)
    msgs2 = history(t2)
    u2 = [m for m in msgs2 if m["role"] == "user"]
    check("带文件时用户气泡也不夹带文件内容",
          u2 and u2[0]["content"] == "这个文件几行？", repr(u2[0]["content"])[:140] if u2 else "无")
    check("带文件时也不夹带【已上传文件内容】",
          u2 and "【已上传文件内容】" not in u2[0]["content"])

    threads2 = httpx.get(f"{BASE}/api/threads", params={"org_id": ORG, "user_id": USER}, timeout=60.0).json()
    mine2 = next((x for x in threads2 if x["thread_id"] == t2), None)
    check("带文件的会话标题也不含文件内容",
          mine2 and (mine2.get("last_message") or "").startswith("这个文件几行"),
          repr((mine2.get("last_message") or ""))[:80])

    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("failed: " + "; ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
