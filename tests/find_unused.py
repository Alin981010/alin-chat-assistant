"""临时：用 AST 粗略找出未使用的 import / 未引用的模块级名字。"""
import ast
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
targets = sorted(list((root / "app").glob("*.py")) + list((root / "tools").glob("*.py")) + [root / "main.py"])

for path in targets:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, str(path))
    imported = {}  # name -> lineno
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[(a.asname or a.name.split(".")[0])] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                imported[(a.asname or a.name)] = node.lineno

    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            cur = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                used.add(cur.id)
    # 字符串注解 / __all__ 里出现也算用到
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for name in list(imported):
                if name in node.value:
                    used.add(name)
    unused = [(n, ln) for n, ln in imported.items() if n not in used]
    rel = path.relative_to(root)
    if unused:
        print(f"{rel}:")
        for n, ln in sorted(unused, key=lambda x: x[1]):
            print(f"    L{ln:<4} {n}")
print("--- 扫描完成 ---")
