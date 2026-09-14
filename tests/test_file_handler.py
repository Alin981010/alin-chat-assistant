"""tools/file_handler.py 的自测。

覆盖：
  1. 各类型解析（csv/tsv/txt/md/json/jsonl/xlsx/docx/pdf）
  2. 错误路径（空文件、旧版 doc/xls、不支持的类型、坏 JSON、坏 ZIP）
  3. 魔数纠错（扩展名撒谎）
  4. generate_code_for_large_file 生成的脚本：先 compile，再真跑一遍，验证输出是 JSON
  5. app/routes.py 的 import 契约（用桩顶掉缺失的沙箱依赖）

运行：.venv\\Scripts\\python.exe tests\\test_file_handler.py
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import zipfile
import zlib
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.file_handler import FileHandler, ParsedFile  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("   [" + str(detail) + "]") if detail else ""))


def expect_error(name, fn, needle):
    try:
        fn()
    except ValueError as e:
        check(name, needle in str(e), f"{needle!r} not in {str(e)[:80]!r}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong exception {type(e).__name__}: {e}")
    else:
        check(name, False, "no exception raised")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

CSV = "id,name,score\n1,alpha,91\n2,beta,77\n3,gamma,88\n"
TSV = "id\tname\tscore\n1\talpha\t91\n2\tbeta\t77\n"
TXT = "第一行内容\nsecond line here\n第三行\n"
MD = "# 标题\n\n- 要点一\n- 要点二\n"
JSON_DOC = json.dumps([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], ensure_ascii=False)
JSONL = "\n".join(json.dumps(o) for o in [{"a": 1}, {"a": 2}, {"a": 3}])

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def make_docx() -> bytes:
    doc = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        "<w:p><w:r><w:t>季度总结</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>营收同比增长 </w:t></w:r><w:r><w:t>18%</w:t></w:r></w:p>"
        "<w:p/>"
        "<w:p><w:r><w:t>风险：供应链</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


def make_xlsx() -> bytes:
    wb = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>'
        f'<sheet name="数据" sheetId="1" r:id="rId1"/>'
        f'<sheet name="备注" sheetId="2" r:id="rId2"/>'
        "</sheets></workbook>"
    )
    rels = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{R_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{R_NS}/worksheet" Target="worksheets/sheet2.xml"/>'
        "</Relationships>"
    )
    shared = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{S_NS}"><si><t>产品</t></si><si><t>销量</t></si>'
        "<si><t>A 型</t></si><si><t>B 型</t></si></sst>"
    )
    sheet1 = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{S_NS}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>120</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>80</v></c></row>'
        "</sheetData></worksheet>"
    )
    sheet2 = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{S_NS}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>备注文本</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", wb)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1)
        zf.writestr("xl/worksheets/sheet2.xml", sheet2)
    return buf.getvalue()


def make_pdf(compress=False) -> bytes:
    content = b"BT /F1 12 Tf 72 720 Td (Hello PDF World) Tj ET"
    if compress:
        body = zlib.compress(content)
        header = b"<< /Length %d /Filter /FlateDecode >>" % len(body)
    else:
        body = content
        header = b"<< /Length %d >>" % len(body)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    out.write(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    out.write(b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    out.write(b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj\n")
    out.write(b"4 0 obj " + header + b"\nstream\n" + body + b"\nendstream\nendobj\n")
    out.write(b"trailer << /Root 1 0 R >>\n%%EOF\n")
    return out.getvalue()


# --------------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------------- #

def test_parsing(tmp: Path):
    p = FileHandler.parse("data.csv", CSV.encode())
    check("csv: 类型/行数/列名", p.file_type == "csv" and p.row_count == 3 and p.columns == ["id", "name", "score"], p)
    check("csv: 小文件走提示词直读", p.is_small is True and p.full_content.strip() == CSV.strip(), repr(p.full_content))
    check("csv: 返回值类型是 ParsedFile", isinstance(p, ParsedFile))

    p = FileHandler.parse("data.tsv", TSV.encode())
    check("tsv: 制表符分隔", p.columns == ["id", "name", "score"] and p.row_count == 2, p.columns)

    p = FileHandler.parse("notes.txt", TXT.encode())
    check("txt: 行数=非空行", p.file_type == "text" and p.row_count == 3 and p.columns == [], p)

    p = FileHandler.parse("readme.md", MD.encode())
    check("md: 标为 markdown", p.file_type == "markdown" and p.row_count == 3, (p.file_type, p.row_count))

    p = FileHandler.parse("a.json", JSON_DOC.encode())
    check("json: 记录数/键名", p.row_count == 2 and p.columns == ["a", "b"], (p.row_count, p.columns))

    p = FileHandler.parse("a.jsonl", JSONL.encode())
    check("jsonl: 逐行解析", p.file_type == "jsonl" and p.row_count == 3, (p.file_type, p.row_count))

    p = FileHandler.parse("报表.xlsx", make_xlsx())
    check("xlsx: 多表拼接 + 首表列名", p.file_type == "xlsx" and p.columns == ["产品", "销量"] and p.row_count == 2,
          (p.file_type, p.row_count, p.columns))
    check("xlsx: 共享字符串与数字都还原", "A 型\t120" in (p.full_content or ""), repr(p.full_content))
    check("xlsx: 第二张表也在正文里", "工作表: 备注" in (p.full_content or ""), repr(p.full_content))

    p = FileHandler.parse("报告.docx", make_docx())
    check("docx: 段落抽成文本", p.file_type == "docx" and "季度总结" in (p.full_content or ""), repr(p.full_content))
    check("docx: 同一段内的多个 run 拼接", "营收同比增长 18%" in (p.full_content or ""), repr(p.full_content))
    check("docx: 行数=非空段落", p.row_count == 3, p.row_count)

    for compressed, label in ((False, "未压缩"), (True, "FlateDecode")):
        p = FileHandler.parse("doc.pdf", make_pdf(compressed))
        check(f"pdf: {label}内容流抽出文本",
              p.file_type == "pdf" and "Hello PDF World" in (p.full_content or ""), repr(p.full_content))

    # 大文件 -> 走沙箱
    big = "id,payload\n" + "\n".join(f"{i},x{'y' * 40}" for i in range(1200))
    p = FileHandler.parse("big.csv", big.encode())
    check("大文件: is_small=False 且保留 preview", p.is_small is False and p.preview and len(p.preview) <= 1800,
          (p.is_small, len(p.preview), p.char_count))

    # 编码
    p = FileHandler.parse("gbk.csv", "id,名称\n1,中文\n".encode("gb18030"))
    check("gb18030 编码自动识别", "中文" in (p.full_content or ""), repr(p.preview))

    p = FileHandler.parse("utf16.txt", "hello 世界".encode("utf-16"))
    check("UTF-16 BOM 自动识别", "世界" in (p.full_content or ""), repr(p.preview))

    # 列名去重/补空
    p = FileHandler.parse("dup.csv", b"a,a,,c\n1,2,3,4\n")
    check("列名去重补空", p.columns == ["a", "a_2", "col_3", "c"], p.columns)


# --------------------------------------------------------------------------- #
# 2. 错误路径
# --------------------------------------------------------------------------- #

def test_errors():
    expect_error("空文件 -> ValueError", lambda: FileHandler.parse("a.txt", b""), "为空")
    expect_error("不支持的扩展名", lambda: FileHandler.parse("a.zip", b"PK\x03\x04junk"), "暂不支持")
    expect_error("旧版 .doc 给出版本提示",
                 lambda: FileHandler.parse("a.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32), "另存为")
    expect_error("坏 JSON", lambda: FileHandler.parse("a.json", b"{not json"), "解析 JSON 失败")
    expect_error("坏 ZIP 的 docx", lambda: FileHandler.parse("a.docx", b"PK\x03\x04broken"), "解析 DOCX 失败")
    expect_error("坏 ZIP 的 xlsx", lambda: FileHandler.parse("a.xlsx", b"PK\x03\x04broken"), "有效的 Excel")
    expect_error("非 bytes", lambda: FileHandler.parse("a.txt", 12345), "必须是 bytes")


def test_magic_override():
    p = FileHandler.parse("mislabelled.txt", make_pdf())
    check("扩展名撒谎(.txt 实为 PDF)按魔数纠正", p.file_type == "pdf", p.file_type)

    p = FileHandler.parse("mislabelled.bin", make_xlsx())
    check("ZIP 包体识别为 xlsx", p.file_type in ("xlsx", "bin"), p.file_type)

    p = FileHandler.parse("mislabelled.txt", make_docx())
    check("ZIP 包体识别为 docx", p.file_type == "docx", p.file_type)


# --------------------------------------------------------------------------- #
# 3. 生成的沙箱脚本：编译 + 真跑
# --------------------------------------------------------------------------- #

def run_generated(file_type: str, columns, sample_name: str, sample: bytes, tmp: Path):
    src = FileHandler.generate_code_for_large_file(
        sandbox_filename=f"file_abc_{sample_name}", file_type=file_type, columns=columns
    )
    try:
        code = compile(src, f"<generated:{file_type}>", "exec")
    except SyntaxError as e:
        check(f"生成脚本可编译 [{file_type}]", False, f"{e.msg} @ line {e.lineno}: {(e.text or '').strip()}")
        return

    # 沙箱里是 /workspace/<name>，本地测试改指到临时文件
    # 注意：repl 用 lambda —— 直接把 Windows 路径塞进 re.sub 会被当成转义序列
    target = tmp / sample_name
    target.write_bytes(sample)
    patched = re.sub(r"^PATH = .*$", lambda _m: "PATH = " + repr(str(target)), src, count=1, flags=re.M)

    buf = io.StringIO()
    ns = {"__name__": "__main__"}
    try:
        with redirect_stdout(buf):
            exec(compile(patched, f"<generated:{file_type}>", "exec"), ns)
    except Exception as e:  # noqa: BLE001
        check(f"生成脚本可运行 [{file_type}]", False, f"{type(e).__name__}: {e}")
        return

    out = buf.getvalue()
    try:
        report = json.loads(out)
    except json.JSONDecodeError as e:
        check(f"生成脚本可运行 [{file_type}]", False, f"输出不是 JSON: {e} :: {out[:120]}")
        return
    check(f"生成脚本可编译并跑出 JSON [{file_type}]", isinstance(report, dict) and bool(report),
          ", ".join(sorted(report))[:80])
    return report


def test_generated(tmp: Path):
    rep = run_generated("csv", ["id", "name", "score"], "d.csv", CSV.encode(), tmp)
    check("csv 脚本: 行数与列统计", rep and rep.get("rows") == 3 and len(rep.get("column_stats", [])) == 3, rep and rep.get("rows"))

    rep = run_generated("xlsx", ["产品", "销量"], "d.xlsx", make_xlsx(), tmp)
    check("xlsx 脚本: 读出表头与行", rep and rep.get("columns") == ["产品", "销量"] and rep.get("rows") == 2,
          rep and (rep.get("columns"), rep.get("rows")))

    rep = run_generated("json", ["a", "b"], "d.json", JSON_DOC.encode(), tmp)
    check("json 脚本: records/keys", rep and rep.get("records") == 2 and rep.get("keys") == ["a", "b"], rep)

    rep = run_generated("text", [], "d.txt", TXT.encode(), tmp)
    check("text 脚本: chars/lines/top_terms", rep and rep.get("lines") == 3 and "top_terms" in rep, rep and rep.get("lines"))

    rep = run_generated("docx", [], "d.docx", make_docx(), tmp)
    check("docx 脚本: 抽出中文正文", rep and "季度总结" in json.dumps(rep, ensure_ascii=False), rep and rep.get("lines"))

    rep = run_generated("pdf", [], "d.pdf", make_pdf(True), tmp)
    check("pdf 脚本: 抽出 PDF 文本", rep and "Hello PDF World" in json.dumps(rep, ensure_ascii=False), rep and rep.get("lines"))

    # PATH 前缀必须是沙箱路径
    src = FileHandler.generate_code_for_large_file("f_1_x.csv", "csv", ["a"])
    check("生成脚本 PATH 指向 /workspace", "PATH = '/workspace/f_1_x.csv'" in src, src.splitlines()[-4:])
    check("未知 file_type 退回文本模板", "def load_text" in FileHandler.generate_code_for_large_file("x.bin", "weird", []))
    expect_error("sandbox_filename 为空", lambda: FileHandler.generate_code_for_large_file("", "csv", []), "不能为空")


# --------------------------------------------------------------------------- #
# 4. app/routes.py 的 import 契约
# --------------------------------------------------------------------------- #

def test_routes_import():
    """用桩顶掉缺失的沙箱依赖 / python-multipart，验证 routes.py 能走到并吃掉这里的两个符号。"""
    import types

    # python-multipart：FastAPI 在注册 File(...)/Form(...) 路由时会检查，
    # 当前 .venv 没装（这是后端自己的待办），这里打桩以便验证 import 契约。
    try:
        import python_multipart  # noqa: F401
    except ImportError:
        stub = types.ModuleType("python_multipart")
        # FastAPI 做的是 `from python_multipart import __version__` 再
        # `assert __version__ > "0.0.12"`，桩必须同时满足这两条。
        stub.__version__ = "0.0.20"
        sys.modules["python_multipart"] = stub

    stub_ok = True
    try:
        import deepagents_opensandbox  # noqa: F401
    except ImportError:
        mod = types.ModuleType("deepagents_opensandbox")
        mod.OpensandboxBackend = type("OpensandboxBackend", (), {})
        sys.modules["deepagents_opensandbox"] = mod

    try:
        import opensandbox.sync.sandbox  # noqa: F401
    except ImportError:
        pkg = types.ModuleType("opensandbox")
        pkg.__path__ = []  # 声明为包
        sync = types.ModuleType("opensandbox.sync")
        sandbox_mod = types.ModuleType("opensandbox.sync.sandbox")
        sandbox_mod.SandboxSync = type("SandboxSync", (), {})
        sys.modules["opensandbox"] = pkg
        sys.modules["opensandbox.sync"] = sync
        sys.modules["opensandbox.sync.sandbox"] = sandbox_mod

    try:
        import app.routes as routes  # noqa: F401
    except Exception as e:  # noqa: BLE001
        stub_ok = False
        check("app.routes 能 import（沙箱依赖打桩后）", False, f"{type(e).__name__}: {e}")

    if stub_ok:
        check("app.routes 能 import（沙箱依赖打桩后）", True)
        check("routes 里拿到的是同一个类", routes.FileHandler is FileHandler and routes.ParsedFile is ParsedFile)
        p = routes._sync_upload_file("file_test", "data.csv", CSV.encode(), "default-org")
        check("routes._sync_upload_file 全链路可用",
              p.filename == "data.csv" and p.row_count == 3 and p.analysis_type == "llm_direct" and p.code is None,
              (p.file_type, p.row_count, p.analysis_type))

        full, org = routes._prepare_message_context("看看这个文件", "file_test", "default-org")
        check("routes._prepare_message_context 注入文件正文",
              "【已上传文件内容】" in full and "alpha" in full and org == "default-org")


# --------------------------------------------------------------------------- #

def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_parsing(tmp)
        test_errors()
        test_magic_override()
        test_generated(tmp)
        test_routes_import()

    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    os.environ.setdefault("DEEPSEEK_API_KEY", "dummy")
    sys.exit(main())
