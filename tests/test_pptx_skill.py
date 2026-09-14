"""simple-pptx-generator 技能的自测。

跑不了 PowerPoint，所以用「读回 + 几何校验」代替肉眼看：
结构、页数、尺寸、文本框不越界、文本框不重叠、条目数上限、中文字体提示、
图表数据是否真的写进去了。顺带验证命令行契约（--json / 缺参数 / 坏数据）。

运行：.venv\\Scripts\\python.exe tests\\test_pptx_skill.py   （需要 python-pptx，属 dev 依赖）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "simple-pptx-generator" / "generate_pptx.py"
sys.path.insert(0, str(SCRIPT.parent))

from pptx import Presentation  # noqa: E402
from pptx.util import Emu  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   [" + str(detail) + "]") if detail else ""))


def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8")


def boxes(slide):
    """(left, top, width, height, text) —— 单位英寸。"""
    out = []
    for sh in slide.shapes:
        if sh.has_text_frame and sh.text_frame.text.strip():
            out.append((Emu(sh.left).inches, Emu(sh.top).inches,
                        Emu(sh.width).inches, Emu(sh.height).inches,
                        sh.text_frame.text.strip()))
    return out


def overlap(a, b, tol=0.06):
    ax, ay, aw, ah, _ = a
    bx, by, bw, bh, _ = b
    return (ax < bx + bw - tol and bx < ax + aw - tol and
            ay < by + bh - tol and by < ay + ah - tol)


def main():
    out_dir = Path(tempfile.mkdtemp(prefix="pptxskill-"))
    deck = out_dir / "q1.pptx"

    # ---------- 1. 命令行契约 ----------
    r = run_cli("--topic", "2026 Q1 业务复盘",
                "--points", "市场概况,增长驱动：用户增长；渠道扩张,风险：供应链；合规,下一步：Q2 聚焦",
                "--page-num", "8", "--data", "12,25,31,44",
                "--subtitle", "汇报人：阿林", "--output", str(deck), "--json")
    check("CLI 退出码 0", r.returncode == 0, r.stderr[-200:])

    payload = None
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        check("--json 的 stdout 是纯 JSON", False, f"{e}: {r.stdout[:120]}")
    if payload:
        check("--json 的 stdout 是纯 JSON", True)
        check("返回 ok / output / pages / bytes",
              payload.get("ok") is True and payload.get("output") and payload.get("pages") and payload.get("bytes"),
              {k: payload.get(k) for k in ("ok", "pages", "bytes")})
        check("output 指向真实文件", Path(payload["output"]).exists(), payload["output"])

    check("文件确实生成", deck.exists(), deck)

    # ---------- 2. 结构 ----------
    prs = Presentation(str(deck))
    slides = list(prs.slides)
    check("页数与 --json 一致", len(slides) == payload["pages"], f"{len(slides)} vs {payload['pages']}")
    check("尺寸是 16:9", abs(Emu(prs.slide_width).inches - 13.333) < 0.01
          and abs(Emu(prs.slide_height).inches - 7.5) < 0.01,
          f"{Emu(prs.slide_width).inches:.3f}x{Emu(prs.slide_height).inches:.3f}")

    cover = "\n".join(t for *_x, t in boxes(slides[0]))
    check("封面含主题", "2026 Q1 业务复盘" in cover, cover[:60])
    check("封面含副标题", "汇报人：阿林" in cover)

    last = "\n".join(t for *_x, t in boxes(slides[-1]))
    check("收尾页是「谢谢」", "谢谢" in last, last[:40])

    kinds = [t.split("\n")[0] for slide in slides for *_x, t in boxes(slide)][:1]
    check("首个小标签来自封面主题", "2026 Q1 业务复盘" in kinds[0])

    # 目录页在点数 >= 4 且页数够时出现
    all_text = "\n".join(t for s in slides for *_x, t in boxes(s))
    check("生成了目录页", "目录" in all_text)

    # 数据页 + 图表
    chart_slides = [s for s in slides if any(getattr(sh, "has_chart", False) for sh in s.shapes)]
    check("生成了 1 页图表", len(chart_slides) == 1, len(chart_slides))
    if chart_slides:
        plot = chart_slides[0].shapes
        chart = next(sh.chart for sh in plot if getattr(sh, "has_chart", False))
        series = chart.plots[0].series[0]
        values = [float(v) for v in series.values]
        check("图表数值与 --data 一致", values == [12.0, 25.0, 31.0, 44.0], values)

    # ---------- 3b. 分隔符语义（回归：分号曾经同时当论点分隔符） ----------
    # 4 个论点，其中 2 个各带 2 条子条目
    sep_points = "市场概况,增长驱动：用户增长；渠道扩张,风险：供应链；合规,下一步：Q2 聚焦"
    r = run_cli("--topic", "分隔符", "--points", sep_points,
                "--page-num", "8", "--output", str(out_dir / "sep.pptx"), "--json")
    psep = json.loads(r.stdout)
    # 4 论点 + 封面 + 目录 + 收尾 = 7 页；若分号也被当论点分隔符会变成 11 页
    check("分号不拆论点（4 论点 → 7 页）", psep["pages"] == 7, psep["pages"])

    prs_sep = Presentation(str(out_dir / "sep.pptx"))
    slides_sep = list(prs_sep.slides)
    agenda_text = "\n".join(t for *_x, t in boxes(slides_sep[1]))
    titles_in_agenda = [t for t in ("市场概况", "增长驱动", "风险", "下一步") if t in agenda_text]
    check("目录列出全部 4 条论点", len(titles_in_agenda) == 4, agenda_text.replace("\n", " / ")[:110])
    check("目录不列出子条目（渠道扩张/供应链不是论点）",
          "渠道扩张" not in agenda_text and "供应链" not in agenda_text, agenda_text.replace("\n", " / ")[:110])

    # 带子条目的页：标题与条目各就各位
    def slide_texts(slides_, head):
        for s in slides_:
            texts = [b[4] for b in boxes(s)]
            if any(t.strip() == head for t in texts):
                return texts
        return []

    texts4 = slide_texts(slides_sep, "增长驱动")
    check("带子条目的页把冒号前当标题", "增长驱动" in texts4, texts4)
    bullets4 = [t for t in texts4 if t.startswith("—")]
    check("子条目各自成行（同一框内两个段落）",
          len(bullets4) == 1 and "用户增长" in bullets4[0] and "渠道扩张" in bullets4[0], bullets4)

    # 每个内容页：页标题不应在条目里重复出现
    def heading_of(texts):
        for t in texts:
            t = t.strip()
            if t.startswith("—") or t.isdigit() or t.startswith("要点 ") or t == "AGENDA":
                continue
            return t
        return ""

    def dup_headings(slides_):
        bad = []
        for i, s in enumerate(slides_, start=1):
            texts = [b[4] for b in boxes(s)]
            head = heading_of(texts)
            if not head:
                continue
            for t in texts:
                if t.startswith("—"):
                    for line in t.split("\n"):
                        if line.strip().lstrip("— ").strip() == head:
                            bad.append((i, head))
        return bad

    check("页标题不在条目里重复", not dup_headings(slides_sep), dup_headings(slides_sep))

    # 论点多于页数时也不应重复
    r = run_cli("--topic", "压缩", "--points",
                "增长驱动：用户增长；渠道扩张,风险：供应链；合规,市场概况,下一步：Q2 聚焦",
                "--page-num", "4", "--output", str(out_dir / "merge.pptx"), "--json")
    prs_m = Presentation(str(out_dir / "merge.pptx"))
    check("合并模式下页标题也不重复", not dup_headings(list(prs_m.slides)), dup_headings(list(prs_m.slides)))

    # ---------- 4. 几何：不越界、不重叠 ----------
    W, H = 13.333, 7.5
    outside, overlapped = [], []
    for i, slide in enumerate(slides, start=1):
        bs = boxes(slide)
        for b in bs:
            if b[0] < -0.01 or b[1] < -0.01 or b[0] + b[2] > W + 0.01 or b[1] + b[3] > H + 0.01:
                outside.append((i, b[4][:20], round(b[0], 2), round(b[1], 2), round(b[2], 2), round(b[3], 2)))
        for j in range(len(bs)):
            for k in range(j + 1, len(bs)):
                if overlap(bs[j], bs[k]):
                    overlapped.append((i, bs[j][4][:16], bs[k][4][:16]))
    check("所有文本框都在页面内", not outside, outside[:3])
    check("文本框互不重叠", not overlapped, overlapped[:3])

    # ---------- 4. 硬性约束 ----------
    for i, slide in enumerate(slides, start=1):
        for b in boxes(slide):
            n = len([ln for ln in b[4].split("\n") if ln.strip()])
            if n > 5:
                check(f"第 {i} 页条目 ≤ 5", False, f"{n} 条")
                break
        else:
            continue
        break
    else:
        check("每页条目 ≤ 5", True)

    # 中文字体：任取一个中文 run，检查 <a:ea> 已写入
    ea_ok = False
    for slide in slides:
        for sh in slide.shapes:
            if not sh.has_text_frame:
                continue
            for para in sh.text_frame.paragraphs:
                for run in para.runs:
                    if any("\u4e00" <= c <= "\u9fff" for c in run.text):
                        xml = run._r.xml
                        if "a:ea" in xml and "微软雅黑" in xml:
                            ea_ok = True
                        break
                if ea_ok:
                    break
            if ea_ok:
                break
        if ea_ok:
            break
    check("中文 run 写入了 <a:ea> 字体提示", ea_ok)

    # ---------- 5. 边界输入 ----------
    r = run_cli("--topic", "只有主题", "--output", str(out_dir / "min.pptx"), "--json")
    check("只有 --topic 也能出片", r.returncode == 0 and (out_dir / "min.pptx").exists(), r.stderr[-150:])
    if r.returncode == 0:
        p2 = json.loads(r.stdout)
        check("无论点时页数=2（封面+收尾）", p2["pages"] == 2, p2["pages"])

    r = run_cli("--topic", "x", "--data", "1,abc,3", "--output", str(out_dir / "bad.pptx"), "--json")
    check("--data 非数字时报错且是 JSON", r.returncode != 0 and json.loads(r.stdout).get("ok") is False,
          r.stdout[:100])

    r = run_cli("--points", "a,b", "--output", str(out_dir / "notopic.pptx"), "--json")
    check("缺少 --topic 时非零退出", r.returncode != 0, r.returncode)

    # 要点多于页数 -> 合并，不超页
    many = ",".join(f"论点{i}" for i in range(1, 13))
    r = run_cli("--topic", "压缩测试", "--points", many, "--page-num", "5",
                "--output", str(out_dir / "many.pptx"), "--json")
    p3 = json.loads(r.stdout)
    check("要点多于页数时合并而非超页", p3["pages"] <= 5, p3["pages"])

    # 页数给大也不空凑
    r = run_cli("--topic", "不凑页", "--points", "一,二", "--page-num", "20",
                "--output", str(out_dir / "few.pptx"), "--json")
    p4 = json.loads(r.stdout)
    check("页数给大不会空凑", p4["pages"] == 4, p4["pages"])   # 封面+2 内容+收尾

    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("failed: " + "; ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
