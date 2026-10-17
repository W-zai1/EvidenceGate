"""检查并清除文件中部的 BOM（U+FEFF）。

一个孤立的 U+FEFF 出现在 CSS 里时，CSS 词法会把它当成一个**类型选择器**
（\ufeff 是合法标识符字符），于是紧随其后的那条规则的选择器变成
`\ufeff .auth-gate`——"名为 U+FEFF 的元素的后代"。这种元素不存在，
整条规则静默失效，而语法检查、brace 计数、node --check 全都看不出来。

文件**开头**的 BOM 是另一回事（浏览器会剥掉），可以保留但没必要。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TARGETS = ["viewer/styles.css", "viewer/app.js", "viewer/api.js",
           "viewer/index.html", "README.md"]

report = []
for rel in TARGETS:
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        continue
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if "\ufeff" not in text:
        continue
    positions = [i for i, ch in enumerate(text) if ch == "\ufeff"]
    for i in positions:
        line = text.count("\n", 0, i) + 1
        at_start = (i == 0)
        tail = text[i + 1:i + 60].replace("\n", "<NL>")
        report.append((rel, line, at_start, tail))

if not report:
    print("未发现 BOM ✓")
else:
    print("发现 BOM：")
    for rel, line, at_start, tail in report:
        print("  %s:%d %s  后接=%r"
              % (rel, line, "（文件开头）" if at_start else "（文件中途）", tail))

# 任何 BOM 都算失败。
# 开头 BOM 对浏览器无害，但会打断别的工具：`ast.parse` 读进来直接
# `invalid non-printable character U+FEFF`，`check_no_deps.py` 就是这样炸的。
# 既然所有文件都在我们自己手里，统一不带 BOM 是最省心的约定。
sys.exit(0 if not report else 1)
