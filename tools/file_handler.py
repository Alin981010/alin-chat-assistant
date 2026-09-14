"""File handler for parsing Excel/CSV files and preparing them for analysis.

Provides smart routing: small files are parsed directly and content is
returned as text for LLM analysis; large files are uploaded to sandbox
and code templates are generated for the agent to execute.

--------------------------------------------------------------------------
``app/routes.py`` 依赖这里的 ``FileHandler`` / ``ParsedFile``，契约如下：

``FileHandler.parse(filename, content) -> ParsedFile``
    * ``filename`` —— 原始文件名（决定按什么格式解析）
    * ``content``  —— 原始字节
    * 解析失败 / 类型不支持时抛 ``ValueError``，路由层映射成 HTTP 400。

``ParsedFile``
    * ``filename`` / ``file_type`` / ``row_count`` / ``columns`` / ``preview``
      直接进 ``FileAnalysisResponse``，并写进给模型的「已上传文件信息」提示词。
    * ``full_content`` —— 抽出的完整文本；``is_small`` 为真时路由层会把它整段
      塞进提示词（LLM 直读模式）。
    * ``is_small`` —— 真=提示词直读；假=沙箱里跑 ``generate_code_for_large_file``
      生成的脚本（避免把超大文件灌进上下文）。沙箱不可用时路由层会把
      ``is_small`` 改回真、把 ``full_content`` 截断成 ``preview``，所以这两个
      字段必须是可写的普通属性。

``FileHandler.generate_code_for_large_file(sandbox_filename, file_type, columns)``
    返回一段**只依赖标准库**的 Python 脚本源码。``sandbox_filename`` 只是文件名，
    真实路径是 ``/workspace/<sandbox_filename>``（与路由层上传到沙箱的路径一致）；
    沙箱是 Python 3.12，没有 pandas / numpy / pip。

依赖策略：Excel(xlsx/xlsm) / docx / csv / json / 文本全部用标准库实现，不新增依赖
（xlsx 与 docx 本质都是 ZIP+XML）。PDF 优先用已安装的 ``pypdf`` / ``PyPDF2``，
没有时退回内置的轻量抽取器；扫描件与缺 ToUnicode 的 CID 字体会抽不到文本，此时
返回一段说明而不是抛异常，便于模型如实告知用户。
"""

from __future__ import annotations

import csv
import importlib
import io
import json
import re
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from html import unescape as _html_unescape
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

__all__ = ["FileHandler", "ParsedFile"]

# --------------------------------------------------------------------------- #
# 阈值
# --------------------------------------------------------------------------- #

#: 抽出的文本不超过这个字符数就整段注入提示词，超过则改走沙箱代码执行。
MAX_INLINE_CHARS = 12_000

#: 保留下来的 full_content 上限，防止超大文本常驻内存（路由层对超大文件本身
#: 也只会用 preview）。
MAX_FULL_CONTENT = 2_000_000

#: preview / 列名个数上限。
PREVIEW_CHARS = 1_800
PREVIEW_LINES = 20
MAX_COLUMNS = 60

#: 没有正文时给出的占位说明（避免提示词里出现空白区块）。
_EMPTY_NOTE = "（文件内容为空，或未提取到可读文本）"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class ParsedFile:
    """一次解析的结果。字段全部可写——路由层会就地修改 ``is_small`` / ``full_content``。"""

    filename: str
    file_type: str
    row_count: int = 0
    columns: List[str] = field(default_factory=list)
    preview: str = ""
    full_content: Optional[str] = None
    is_small: bool = True

    @property
    def char_count(self) -> int:
        return len(self.full_content or "")


# --------------------------------------------------------------------------- #
# 解码 / 小工具
# --------------------------------------------------------------------------- #

# 顺序即优先级：utf-8 失败再试中文常见编码，最后必须有一个不会失败的兜底。
_ENCODINGS: Tuple[str, ...] = ("utf-8-sig", "utf-8", "gb18030", "big5", "shift_jis", "cp1252", "latin-1")
_BOMS: Tuple[Tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

_DELIMITED_EXTS = {"csv", "tsv"}
_JSON_EXTS = {"json", "jsonl", "ndjson"}
_XLSX_EXTS = {"xlsx", "xlsm"}
_TEXT_EXTS = {"txt", "text", "log", "md", "markdown", "rst"}
_LEGACY_OFFICE_EXTS = {"doc", "xls", "ppt"}


def _ext(filename: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]+)$", (filename or "").strip())
    return m.group(1).lower() if m else ""


def _decode(data: bytes) -> str:
    """按 BOM → 常见编码顺序解码，永不抛异常。"""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            try:
                return (data if enc == "utf-8-sig" else data[len(bom):]).decode(enc)
            except (UnicodeDecodeError, LookupError):
                break
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _clean_columns(raw: Sequence[Any], width: Optional[int] = None) -> List[str]:
    """列名去空、去重、补位，保证是干净的 str 列表。"""
    names: List[str] = []
    for i, item in enumerate(raw):
        name = str(item).strip() if item is not None else ""
        if not name:
            name = f"col_{i + 1}"
        base, dup = name, 2
        while name in names:
            name = f"{base}_{dup}"
            dup += 1
        names.append(name)
    if width is not None and len(names) < width:
        names.extend(f"col_{i + 1}" for i in range(len(names), width))
    return names[:MAX_COLUMNS]


def _sniff_delimiter(sample: str, default: str = ",") -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except Exception:
        return default


def _rows_to_text(rows: Sequence[Sequence[Any]], delimiter: str = "\t") -> str:
    """二维数组 → 紧凑文本，供 full_content / preview 使用。"""
    lines = [delimiter.join("" if c is None else str(c) for c in row).rstrip() for row in rows]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _table_shape(rows: Sequence[Sequence[Any]]) -> Tuple[List[str], int]:
    """返回 (列名, 数据行数)；首行视作表头。"""
    if not rows:
        return [], 0
    width = max((len(r) for r in rows), default=0)
    columns = _clean_columns(rows[0], width=width) if width else []
    if not columns:
        return [], len(rows)
    return columns, max(0, len(rows) - 1)


def _finalise(
    filename: str,
    file_type: str,
    text: str,
    row_count: int,
    columns: Sequence[Any],
    preview: Optional[str] = None,
) -> ParsedFile:
    text = _normalise_newlines(text or "")
    if len(text) > MAX_FULL_CONTENT:
        text = text[:MAX_FULL_CONTENT] + "\n…（内容过长，已截断）"
    if not text.strip():
        text = _EMPTY_NOTE
    if preview is None:
        preview = text[:PREVIEW_CHARS]
    else:
        preview = _normalise_newlines(preview)[:PREVIEW_CHARS]
    return ParsedFile(
        filename=filename,
        file_type=file_type,
        row_count=max(0, int(row_count)),
        columns=_clean_columns(columns)[:MAX_COLUMNS],
        preview=preview,
        full_content=text,
        is_small=len(text) <= MAX_INLINE_CHARS,
    )


# --------------------------------------------------------------------------- #
# Excel (.xlsx / .xlsm) —— ZIP + XML，标准库即可
# --------------------------------------------------------------------------- #

_NS_SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _col_index(ref: str) -> Optional[int]:
    """"BC12" → 54（0 基）。没有 r 属性的单元格返回 None。"""
    m = re.match(r"([A-Za-z]+)", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx_open(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("不是有效的 Excel 文件（缺少 ZIP 头）。若是旧版 .xls，请先另存为 .xlsx。") from e


def _xlsx_namelist(zf: zipfile.ZipFile) -> Dict[str, str]:
    """小写路径 → 真实路径，规避大小写差异。"""
    return {n.lower(): n for n in zf.namelist()}


def _xlsx_shared_strings(zf: zipfile.ZipFile, names: Dict[str, str]) -> List[str]:
    path = names.get("xl/sharedstrings.xml")
    if not path:
        return []
    try:
        root = ET.fromstring(zf.read(path))
    except ET.ParseError:
        return []
    out: List[str] = []
    for si in root:
        out.append("".join(t.text or "" for t in si.iter(_NS_SS + "t")))
    return out


def _xlsx_sheets(zf: zipfile.ZipFile, names: Dict[str, str]) -> List[Tuple[str, str]]:
    """[(工作表名, zip 内路径)]，按工作簿声明顺序。"""
    wb_path = names.get("xl/workbook.xml")
    if not wb_path:
        raise ValueError("不是有效的 Excel 文件（缺少 xl/workbook.xml）。")

    rid_to_target: Dict[str, str] = {}
    rels_path = names.get("xl/_rels/workbook.xml.rels")
    if rels_path:
        try:
            for rel in ET.fromstring(zf.read(rels_path)):
                rid, target = rel.get("Id"), rel.get("Target")
                if rid and target:
                    rid_to_target[rid] = target
        except ET.ParseError:
            pass

    sheets: List[Tuple[str, str]] = []
    try:
        root = ET.fromstring(zf.read(wb_path))
    except ET.ParseError as e:
        raise ValueError(f"解析 Excel 工作簿失败：{e}") from e

    for sheet in root.iter(_NS_SS + "sheet"):
        name = sheet.get("name") or f"Sheet{len(sheets) + 1}"
        target = rid_to_target.get(sheet.get(_NS_DOC_REL + "id") or "", "")
        if target.startswith("/"):
            path = target.lstrip("/")
        elif target.startswith("xl/"):
            path = target
        elif target:
            path = "xl/" + target.lstrip("./")
        else:
            path = f"xl/worksheets/sheet{len(sheets) + 1}.xml"
        sheets.append((name, path))

    if not sheets:
        fallback = sorted(p for p in names.values() if p.lower().startswith("xl/worksheets/"))
        sheets = [(f"Sheet{i + 1}", p) for i, p in enumerate(fallback)]
    return sheets


def _xlsx_rows(zf: zipfile.ZipFile, path: str, names: Dict[str, str], shared: Sequence[str]) -> List[List[str]]:
    real = names.get(path.lower(), path)
    try:
        root = ET.fromstring(zf.read(real))
    except (KeyError, ET.ParseError) as e:
        raise ValueError(f"解析工作表失败：{e}") from e

    rows: List[List[str]] = []
    for row_el in root.iter(_NS_SS + "row"):
        cells: Dict[int, str] = {}
        next_idx = 0
        for c in row_el.iter(_NS_SS + "c"):
            idx = _col_index(c.get("r") or "")
            if idx is None:
                idx = next_idx
            next_idx = idx + 1
            ctype = c.get("t")
            value = ""
            if ctype == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(_NS_SS + "t"))
            else:
                v = c.find(_NS_SS + "v")
                if v is not None and v.text is not None:
                    value = v.text
                    if ctype == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            pass
                    elif ctype == "b":
                        value = "TRUE" if value == "1" else "FALSE"
                else:
                    is_el = c.find(_NS_SS + "is")
                    if is_el is not None:
                        value = "".join(t.text or "" for t in is_el.iter(_NS_SS + "t"))
            cells[idx] = value
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
        else:
            rows.append([])
    while rows and not any(str(c).strip() for c in rows[-1]):
        rows.pop()
    return rows


def _parse_xlsx(filename: str, data: bytes, file_type: str = "xlsx") -> ParsedFile:
    with _xlsx_open(data) as zf:
        names = _xlsx_namelist(zf)
        sheets = _xlsx_sheets(zf, names)
        shared = _xlsx_shared_strings(zf, names)

        blocks: List[str] = []
        first_rows: List[List[str]] = []
        for i, (sheet_name, path) in enumerate(sheets):
            try:
                rows = _xlsx_rows(zf, path, names, shared)
            except ValueError:
                continue
            if i == 0:
                first_rows = rows
            body = _rows_to_text(rows)
            blocks.append((f"# 工作表: {sheet_name}\n" + body) if len(sheets) > 1 else body)

    text = "\n\n".join(b for b in blocks if b.strip()).strip()
    columns, row_count = _table_shape(first_rows)
    preview = "\n".join(text.split("\n")[:PREVIEW_LINES])
    return _finalise(filename, file_type, text, row_count, columns, preview=preview)


# --------------------------------------------------------------------------- #
# PDF 文本抽取（无第三方依赖时的兜底实现）
# --------------------------------------------------------------------------- #

_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_TOKEN_RE = re.compile(
    r"\((?:[^()\\]|\\.)*\)"      # 字面量字符串
    r"|<[0-9A-Fa-f\s]*>"         # 十六进制字符串
    r"|\bTj\b|\bTJ\b"            # 显示文本
    r"|\bTd\b|\bTD\b|T\*|'|\""   # 换行类算子
)
# 这些流不是页面正文，参与 tokenize 只会引入噪声。
_SKIP_STREAM_MARKERS = (
    b"/Image", b"/ObjStm", b"/Metadata", b"/XRef", b"/FontFile", b"/XML",
    b"/Type1C", b"/CIDFontType0C",
)
_PDF_ESC_RE = re.compile(r"\\([nrtbf()\\]|[0-7]{1,3})")
_PDF_ESC_MAP = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "(": "(", ")": ")", "\\": "\\"}
_PDF_WS_RE = re.compile(r"[ \t\x00-\x08\x0b\x0c\x0e-\x1f]+")
_PDF_BLANK_RE = re.compile(r"\n{3,}")


def _inflate(raw: bytes) -> Optional[bytes]:
    """FlateDecode；个别 PDF 用裸 deflate，两种都试。"""
    try:
        return zlib.decompress(raw)
    except zlib.error:
        pass
    try:
        return zlib.decompressobj(-15).decompress(raw)
    except zlib.error:
        return None


def _unescape_pdf_string(s: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        g = m.group(1)
        return _PDF_ESC_MAP[g] if g in _PDF_ESC_MAP else chr(int(g, 8) & 0xFF)

    return _PDF_ESC_RE.sub(repl, s)


def _hex_pdf_string(h: str) -> str:
    digits = re.sub(r"\s+", "", h)
    if len(digits) % 2:
        digits += "0"
    try:
        raw = bytes.fromhex(digits)
    except ValueError:
        return ""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    return raw.decode("latin-1", errors="replace")


def _tokens_to_text(src: str) -> str:
    out: List[str] = []
    for m in _PDF_TOKEN_RE.finditer(src):
        tok = m.group(0)
        if tok.startswith("("):
            out.append(_unescape_pdf_string(tok[1:-1]))
        elif tok.startswith("<"):
            out.append(_hex_pdf_string(tok[1:-1]))
        else:
            out.append("\n")
    text = _PDF_WS_RE.sub(" ", "".join(out))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _PDF_BLANK_RE.sub("\n\n", text).strip()


def _pdf_text_with_pypdf(data: bytes) -> Optional[str]:
    """装了 pypdf / PyPDF2 就用它（对 CJK、压缩对象流更靠谱）。"""
    for mod_name in ("pypdf", "PyPDF2"):
        try:
            module = importlib.import_module(mod_name)
        except ImportError:
            continue
        try:
            reader = module.PdfReader(io.BytesIO(data))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception:
                    return "（该 PDF 已加密，无法提取文本。）"
            pages = [(page.extract_text() or "") for page in reader.pages]
            return _normalise_newlines("\n\n".join(p.strip() for p in pages)).strip()
        except Exception:
            continue
    return None


def _pdf_text_builtin(data: bytes) -> str:
    """内置兜底抽取器：扫描所有内容流，按文本算子拼装。"""
    chunks: List[str] = []
    for m in _STREAM_RE.finditer(data):
        head = data[max(0, m.start() - 400):m.start()]
        if any(marker in head for marker in _SKIP_STREAM_MARKERS):
            continue
        raw = m.group(1)
        if b"FlateDecode" in head:
            inflated = _inflate(raw)
            if inflated is None:
                continue
            raw = inflated
        src = raw.decode("latin-1", errors="replace")
        if "Tj" not in src and "TJ" not in src:
            continue
        chunk = _tokens_to_text(src)
        if chunk:
            chunks.append(chunk)
    return _normalise_newlines("\n\n".join(chunks)).strip()


_NO_TEXT_NOTE = (
    "（未能从该 PDF 提取到文本：可能是扫描件/图片型 PDF，或使用了需要 ToUnicode "
    "映射的 CID 字体。建议先做 OCR，或导出为 TXT/DOCX 后重新上传。）"
)


# --------------------------------------------------------------------------- #
# 其余类型
# --------------------------------------------------------------------------- #


def _parse_delimited(filename: str, data: bytes, ext: str) -> ParsedFile:
    text = _normalise_newlines(_decode(data))
    lines = text.split("\n")
    delimiter = "\t" if ext == "tsv" else _sniff_delimiter("\n".join(lines[:50]))
    try:
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    except csv.Error as e:
        raise ValueError(f"解析 {ext.upper()} 失败：{e}") from e

    columns, row_count = _table_shape(rows)
    preview = "\n".join(lines[:PREVIEW_LINES])
    return _finalise(filename, ext, text, row_count, columns, preview=preview)


def _json_columns(records: Sequence[Any]) -> List[str]:
    seen: List[str] = []
    for item in records[:200]:
        if isinstance(item, dict):
            for key in item:
                key = str(key)
                if key not in seen:
                    seen.append(key)
        if len(seen) >= MAX_COLUMNS:
            break
    return seen[:MAX_COLUMNS]


def _parse_json(filename: str, data: bytes, ext: str) -> ParsedFile:
    text = _normalise_newlines(_decode(data)).strip()
    file_type = "jsonl" if ext in ("jsonl", "ndjson") else "json"
    if not text:
        return _finalise(filename, file_type, "", 0, [])

    if ext in ("jsonl", "ndjson"):
        try:
            payload: Any = [json.loads(line) for line in text.split("\n") if line.strip()]
        except json.JSONDecodeError as e:
            raise ValueError(f"解析 {ext.upper()} 失败：{e}") from e
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # 有些 .json 实际是逐行 JSON，再给一次机会
            try:
                payload = [json.loads(line) for line in text.split("\n") if line.strip()]
            except json.JSONDecodeError as e:
                raise ValueError(f"解析 JSON 失败：{e}") from e

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = [payload]
    else:
        return _finalise(filename, file_type, text, 1, [])

    preview = "\n".join(text.split("\n")[:PREVIEW_LINES])
    return _finalise(filename, file_type, text, len(records), _json_columns(records), preview=preview)


def _parse_text(filename: str, data: bytes, file_type: str) -> ParsedFile:
    text = _normalise_newlines(_decode(data))
    lines = text.split("\n")
    preview = "\n".join(lines[:PREVIEW_LINES])
    return _finalise(filename, file_type, text, sum(1 for line in lines if line.strip()), [], preview=preview)


def _parse_docx(filename: str, data: bytes) -> ParsedFile:
    if not data.startswith(b"PK"):
        raise ValueError("不是有效的 .docx 文件（缺少 ZIP 头）。若是旧版 .doc，请先另存为 .docx。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = _xlsx_namelist(zf)
            path = names.get("word/document.xml")
            if not path:
                raise ValueError("不是有效的 .docx 文件（缺少 word/document.xml）。")
            xml = zf.read(path).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as e:
        raise ValueError(f"解析 DOCX 失败：{e}") from e

    xml = re.sub(r"<w:tab\b[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br\b[^>]*/>", "\n", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    text = _normalise_newlines(_html_unescape(re.sub(r"<[^>]+>", "", xml)))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.split("\n"))).strip()
    preview = "\n".join(text.split("\n")[:PREVIEW_LINES])
    return _finalise(filename, "docx", text, sum(1 for line in text.split("\n") if line.strip()), [], preview=preview)


def _parse_pdf(filename: str, data: bytes) -> ParsedFile:
    text = _pdf_text_with_pypdf(data)
    if text is None:
        text = _pdf_text_builtin(data)
    if not text.strip():
        text = _NO_TEXT_NOTE
    preview = "\n".join(text.split("\n")[:PREVIEW_LINES])
    return _finalise(filename, "pdf", text, sum(1 for line in text.split("\n") if line.strip()), [], preview=preview)


# --------------------------------------------------------------------------- #
# 沙箱分析脚本模板
# 全部用 raw 字符串书写 —— 模板内容即生成结果，避免二次转义。
# 拼装顺序：PRELUDE + [NUM_HELPER] + <LOADER> + <SUMMARY + MAIN>
# --------------------------------------------------------------------------- #

_GEN_PRELUDE = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由 MINI-RAG 自动生成的文件分析脚本 —— 在沙箱里运行。

目标文件 : @@PATH@@
文件类型 : @@TYPE@@
已知列名 : @@COLUMNS@@

沙箱是 Python 3.12，**没有 pandas / numpy / pip**，所以这里只用标准库
（xlsx / docx 本质是 ZIP+XML，一样能解析）。

    python3 /workspace/analyze.py

要回答用户的具体问题，请在 main() 末尾追加自己的统计逻辑后重新运行；
不要打印整份文件，只打印聚合结果。
"""
import csv
import json
import re
import statistics
from collections import Counter
from html import unescape as _html_unescape

PATH = @@PATH_REPR@@
COLUMNS = @@COLUMNS_REPR@@
'''

_GEN_NUM_HELPER = r'''

def _as_number(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
'''

_GEN_CSV_LOADER = r'''

def _sniff(sample, default=","):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except Exception:
        return default


def load_rows():
    with open(PATH, "r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        reader = csv.reader(f, delimiter=_sniff(sample))
        return [r for r in reader if any(c.strip() for c in r)]
'''

_GEN_XLSX_LOADER = r'''
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref):
    m = re.match(r"([A-Za-z]+)", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def load_rows():
    import zipfile
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(PATH) as zf:
        names = {n.lower(): n for n in zf.namelist()}
        shared = []
        ss = names.get("xl/sharedstrings.xml")
        if ss:
            root = ET.fromstring(zf.read(ss))
            for si in root:
                shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
        sheet = names.get("xl/worksheets/sheet1.xml")
        if not sheet:
            candidates = sorted(p for p in names.values() if p.lower().startswith("xl/worksheets/"))
            if not candidates:
                raise SystemExit("找不到工作表")
            sheet = candidates[0]
        root = ET.fromstring(zf.read(sheet))

    rows = []
    for row_el in root.iter(NS + "row"):
        cells = {}
        nxt = 0
        for c in row_el.iter(NS + "c"):
            idx = _col_index(c.get("r") or "")
            if idx is None:
                idx = nxt
            nxt = idx + 1
            ctype = c.get("t")
            value = ""
            if ctype == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(NS + "t"))
            else:
                v = c.find(NS + "v")
                if v is not None and v.text is not None:
                    value = v.text
                    if ctype == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            pass
                    elif ctype == "b":
                        value = "TRUE" if value == "1" else "FALSE"
                else:
                    is_el = c.find(NS + "is")
                    if is_el is not None:
                        value = "".join(t.text or "" for t in is_el.iter(NS + "t"))
            cells[idx] = value
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows
'''

_GEN_ROW_SUMMARY = r'''

def summarize(rows):
    if not rows:
        return {"rows": 0, "columns": [], "note": "文件为空"}
    width = max(len(r) for r in rows)
    header = list(rows[0]) + ["col_%d" % i for i in range(len(rows[0]), width)]
    data = rows[1:]
    stats = []
    for i, name in enumerate(header):
        values = [r[i] for r in data if i < len(r) and str(r[i]).strip()]
        if not values:
            continue
        numbers = [n for n in (_as_number(v) for v in values) if n is not None]
        entry = {"column": name, "non_empty": len(values), "unique": len(set(values))}
        if numbers and len(numbers) >= max(1, int(0.8 * len(values))):
            entry["mean"] = round(statistics.fmean(numbers), 4)
            entry["min"] = min(numbers)
            entry["max"] = max(numbers)
            if len(numbers) > 1:
                entry["stdev"] = round(statistics.stdev(numbers), 4)
        else:
            entry["top"] = Counter(str(v) for v in values).most_common(5)
        stats.append(entry)
    return {"rows": len(data), "columns": header, "column_stats": stats, "head": rows[:6]}


def main():
    rows = load_rows()
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2, default=str))
    # TODO: 在这里追加针对用户问题的统计逻辑


if __name__ == "__main__":
    main()
'''

_GEN_JSON_BODY = r'''

def load_records():
    with open(PATH, "r", encoding="utf-8", errors="replace") as f:
        text = f.read().strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(payload, dict):
        payload = [payload]
    return payload


def summarize(records):
    keys = []
    for item in records[:200]:
        if isinstance(item, dict):
            for k in item:
                if str(k) not in keys:
                    keys.append(str(k))
    stats = []
    for key in keys:
        values = [r[key] for r in records if isinstance(r, dict) and r.get(key) is not None]
        numbers = [n for n in (_as_number(v) for v in values) if n is not None]
        entry = {"key": key, "present": len(values), "unique": len({str(v) for v in values})}
        if numbers and not isinstance(values[0], str):
            entry["mean"] = round(statistics.fmean(numbers), 4)
            entry["min"] = min(numbers)
            entry["max"] = max(numbers)
        else:
            entry["top"] = Counter(str(v) for v in values).most_common(5)
        stats.append(entry)
    return {
        "records": len(records),
        "keys": keys,
        "field_stats": stats,
        "sample": json.dumps(records[:3], ensure_ascii=False, default=str),
    }


def main():
    records = load_records()
    print(json.dumps(summarize(records), ensure_ascii=False, indent=2, default=str))
    # TODO: 在这里追加针对用户问题的统计逻辑


if __name__ == "__main__":
    main()
'''

_GEN_TEXT_LOADER = r'''

def load_text():
    with open(PATH, "r", encoding="utf-8", errors="replace") as f:
        return f.read()
'''

_GEN_DOCX_LOADER = r'''

def load_text():
    import zipfile

    with zipfile.ZipFile(PATH) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"<w:tab\b[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br\b[^>]*/>", "\n", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    return _html_unescape(re.sub(r"<[^>]+>", "", xml))
'''

_GEN_PDF_LOADER = r'''

def load_text():
    import zlib

    with open(PATH, "rb") as f:
        data = f.read()

    token_re = re.compile(
        r"\((?:[^()\\]|\\.)*\)"
        r"|<[0-9A-Fa-f\s]*>"
        r"|\bTj\b|\bTJ\b|\bTd\b|\bTD\b|T\*|'|\""
    )
    skip = (b"/Image", b"/ObjStm", b"/Metadata", b"/XRef", b"/FontFile", b"/XML")
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        head = data[max(0, m.start() - 400):m.start()]
        if any(marker in head for marker in skip):
            continue
        raw = m.group(1)
        if b"FlateDecode" in head:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                try:
                    raw = zlib.decompressobj(-15).decompress(raw)
                except zlib.error:
                    continue
        src = raw.decode("latin-1", errors="replace")
        if "Tj" not in src and "TJ" not in src:
            continue
        parts = []
        for tok in token_re.finditer(src):
            value = tok.group(0)
            if value.startswith("("):
                body = value[1:-1]
                body = re.sub(
                    r"\\([nrtbf()\\])",
                    lambda mm: {"n": "\n", "r": "\r", "t": "\t", "b": "\b",
                                "f": "\f", "(": "(", ")": ")", "\\": "\\"}[mm.group(1)],
                    body,
                )
                parts.append(re.sub(r"\\([0-7]{1,3})", lambda mm: chr(int(mm.group(1), 8) & 0xFF), body))
            elif value.startswith("<"):
                digits = re.sub(r"[^0-9A-Fa-f]", "", value)
                if len(digits) % 2:
                    digits += "0"
                parts.append(bytes.fromhex(digits).decode("latin-1", errors="replace"))
            else:
                parts.append("\n")
        chunk = "".join(parts)
        if chunk.strip():
            chunks.append(chunk)
    return "\n\n".join(chunks)
'''

_GEN_TEXT_SUMMARY = r'''

_STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "you", "your", "can", "will", "one",
}


def summarize(text):
    lines = text.splitlines()
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}", text.lower())
    meaningful = [w for w in words if w not in _STOPWORDS]
    return {
        "chars": len(text),
        "lines": len(lines),
        "non_empty_lines": sum(1 for line in lines if line.strip()),
        "words": len(words),
        "top_terms": Counter(meaningful).most_common(25),
        "head": "\n".join(lines[:40]),
    }


def main():
    text = load_text()
    print(json.dumps(summarize(text), ensure_ascii=False, indent=2, default=str))
    # TODO: 在这里追加针对用户问题的检索/统计逻辑


if __name__ == "__main__":
    main()
'''

_TABULAR_TYPES = {"csv", "tsv"}
_XLSX_GEN_TYPES = {"xlsx", "xlsm", "excel"}
_DOCX_GEN_TYPES = {"docx"}
_PDF_GEN_TYPES = {"pdf"}


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #


class FileHandler:
    """上传文件解析入口。所有方法都是无状态的。"""

    #: 允许上传的扩展名（路由层不校验，前端按这个列表过滤）。
    SUPPORTED_EXTENSIONS: Tuple[str, ...] = (
        "pdf", "docx", "xlsx", "xlsm",
        "md", "markdown", "txt", "text", "log",
        "csv", "tsv", "json", "jsonl", "ndjson",
    )

    # ------------------------------------------------------------------ #
    @classmethod
    def parse(cls, filename: str, content: bytes) -> ParsedFile:
        """按扩展名把上传的字节解析成 :class:`ParsedFile`。

        参数
        ----
        filename:
            原始文件名，只取扩展名做分派。
        content:
            原始字节。

        异常
        ----
        ValueError:
            内容为空、类型不支持、或文件损坏。路由层映射为 HTTP 400。
        """
        if content is None:
            raise ValueError("文件内容为空。")
        if isinstance(content, str):
            content = content.encode("utf-8", errors="replace")
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise ValueError("文件内容必须是 bytes。")
        data = bytes(content)
        if not data:
            raise ValueError("文件内容为空。")

        name = (filename or "unnamed").strip() or "unnamed"
        ext = _ext(name)

        # 扩展名不可信时按魔数纠正（PDF / OOXML / 旧版 OLE）
        if data[:4] == b"%PDF":
            ext = "pdf"
        elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError(
                f"暂不支持旧版 Office 二进制格式（.{_ext(name) or 'doc/xls/ppt'}）。"
                "请先在 Office/WPS 里另存为 .docx / .xlsx 后重新上传。"
            )
        elif data[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    inside = {n.lower() for n in zf.namelist()}
                if "word/document.xml" in inside:
                    ext = "docx"
                elif any(n.startswith("xl/") for n in inside):
                    ext = "xlsx" if ext not in _XLSX_EXTS else ext
            except zipfile.BadZipFile:
                pass

        if ext in _DELIMITED_EXTS:
            return _parse_delimited(name, data, ext)
        if ext in _XLSX_EXTS:
            return _parse_xlsx(name, data, ext)
        if ext in _JSON_EXTS:
            return _parse_json(name, data, ext)
        if ext == "pdf":
            return _parse_pdf(name, data)
        if ext == "docx":
            return _parse_docx(name, data)
        if ext in _LEGACY_OFFICE_EXTS:
            raise ValueError(
                f"暂不支持旧版 Office 二进制格式（.{ext}）。请先另存为 .docx / .xlsx 后重新上传。"
            )
        if ext in _TEXT_EXTS:
            file_type = "markdown" if ext in ("md", "markdown") else "text"
            return _parse_text(name, data, file_type)

        supported = " / ".join(sorted(set(cls.SUPPORTED_EXTENSIONS)))
        raise ValueError(f"暂不支持的文件类型：.{ext or '未知'}。当前支持：{supported}。")

    # ------------------------------------------------------------------ #
    @classmethod
    def generate_code_for_large_file(
        cls,
        sandbox_filename: str,
        file_type: str,
        columns: Optional[Sequence[Any]] = None,
    ) -> str:
        """为「大文件走沙箱」生成分析脚本源码（仅标准库）。

        ``sandbox_filename`` 是文件名（不是路径），脚本内部会拼成
        ``/workspace/<name>``，与 ``app/routes.py`` 上传到沙箱的位置一致。
        """
        name = str(sandbox_filename or "").strip().lstrip("/")
        if not name:
            raise ValueError("sandbox_filename 不能为空。")
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        path = "/workspace/" + name

        kind = str(file_type or "").strip().lower()
        cols = [str(c) for c in (columns or [])][:MAX_COLUMNS]

        prelude = (
            _GEN_PRELUDE
            .replace("@@PATH@@", path)
            .replace("@@TYPE@@", kind or "unknown")
            .replace("@@COLUMNS@@", ", ".join(cols) if cols else "（未识别）")
            .replace("@@PATH_REPR@@", repr(path))
            .replace("@@COLUMNS_REPR@@", repr(cols))
        )

        if kind in _TABULAR_TYPES:
            return prelude + _GEN_NUM_HELPER + _GEN_CSV_LOADER + _GEN_ROW_SUMMARY
        if kind in _XLSX_GEN_TYPES:
            return prelude + _GEN_NUM_HELPER + _GEN_XLSX_LOADER + _GEN_ROW_SUMMARY
        if kind in _JSON_EXTS:
            return prelude + _GEN_NUM_HELPER + _GEN_JSON_BODY
        if kind in _DOCX_GEN_TYPES:
            return prelude + _GEN_DOCX_LOADER + _GEN_TEXT_SUMMARY
        if kind in _PDF_GEN_TYPES:
            return prelude + _GEN_PDF_LOADER + _GEN_TEXT_SUMMARY
        return prelude + _GEN_TEXT_LOADER + _GEN_TEXT_SUMMARY


if __name__ == "__main__":  # pragma: no cover - 手动调试：python tools/file_handler.py <文件>
    import sys

    if len(sys.argv) >= 2:
        target = sys.argv[1]
        with open(target, "rb") as fh:
            parsed = FileHandler.parse(target.replace("\\", "/").rsplit("/", 1)[-1], fh.read())
        print(f"file_type={parsed.file_type} rows={parsed.row_count} cols={parsed.columns}")
        print(f"is_small={parsed.is_small} chars={parsed.char_count}")
        print("-" * 60)
        print(parsed.preview)
