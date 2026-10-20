"""证据包（bundle）+ 离线可验证脚本。

把一次审查的「材料摘要 + 主张 + 原文切片 + 闸门结论」打包成 zip，并附一份独立的
``verify.py``。对方不信任我们时，下载包、解压、跑 ``python verify.py`` 就能自己复验：

* 引文是否仍逐字命中包内材料；
* 包内每个成员（claims.jsonl / findings.jsonl / gate.json / artifacts/*）的
  SHA-256 是否与 manifest.json 里记录的 ``file_digests`` 一致——任一字节被改都能发现。

确定性要求：同一组输入，build_bundle 必须产出字节级完全相同的 zip。
"""
import hashlib
import io
import json
import os
import zipfile
from   engine.spine.ir import canonical_json, digest

_VERIFY_PY = r'''
# EvidenceGate 证据包离线校验器（独立、无依赖）。
# 用法：python verify.py  | 退出码 0 = 通过，1 = 失败。仅依赖标准库。
# 在解压后的目录里运行，直接读本目录的 manifest.json 与各成员文件。
import hashlib
import json
import os
import re
import sys
import unicodedata


def norm(t):
    t = unicodedata.normalize("NFKC", t or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    man_path = os.path.join(here, "manifest.json")
    if not os.path.exists(man_path):
        print("VERIFY FAILED: missing manifest.json")
        return 1
    with open(man_path, encoding="utf-8") as fh:
        man = json.load(fh)
    failed = []

    # 1) 防篡改：每个成员的哈希必须与 manifest 记录一致
    digests = man.get("file_digests", {})
    for name, expect in digests.items():
        p = os.path.join(here, name)
        if not os.path.exists(p):
            failed.append("缺失成员: %s" % name)
            continue
        if sha256_file(p) != expect:
            failed.append("成员被篡改: %s" % name)

    # 2) 引文逐字命中原文
    arts = {a["id"]: a for a in man.get("artifacts", [])}
    for c in man.get("claims", []):
        for ref in c.get("citations", []):
            a = arts.get(ref.get("artifact_id"))
            if not a:
                failed.append("%s: 引用材料缺失" % c["id"])
                continue
            if a.get("text"):
                if norm(ref.get("quote", "")) not in norm(a["text"]):
                    failed.append("%s: 引文未命中原文" % c["id"])

    # 3) 闸门结论须为 pass
    gate = man.get("gate", {})
    if gate.get("verdict") != "pass":
        failed.append("闸门结论非 pass: %s" % gate.get("verdict"))

    if not failed:
        print("VERIFY OK （未签名：仅可核验，不可抵赖）")
        return 0
    print("VERIFY FAILED")
    for f in failed:
        print("  -", f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''


def _member_bytes(case, artifacts, claims, findings, gate_result, approvals, include_text):
    """把所有成员组装成 {name: bytes}，便于确定性写入与哈希。"""
    manifest = _build_manifest(case, artifacts, claims, findings, gate_result,
                               approvals, include_text)
    members = {"manifest.json": canonical_json(manifest).encode("utf-8")}
    claims_jsonl = "".join(canonical_json(c.to_dict()) + "\n" for c in claims).encode("utf-8")
    members["claims.jsonl"] = claims_jsonl
    findings_jsonl = "".join(canonical_json(f.to_dict()) + "\n" for f in findings).encode("utf-8")
    members["findings.jsonl"] = findings_jsonl
    members["gate.json"] = canonical_json(
        gate_result.to_dict() if hasattr(gate_result, "to_dict") else gate_result).encode("utf-8")
    if include_text:
        for a in artifacts:
            members["artifacts/%s.txt" % a.id] = (a.text or "").encode("utf-8")
    members["verify.py"] = _VERIFY_PY.encode("utf-8")
    return members


def _build_manifest(case, artifacts, claims, findings, gate_result, approvals, include_text):
    verified = sum(1 for c in claims if getattr(c, "status", "") == "verified")
    return {
        "schema": "evidence-bundle/1",
        "generated_at": "",
        "disclaimer": "本证据包未签名、未做外部时间戳锚定；只能证明包内内容自生成后未被篡改，"
                      "不能证明链头未被整体替换，也不能替代事实真实性核验。",
        "case": case.to_dict() if hasattr(case, "to_dict") else case,
        "counts": {"verified": verified, "total": len(claims)},
        "artifacts": [
            {"id": a.id, "role": a.role, "filename": a.filename,
             "sha256": a.ensure_hash(), "text": (a.text if include_text else None)}
            for a in artifacts
        ],
        "claims": [c.to_dict() for c in claims],
        "findings": [f.to_dict() for f in findings],
        "approvals": [ap.to_dict() for ap in (approvals or [])],
        "gate": gate_result.to_dict() if hasattr(gate_result, "to_dict") else gate_result,
    }


def build_bundle(case, artifacts, claims, findings, gate_result, approvals=(), include_text=False, generated_at=""):
    members = _member_bytes(case, artifacts, claims, findings, gate_result, approvals, include_text)
    # 把 generated_at / file_digests 写回 manifest（在算完成员哈希之后）
    man = json.loads(members["manifest.json"].decode("utf-8"))
    man["generated_at"] = generated_at
    man["file_digests"] = {n: hashlib.sha256(b).hexdigest()
                           for n, b in members.items() if n != "manifest.json"}
    members["manifest.json"] = canonical_json(man).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members.keys()):
            zf.writestr(name, members[name])
    return out.getvalue()


def hash_of(data):
    if isinstance(data, (dict, list)):
        data = canonical_json(data)
    return hashlib.sha256((data or "").encode("utf-8")).hexdigest()
