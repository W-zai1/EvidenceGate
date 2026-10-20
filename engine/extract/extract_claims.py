"""主张抽取（extract 层）。

把"一段证据文本"变成"可被脊柱核验的主张候选"——关键动作是**给每段主张标出它引用
的原文片段（quote）与定位（locator）**，这样下游 verify 才能确定性地回链。

本模块不调 LLM、不做真伪判断（那是脊柱闸门的事）。它只负责：
- 把长文本按句切成主张候选；
- 为每条候选在给定证据文本里找原文锚点（quote）与字符定位（locator）；
- 返回 [(text, citations=[{locator,quote}])]，status 默认 unverified（由脊柱决定）。

纯函数、零依赖、可离线。LLM 增强（如抽取更细的主张）留给上层可选注入。
"""
import re

_SENT = re.compile(r"[。！？!?；;\n]+")


def split_sentences(text):
    out, buf = [], ""
    for ch in text:
        buf += ch
        if _SENT.search(ch):
            t = buf.strip()
            if t:
                out.append(t)
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def _locate_quote(evidence_text, quote, window=24):
    """在证据文本里找 quote 的字符定位；找不到返回空 locator。"""
    i = evidence_text.find(quote)
    if i < 0:
        return ""
    start = max(0, i - window)
    snippet = evidence_text[start:i]
    loc = "char %d" % i
    if snippet.strip():
        loc = (snippet.strip()[-window:]) + " … " + loc
    return loc


def extract_claims(text, evidence_text=None, meta=None):
    """text：待抽取文本；evidence_text：回链用的证据原文（默认用 text 自身）。

    返回：[{"text","citations":[{"locator","quote"}]}]，无句子返回 []。
    """
    evidence_text = evidence_text if evidence_text is not None else text
    claims = []
    for sent in split_sentences(text):
        quote = sent.strip()
        citations = []
        if evidence_text and quote:
            loc = _locate_quote(evidence_text, quote)
            if loc:
                citations.append({"locator": loc, "quote": quote})
        claims.append({
            "text": sent.strip(),
            "citations": citations,
            "status": "unverified",
            "meta": meta or {},
        })
    return claims


def claims_to_spine(claims):
    """把抽取结果转成脊柱认得的入参形状：每条只留 text + citations。"""
    return [{"text": c["text"], "citations": c["citations"]} for c in claims]
