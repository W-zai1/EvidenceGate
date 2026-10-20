"""resume-parse · 简历解析。

设计要点
--------
PDF / DOCX 的解析统一走 engine/docparse.py（纯标准库实现）。
这里不再返回"（简历 PDF 解析占位：未安装 pypdf…）"这类占位字符串——
那是把这个提示当成**简历正文**喂给评委，等于凭空造了一份简历。

解析不出来就抛错，让 HR 看到"这份 PDF 抽不出文字，请换格式"，
而不是让它悄悄变成一段评审依据。
"""
import os
import   sys

# 允许从 engine/ 导入 docparse（本文件位于 skills/resume-parse/）
_ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "..", "engine"))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)


class ResumeParseError(RuntimeError):
    """简历无法解析成可用文本。"""


def parse_resume(path: str) -> str:
    """返回简历纯文本；解析失败抛 ResumeParseError（绝不返回占位串）。"""
    if not path or not os.path.isfile(path):
        raise ResumeParseError("简历文件不存在：%s" % path)

    try:
        from docparse import parse_bytes, ParseError
    except ImportError as e:
        raise ResumeParseError("无法加载 docparse 模块：%s" % e)

    with open(path, "rb") as f:
        data = f.read()

    try:
        r = parse_bytes(os.path.basename(path), data)
    except ParseError as e:
        raise ResumeParseError("简历解析失败（%s）：%s" % (os.path.basename(path), e))

    text = (r.get("text") or "").strip()
    if not text:
        warn = "；".join(r.get("warnings") or []) or "抽不出文字"
        raise ResumeParseError("简历解析不出文字（%s）：%s" % (os.path.basename(path), warn))
    return text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python parse.py <resume 文件>")
        sys.exit(2)
    try:
        print(parse_resume(sys.argv[1]))
    except ResumeParseError as e:
        print("[error] %s" % e, file=sys.stderr)
        sys.exit(1)
