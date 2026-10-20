"""CI 守卫：engine/ 必须只用 Python 标准库。

这个项目"零依赖"是一个刻意的约束——HR 的电脑上装不了东西。
但它很容易在某次提交里悄悄破功（"就加个 requests 吧"），
所以用 AST 扫一遍，把第三方 import 变成硬失败。

engine/ 内部模块互相以平铺模块名导入（import db / from engine.guardrails.audit import ...），
所以本地模块名必须算白名单，否则会把所有同目录导入误报成第三方依赖。
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
files = sorted((ROOT / "engine").glob("*.py"))
local = {p.stem for p in files} | {"engine", "skills"}
allowed = set(sys.stdlib_module_names) | local

bad = []
for p in files:
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        bad.append((p.name, "语法错误：%s" % e))
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in allowed:
                    bad.append((p.name, a.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".")[0]
            if root not in allowed:
                bad.append((p.name, node.module))

if bad:
    print("发现第三方依赖（引擎必须只用标准库）：")
    for f, m in bad:
        print("  %s: %s" % (f, m))
    sys.exit(1)

print("engine/ 仅使用标准库 ✓  （扫描 %d 个文件，本地模块 %d 个）" % (len(files), len(local)))
