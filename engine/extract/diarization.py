"""说话人归属（extract 层）。

把"一段段带/不带说话人前缀的文本"变成"每段都有 speaker 判定"的结构化结果。
领域无关：interviewer/candidate 只是一对二元标签，换成"提问方/答辩方"也成立。

三层归属（历史教训凝练）：
- 层 0（前缀）：原文自带「面试官：」「候选人：」等前缀，确定性，置信 1.0。
- 层 1（问题对齐）：段文本命中问题清单，提问方基本可定。
- 层 2（LLM 言语行为）：对无信号的段调用 LLM 判定提问/自述。

人工修正优先级最高：source=manual 的段任何层都不许翻案。engine 段重跑时重置重推。
"""
import re

_PREFIX_RE = re.compile(r"^\s*(面试官|候选人|提问方|答辩方|访问者|受访者)[A-Za-z]?\s*[:：]\s*(.*)$")
_SENT_SPLIT = re.compile(r"[。！？!?；;\n]")


def similarity(a, b):
    """字符级 Jaccard，0~1。空串返回 0。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def segments_from_text(text, max_chars=80):
    """把逐字稿文本切成段。带前缀的行解析出 speaker（层 0）。

    返回：[{"idx","start","end","text","speaker","confidence","reason",
           "asker","source"}]
    - 前缀行：speaker 取自前缀，confidence=1.0，source="prefix"；
    - 普通行：speaker="unassigned"，source="engine"（待归属）；
    - 空行丢弃；超长无标点行按句末标点再切、仍无标点则整段保留。
    """
    out = []
    idx = 0
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _PREFIX_RE.match(line)
        if m:
            speaker = _norm_role(m.group(1))
            body = m.group(2).strip() or line.strip()
            out.append(_seg(idx, idx, idx + 1, body, speaker, 1.0,
                            "原文自带说话人前缀", "", "prefix"))
            idx += 1
            continue
        for part in _split_long(line.strip(), max_chars):
            out.append(_seg(idx, idx, idx + 1, part, "unassigned", 0.0,
                            "", "", "engine"))
            idx += 1
    return out


def _split_long(line, max_chars):
    if len(line) <= max_chars or not _SENT_SPLIT.search(line):
        return [line]
    parts, buf = [], ""
    for ch in line:
        buf += ch
        if _SENT_SPLIT.search(ch) and len(buf) >= max_chars:
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return parts or [line]


def _norm_role(r):
    return "interviewer" if r in ("面试官", "提问方", "访问者") else "candidate"


def _seg(idx, start, end, text, speaker, conf, reason, asker, source):
    return {"idx": idx, "start": float(start), "end": float(end), "text": text,
            "speaker": speaker, "confidence": conf, "reason": reason,
            "asker": asker, "source": source}


def attribute(segments, questions=None, judge=None):
    """对段做归属。judge(文本)->(speaker,conf,reason,asker) 为 LLM 桩；
    未提供时只做层 0/层 1 的确定性归属。返回 {"segments","stats"}。
    """
    questions = questions or []
    out = []
    for s in segments:
        # 人工修正与 prefix 段是终局，任何层不翻案
        if s.get("source") in ("manual", "prefix"):
            out.append(dict(s))
            continue
        sp, conf, reason, asker = "unassigned", 0.0, "模型未输出该段的判断", ""
        hit_q = _align_question(s["text"], questions)
        if hit_q is not None:
            sp, conf, reason = "interviewer", 0.6, "面试通知问题清单命中 #%d" % hit_q
        elif judge is not None:
            try:
                sp, conf, reason, asker = judge(s["text"])
            except _Skip:
                pass
        out.append(_seg(s["idx"], s["start"], s["end"], s["text"],
                        sp, conf, reason, asker, s.get("source", "engine")))
    return {"segments": out, "stats": _stats(out)}


class _Skip(Exception):
    pass


def _align_question(text, questions):
    best, best_i = 0.0, None
    for i, q in enumerate(questions):
        qt = q["q"] if isinstance(q, dict) else q
        sim = similarity(text, qt)
        if sim > best:
            best, best_i = sim, i
    return best_i if best >= 0.5 else None


def _stats(segs):
    inter = sum(1 for s in segs if s["speaker"] == "interviewer")
    cand = sum(1 for s in segs if s["speaker"] == "candidate")
    un = sum(1 for s in segs if s["speaker"] == "unassigned")
    assigned = inter + cand
    total = len(segs) or 1
    return {
        "interviewer": inter,
        "candidate": cand,
        "unassigned": un,
        "coverage": round(assigned / total, 3),
        "asker_assigned": sum(1 for s in segs if s.get("asker")),
    }
