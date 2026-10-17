"""检测（并可选修复）被 GBK 误解码造成的乱码。

原理：如果一段 UTF-8 中文被按 GBK 解读、再按 UTF-8 存回，
那么把它按 GBK 编码回去、再按 UTF-8 解码，就能还原原文。
这个往返是可验证的：还原结果里应当出现正常的中文常用字。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ["viewer/app.js", "viewer/api.js", "viewer/index.html",
           "viewer/styles.css", "engine/api.py", "engine/rubric.py",
           "engine/store.py", "engine/agents.py", "engine/jobs_runner.py",
           "README.md"]

APPLY = "--apply" in sys.argv


def try_repair_line(line):
    """返回修复后的行；无法可靠修复时返回 None。"""
    if not any(ord(c) > 127 for c in line):
        return None
    try:
        back = line.encode("gbk")
    except UnicodeEncodeError:
        return None
    try:
        fixed = back.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if fixed == line:
        return None
    # 只在"修复后是合法中文/正常文本"时才接受
    if not any("\u4e00" <= c <= "\u9fff" for c in fixed):
        return None
    return fixed


total = 0
for rel in TARGETS:
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        continue
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    hits = []
    for i, line in enumerate(lines, 1):
        fixed = try_repair_line(line)
        if fixed is not None:
            hits.append((i, line.strip()[:60], fixed.strip()[:60]))
    if not hits:
        continue
    total += len(hits)
    print("%s：%d 行疑似乱码" % (rel, len(hits)))
    for i, before, after in hits[:6]:
        print("   L%-5d %s" % (i, before))
        print("      →   %s" % after)
    if len(hits) > 6:
        print("   …另有 %d 行" % (len(hits) - 6))
    if APPLY:
        for i, _, _ in hits:
            lines[i - 1] = try_repair_line(lines[i - 1]) or lines[i - 1]
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines))
        print("   已修复并写回")

if not total:
    print("未发现 GBK 误解码造成的乱码 ✓")
sys.exit(0 if not total else 1)
