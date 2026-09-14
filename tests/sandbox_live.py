"""临时验证：真实沙箱端到端。

1. POST /api/sandbox/execute —— 真的在容器里跑命令
2. 上传大文件（>12000 字符）—— 确认分流到 code_execution 并生成脚本
3. 在沙箱里 ls /workspace —— 确认上传的文件确实落进了容器
"""

import io
import os
import sys

import httpx

BASE = os.getenv("BASE", "http://127.0.0.1:8088")
ORG = "default-org"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   [" + str(detail) + "]") if detail else ""))


with httpx.Client(base_url=BASE, timeout=180.0, ) as c:
    # 0. 状态
    st = c.get("/api/sandbox/status").json()
    check("沙箱可用", st.get("available") is True, st)

    # 1. 真跑一段代码
    cmd = 'python3 -c "import platform,sys;print(\'py=\'+platform.python_version());print(sum(range(101)))"'
    r = c.post("/api/sandbox/execute", json={"code": cmd, "timeout": 60, "org_id": ORG})
    body = r.json() if r.status_code == 200 else {"raw": r.text}
    check("POST /api/sandbox/execute 返回 200", r.status_code == 200, r.status_code)
    out = (body.get("output") or "")
    check("容器里跑出 Python 版本与 5050", "py=3." in out and "5050" in out, out.strip().replace("\n", " | "))
    check("exit_code == 0", body.get("exit_code") == 0, body.get("exit_code"))

    # 2. 执行后应有一个活跃沙箱
    st2 = c.get("/api/sandbox/status").json()
    check("沙箱状态记录到活跃容器", st2.get("available") is True and st2.get("active_count", 0) >= 1, st2)

    # 3. 大文件 -> 应为 code_execution 并返回脚本
    big = "id,payload,score\n" + "\n".join(f"{i},x{'y' * 30},{i % 97}" for i in range(1200))
    raw = big.encode()
    r = c.post("/api/files/upload", files={"file": ("big.csv", io.BytesIO(raw), "text/csv")}, data={"org_id": ORG})
    check("上传大文件 200", r.status_code == 200, r.status_code)
    info = r.json()
    check("大文件分流到 code_execution", info.get("analysis_type") == "code_execution", info.get("analysis_type"))
    check("生成了沙箱分析脚本", bool(info.get("code")), (info.get("code") or "")[:0] or f"{len(info.get('code') or '')} chars")
    check("行数/列名解析正确", info.get("row_count") == 1200 and info.get("columns") == ["id", "payload", "score"],
          (info.get("row_count"), info.get("columns")))

    # 4. 脚本里的 PATH 必须与落到沙箱里的路径一致
    expected = f"/workspace/{info['file_id']}_big.csv"
    check("生成脚本 PATH 指向该文件", expected in (info.get("code") or ""), expected)

    # 5. 去容器里确认文件真的在，且内容完整
    #    注意：wc -l 数的是换行符，CSV 末行无换行会少 1，所以用 Python 语义计数
    count_cmd = f'python3 -c "print(\'LINES=\', sum(1 for _ in open(\'{expected}\')))"'
    r = c.post("/api/sandbox/execute", json={"code": f"ls -la /workspace && {count_cmd}", "timeout": 60, "org_id": ORG})
    out = (r.json().get("output") or "") if r.status_code == 200 else r.text
    check("沙箱 /workspace 里存在该文件", "big.csv" in out, out.strip().replace("\n", " | ")[:160])
    check("容器内共 1201 行（1200 数据 + 表头）", "LINES= 1201" in out, out.strip().replace("\n", " | ")[-80:])

    # 6. 沙箱里没有 pandas —— 这决定了脚本必须只用标准库
    r = c.post("/api/sandbox/execute", json={"code": "python3 -c \"import pandas\" 2>&1 | tail -1", "timeout": 60, "org_id": ORG})
    out = (r.json().get("output") or "") if r.status_code == 200 else r.text
    check("沙箱确实没有 pandas（脚本必须纯标准库）", "ModuleNotFoundError" in out or "No module named" in out,
          out.strip()[:90])

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
if FAIL:
    print("failed: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
