#!/usr/bin/env python
"""把深色 styles.css 机械地映射成浅色版本（一次性构建脚本）。

为什么用脚本而不是重写
----------------------
viewer/styles.css 有 1700 行、200 多个硬编码颜色，是两版迭代堆出来的。
手抄一份浅色主题，必然会漏掉某些弹窗/状态色——而漏掉的地方在浅色下
往往是「白底白字」，比难看严重得多。

这里按 HSL 做一次**有规则的**映射：
  · 深色背景（L 低）→ 近白纸面（保留一点色相，避免死白）
  · 浅色文字（L 高、S 低）→ 深墨色
  · 高饱和强调色（金/蓝/红/绿）→ 保留色相、压到中等明度（白底上可读）
  · 近黑且带透明度 → 阴影（否则会变成白色投影）
然后手写一段覆盖层（文件末尾）修正脚本判断不了的少数位置。

原始深色主题完整保存在 viewer/styles-dark.css，由前端主题开关切回。
"""
import colorsys
import   pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "viewer" / "styles-dark.css"   # 深色原稿（唯一真源）
DST = ROOT / "viewer" / "styles.css"        # 生成的浅色主题

HEX = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
RGB = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)")

INK = (0x14 / 255, 0x1A / 255, 0x22 / 255)


def _hex_out(r, g, b):
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _rgba_out(r, g, b, a):
    return "rgba(%d,%d,%d,%.2f)" % (round(r * 255), round(g * 255), round(b * 255), a)


def convert(r, g, b, a):
    h, l, s = colorsys.rgb_to_hls(r, g, b)

    # 纯黑/近黑 + 半透明 = 阴影/遮罩，不能翻成白色
    if max(r, g, b) <= 0.05 and a < 0.95:
        return _rgba_out(0x0F / 255, 0x17 / 255, 0x2A / 255, min(0.5, a * 0.55))

    if max(r, g, b) <= 0.05:                       # 纯黑文字
        return _hex_out(*INK)

    if s >= 0.45 and l >= 0.35:                    # 强调色
        nl = min(max(0.34 + (l - 0.6) * 0.16, 0.32), 0.50)
        ns = min(1.0, s * 0.95)
        r2, g2, b2 = colorsys.hls_to_rgb(h, nl, ns)
        return _hex_out(r2, g2, b2) if a >= 0.999 else _rgba_out(r2, g2, b2, a)

    if l <= 0.30:                                   # 深色底
        nl = 0.965 - l * 0.12
        ns = min(s * 0.12, 0.10)
        na = a if a >= 0.95 else min(1.0, a + 0.30)
        r2, g2, b2 = colorsys.hls_to_rgb(h, nl, ns)
        return _hex_out(r2, g2, b2) if a >= 0.999 and na >= 0.999 else _rgba_out(r2, g2, b2, na)

    if l >= 0.80:                                   # 亮文字
        nl = 0.10 + (1 - l) * 0.30
        ns = min(s, 0.14)
    elif l >= 0.62:                                 # 次级亮文字
        nl = 0.18 + (1 - l) * 0.55
        ns = min(s, 0.18)
    else:                                           # 中间调
        nl = 0.26 + (l - 0.30) * 0.45
        ns = min(s, 0.22)

    r2, g2, b2 = colorsys.hls_to_rgb(h, nl, min(ns, 1.0))
    return _hex_out(r2, g2, b2) if a >= 0.999 else _rgba_out(r2, g2, b2, a)


def sub_hex(m):
    v = m.group(1)
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    r, g, b = (int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return convert(r, g, b, 1.0)


def sub_rgb(m):
    r, g, b = (float(m.group(i)) / 255 for i in (1, 2, 3))
    a = 1.0 if m.group(4) is None else float(m.group(4))
    return convert(r, g, b, a)


def main():
    text = SRC.read_text(encoding="utf-8")
    if DST.exists() and "浅色主题 · 手工修正层" in DST.read_text(encoding="utf-8"):
        # 生成会覆盖手工修正层。修正层是逐屏调出来的，不能靠重跑脚本找回来。
        print("拒绝执行：%s 已有手工修正层。先备份/移除它，再重跑生成。" % DST.name)
        return 2
    out = RGB.sub(sub_rgb, HEX.sub(sub_hex, text))
    DST.write_text(out, encoding="utf-8")
    print("已生成浅色 styles.css：%d 字符（原 %d）" % (len(out), len(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
