"""扫描"形近字符"——只查标识符位置，不查正文。

背景事故：`.auth-gate` 这条 CSS 规则在样式表里存在、declaration 也齐全，
但浏览器根本不匹配它。原因不在选择器文字，而在它前面有个孤立的 U+FEFF
（已由 check_bom.py 负责）。同类风险还包括 U+2010 假连字符、全角字母等：
它们在正文里完全正常，一旦进了选择器/类名/id 就会让整块代码静默失效。

因此本检查**只看标识符位置**：
  · CSS：每条规则选择器部分（`{` 之前）
  · HTML：class / id 属性值
  · JS：传给 $() / getElementById / querySelector / closest 的字符串，
        以及 data-* 选择器字符串
正文（中文说明、破折号、引号）一律不查，避免假阳性。
"""
import os
import re
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 在标识符里不该出现的字符：形近符号 + 零宽 + 全角
BAD_IN_IDENT = {
    0x2010: "HYPHEN（假连字符，应为 ASCII '-'）",
    0x2011: "NON-BREAKING HYPHEN",
    0x2012: "FIGURE DASH",
    0x2013: "EN DASH",
    0x2014: "EM DASH",
    0x2212: "MINUS SIGN",
    0x00A0: "NBSP",
    0x200B: "ZWSP",
    0x200C: "ZWNJ",
    0x200D: "ZWJ",
    0xFEFF: "BOM",
    0x3000: "IDEOGRAPHIC SPACE（全角空格）",
    0x0430: "CYRILLIC a",
    0x0435: "CYRILLIC e",
    0x043E: "CYRILLIC o",
    0x0441: "CYRILLIC c",
    0x0440: "CYRILLIC p",
    0x0445: "CYRILLIC x",
    0x0455: "CYRILLIC s",
}

problems = []


def scan(rel, label, text, line_of):
    for idx, ch in enumerate(text):
        cp = ord(ch)
        if cp < 128:
            continue
        # 全角字母/数字混进标识符同样致命
        if 0xFF01 <= cp <= 0xFF5E:
            problems.append((rel, line_of(idx), ch, "全角字符（U+%04X）" % cp))
            continue
        if cp in BAD_IN_IDENT:
            problems.append((rel, line_of(idx), ch, BAD_IN_IDENT[cp]))
            continue
        if ch.isspace() and cp != 0x20:
            problems.append((rel, line_of(idx), ch,
                             "非 ASCII 空白（U+%04X）" % cp))


def make_lineof(text):
    def f(idx):
        return text.count("\n", 0, idx) + 1
    return f


# ---- CSS：只看选择器 ----
css_path = os.path.join(ROOT, "viewer", "styles.css")
if os.path.exists(css_path):
    text = open(css_path, encoding="utf-8").read()
    line_of = make_lineof(text)
    # 去掉注释后逐条取 `{` 之前的部分
    stripped = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), text, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{", stripped):
        scan("viewer/styles.css", "selector", m.group(1), line_of)

# ---- HTML：只看 class / id ----
html_path = os.path.join(ROOT, "viewer", "index.html")
if os.path.exists(html_path):
    text = open(html_path, encoding="utf-8").read()
    line_of = make_lineof(text)
    for m in re.finditer(r'\b(?:class|id)="([^"]*)"', text):
        scan("viewer/index.html", "class/id", m.group(1), line_of)

# ---- JS：只看选择器/元素 id 字符串 ----
for name in ("viewer/app.js", "viewer/api.js"):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        continue
    text = open(p, encoding="utf-8").read()
    line_of = make_lineof(text)
    pats = [
        r'\$\(\s*"([^"]*)"\s*\)',
        r'\$\(\s*\'([^\']*)\'\s*\)',
        r'getElementById\(\s*"([^"]*)"',
        r'querySelector(?:All)?\(\s*[`"\']([^`"\']*)',
        r'closest\(\s*"([^"]*)"',
        r'data-[\w-]*\[\s*"([^"]*)"',
        r'\[data-[\w-]+="([^"]*)"\]',
    ]
    for pat in pats:
        for m in re.finditer(pat, text):
            scan(name, "selector", m.group(1), line_of)

if problems:
    print("标识符位置发现可疑字符：")
    for rel, line, ch, why in problems:
        print("  %s:%d  %r  (%s)" % (rel, line, ch, why))
    sys.exit(1)

print("标识符位置未发现形近字符 ✓")
sys.exit(0)
