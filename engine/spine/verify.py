"""引用核验器（确定性，不调模型）。

每条审查结论必须引用系统中真实存在的证据，并能定位到原文。核验器做四件事：

* 引用是否存在（artifact_id 在材料库里）；
* 引文是否逐字命中原文（带 NFKC / 空白归一，兼容全角数字与排版差异）；
* 字符区间（若提供）是否真的框住了那段引文；
* 算术是否复算一致（如「合计 = 各项之和」）。

引文存在不等于引文能证明结论——语义关系由审查分析节点判断，这里只负责「能不能回链」。
"""
import re
import unicodedata

from engine.spine.ir import (
    Claim, EvidenceRef, Finding, UNVERIFIED, VERIFIED, UNSUPPORTED,
    CAT_CITATION,
)


def _norm(text):
    if text is None:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = t.replace("\u3000", " ")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _artifact_by_id(artifacts):
    return {a.id: a for a in artifacts}


def locate(artifact, quote):
    norm_text = _norm(artifact.text)
    norm_quote = _norm(quote)
    if not norm_quote:
        return None
    idx = norm_text.find(norm_quote)
    if idx < 0:
        return None
    return idx, idx + len(norm_quote)


def verify_claim(claim, artifacts):
    arts = _artifact_by_id(artifacts)
    cites = claim.citations or []
    if not cites:
        return claim, UNSUPPORTED, "无引用"
    for ref in cites:
        art = arts.get(ref.artifact_id)
        if art is None:
            return claim, UNSUPPORTED, "引用不存在的材料 %s" % ref.artifact_id
        if ref.sha256 and art.sha256 and ref.sha256 != art.ensure_hash():
            return claim, UNSUPPORTED, "材料指纹不符"
        pos = locate(art, ref.quote)
        if pos is None:
            return claim, UNSUPPORTED, "原文找不到引文"
        if ref.char_start is not None and ref.char_end is not None:
            cs, ce = int(ref.char_start), int(ref.char_end)
            s, e = pos
            if not (cs <= s and e <= ce):
                return claim, UNSUPPORTED, "字符区间与引文不符"
    return claim, VERIFIED, "逐字命中"


def verify_claims(claims, artifacts):
    out = []
    verified = 0
    for c in claims:
        nc, status, note = verify_claim(c, artifacts)
        if c.status in (UNSUPPORTED, VERIFIED):
            status = c.status
        nc.status = status
        nc.note = note
        if status == VERIFIED:
            verified += 1
        out.append(nc)
    stat = {"total": len(out), "verified": verified, "unsupported": len(out) - verified}
    return out, stat


def sums_equal(parts, total, tol=1e-6):
    try:
        return abs(sum(float(p) for p in parts) - float(total)) <= tol
    except (TypeError, ValueError):
        return False


def coverage(artifacts, required_roles):
    present = {a.role for a in artifacts}
    missing = [r for r in required_roles if r not in present]
    return {"present": sorted(present), "missing": missing}


def audit_claims(claims):
    out = []
    for c in claims:
        if c.status == UNSUPPORTED:
            out.append(Finding(
                code="CITE-%s" % c.id, title="引用无法回链：%s" % (c.text or c.id),
                category=CAT_CITATION, status="open",
                detail=c.note or "该主张的引用未通过确定性核验",
            ))
    return out


# 兼容旧调用：``verify`` 即批量核验入口。
verify = verify_claims
