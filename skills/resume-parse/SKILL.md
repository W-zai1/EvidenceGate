---
name: resume-parse
description: 简历解析 skill —— 把 PDF/Word/DOCX 简历转为纯文本，供 engine/ingest.py 装配候选人上下文。
type: skill
entry: parse.py
---

# resume-parse · 简历解析

将候选人简历文件（PDF / Word / DOCX / TXT / MD）解析为纯文本，作为多 Agent 评判引擎的
「简历」上下文来源。

## 接口约定
`parse.py` 暴露函数：

```python
def parse_resume(path: str) -> str:
    """返回简历纯文本。"""
```

- `ingest.py` 在扫描到 `*.resume.pdf` 时调用本函数。
- 其他格式（.txt/.md）由 ingest 直接读取，不经过本 skill。

## 引擎模式（占位 → 真实）
- `mock`：当前占位实现，直接读取文本类文件；PDF 尝试用 `pdftotext`/PyPDF，缺失依赖时返回占位提示。
- 真实接入：安装 `pypdf` 或 `python-docx` 后，real 分支解析 PDF/DOCX。

> 这是输入管道 skill，与 `asr-transcribe` 并列，使 engine 的「简历/测评/转写」三类来源都有真实装配链路，
> 而非手写塞入字段。
