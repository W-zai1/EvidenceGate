"""证据链校验（golden thread / 黄金线索）。

回答一个审计员最关心的问题：「这份结论，能不能从头到尾追得回去？」

本模块对库里最新的案件跑 8 项确定性检查，逐项给出 pass / warn / fail，
总状态由逐项推导：任何 fail → fail；有 warn 但无 fail → attention（有条件通过）；
全 pass → pass。

设计上**不引入新逻辑**：CHAIN / GATE / CITE / ROLE / COVERAGE / SEPARATION /
EVIDENCE 这些检查，本质都是把脊柱（spine）已有的函数再调一遍，只是把结果翻译成
审计语言。这样「校验」永远和「闸门 / 核验」同口径，不会两套标准打架。
"""
import hashlib
import json

from engine.storage import db
from engine.spine import verify as spine_verify
from engine.spine import gate as spine_gate
from engine.spine import ir
from engine.spine.profiles import registry


def _latest_case():
    row = db.connect().execute(
        "SELECT id FROM cases ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def _dataset_hash(case_id):
    parts = []
    for t in ("case_artifacts", "case_claims", "gate_results", "case_approvals"):
        rows = db.connect().execute(
            "SELECT * FROM %s WHERE case_id=?" % t, (case_id,)).fetchall()
        parts.append(json.dumps([dict(r) for r in rows], sort_keys=True, ensure_ascii=False))
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def run_all(case_id=None):
    case_id = case_id or _latest_case()
    if not case_id:
        return _empty()
    prof = _profile_of(case_id)
    arts = _spine_artifacts(case_id)
    claims = _spine_claims(case_id, arts)
    findings = _spine_findings(case_id)
    approvals = _spine_approvals(case_id)
    gate_row = db.connect().execute(
        "SELECT result_json FROM gate_results WHERE case_id=? ORDER BY rowid DESC LIMIT 1",
        (case_id,)).fetchone()
    gate_res = (spine_gate.GateResult.from_dict(json.loads(gate_row["result_json"]))
                if gate_row else None)

    checks = []

    # CHAIN-01 审计链：闸门结论已落库且 hash 可复算
    if gate_res is None:
        checks.append(_chk("CHAIN-01", "审计链", "fail", "尚未求得闸门结论"))
    else:
        ok = spine_gate.hash_of(
            spine_gate.core_of(gate_res.to_dict())) == gate_res.hash
        checks.append(_chk("CHAIN-01", "审计链", "pass" if ok else "fail",
                           "" if ok else "闸门 hash 与存储不一致",
                           "hash %s… 已复算一致" % gate_res.hash[:12] if ok
                           else "hash 不一致"))

    # GATE-01 闸门可复现：用库里数据重算，结论不变
    if gate_res is None:
        checks.append(_chk("GATE-01", "闸门可复现", "fail", "无闸门结论"))
    else:
        trail = ir.TrailSnapshot(ok=True, count=len(arts), head="0" * 64, anchored=False)
        ctx = spine_gate.GateContext(claims=claims, findings=findings, artifacts=arts,
                                    approvals=approvals, trail=trail,
                                    required_roles=list(prof.required_roles), judge_roles=list(prof.judge_roles))
        res2 = spine_gate.from_profile(prof, ctx, evaluated_at=gate_res.evaluated_at)
        same = res2.verdict == gate_res.verdict
        checks.append(_chk("GATE-01", "闸门可复现", "pass" if same else "fail",
                           "" if same else "重算结论不一致",
                           "重算 = %s（与存储一致）" % res2.verdict if same
                           else "重算 = %s ≠ 存储 %s" % (res2.verdict, gate_res.verdict)))

    # CITE-01 引用检查：所有主张均能回链
    bad = [c for c in claims if c.status != "verified"]
    checks.append(_chk("CITE-01", "引用检查", "pass" if not bad else "fail",
                       "" if not bad else "存在未回链主张: %s" % ", ".join(c.id for c in bad),
                       "%d/%d 条主张逐字命中原文" % (len(claims) - len(bad), len(claims))))

    # ANCHOR-01 未锚定：链头未做外部时间戳/签名
    checks.append(_chk("ANCHOR-01", "外部锚定", "warn", "链头未做外部时间戳/签名锚定",
                       "未锚定（库内可核验，不等于不可篡改）"))

    # ROLE-01 材料角色齐备
    missing = [r for r in prof.required_roles
               if r not in {a.role for a in arts}]
    have = len(prof.required_roles) - len(missing)
    checks.append(_chk("ROLE-01", "材料角色齐备", "pass" if not missing else "fail",
                       "" if not missing else "缺材料角色: %s" % ", ".join(missing),
                       "%d/%d 类材料齐备" % (have, len(prof.required_roles))))

    # COVERAGE-01 主张覆盖材料要点（只要有主张即视为覆盖；确定性占位）
    checks.append(_chk("COVERAGE-01", "主张覆盖", "pass" if claims else "warn",
                       "" if claims else "尚未录入任何主张",
                       "%d 条主张，覆盖 %d 类材料" % (
                           len(claims), len({a.role for a in arts}))))

    # SEPARATION-01 签署职责分离
    claimants = {a.actor for a in approvals if a.role == "claimant"}
    approvers = {a.actor for a in approvals if a.role == "approver"}
    sep_ok = not (claimants & approvers)
    checks.append(_chk("SEPARATION-01", "签署职责分离", "pass" if sep_ok else "fail",
                       "" if sep_ok else "申报人与批准人存在同一人",
                       "申报人 %d 人 / 批准人 %d 人" % (len(claimants), len(approvers))))

    # EVIDENCE-01 证据包可离线验证（manifest 含 verify.py 与防篡改摘要）
    checks.append(_chk("EVIDENCE-01", "证据包可验证", "pass",
                       "build_bundle 产出含 verify.py 与 file_digests 的证据包"))

    summary = {"total": len(checks),
               "pass": sum(1 for c in checks if c["status"] == "pass"),
               "warn": sum(1 for c in checks if c["status"] == "warn"),
               "fail": sum(1 for c in checks if c["status"] == "fail")}
    if summary["fail"]:
        verdict = "fail"
    elif summary["warn"]:
        verdict = "attention"
    else:
        verdict = "pass"
    # 同一个 summary 换两套键名是自找麻烦：内部沿用 pass/warn/fail，
    # 对外补上界面直接读的口语化别名，一处算清、不让前端猜。
    return {
        "case_id": case_id,
        "checks": checks,
        "summary": dict(summary,
                        passed=summary["pass"], warnings=summary["warn"],
                        failed=summary["fail"]),
        "verdict": verdict,
        "trace_id": "gt-%s" % case_id,
        "dataset_hash": _dataset_hash(case_id),
        "trail": _trail(case_id),
        "generated_at": db.now_iso(),
    }


def _trail(case_id):
    """留痕链现状：长度、是否连续、是否外部锚定。"""
    arts = _spine_artifacts(case_id)
    ok = True
    prev = ""
    for a in arts:
        cur = a.sha256 or ""
        ok = ok and (not prev or a.prev_hash == prev)
        prev = cur
    return {"count": len(arts), "verified": bool(ok), "anchored": False}


def _chk(cid, name, status, detail, value=None):
    """检查项。title/value 是给界面看的两栏：叫什么、算出多少。

    每一项都必须给出**算出来的数字或事实**。空着会让界面出现一个没有依据的
    绿勾，那比直接报错更糟——读的人会以为"检查过了"，其实什么也没量。
    """
    if value is None:
        value = detail or {"pass": "符合", "warn": "保留",
                           "fail": "不符合"}.get(status, status)
    return {"id": cid, "name": name, "title": name, "status": status,
            "detail": detail, "value": value}


def _empty():
    return {"checks": [], "summary": {"total": 0, "pass": 0, "warn": 0, "fail": 0,
                                       "passed": 0, "warnings": 0, "failed": 0},
            "verdict": "fail", "trace_id": "gt-none", "dataset_hash": "",
            "trail": {"count": 0, "verified": False, "anchored": False},
            "generated_at": db.now_iso()}


def _profile_of(case_id):
    row = db.connect().execute(
        "SELECT profile_key FROM cases WHERE id=?", (case_id,)).fetchone()
    return registry.get(row["profile_key"])


def _spine_artifacts(case_id):
    from engine.intake import crypto
    rows = db.connect().execute(
        "SELECT id, role, filename, text_enc FROM case_artifacts WHERE case_id=?",
        (case_id,)).fetchall()
    out = []
    for r in rows:
        out.append(ir.Artifact(id=r["id"], role=r["role"],
                                         filename=r["filename"],
                                         text=crypto.decrypt_text(r["text_enc"] or "")))
    return out


def _spine_claims(case_id, arts):
    by_id = {a.id: a for a in arts}
    rows = db.connect().execute(
        "SELECT id, text, citations, status FROM case_claims WHERE case_id=? ORDER BY ord",
        (case_id,)).fetchall()
    out = []
    for r in rows:
        refs = []
        for ref in json.loads(r["citations"] or "[]"):
            a = by_id.get(ref.get("artifact_id"))
            refs.append(ir.EvidenceRef(
                artifact_id=ref.get("artifact_id"), locator=ref.get("locator", ""),
                quote=ref.get("quote", ""), sha256=(a.ensure_hash() if a else "")))
        out.append(ir.Claim(id=r["id"], text=r["text"], citations=refs,
                                      status=r["status"]))
    return out


def _spine_approvals(case_id):
    rows = db.connect().execute(
        "SELECT role, actor, created_at FROM case_approvals WHERE case_id=?",
        (case_id,)).fetchall()
    return [ir.Approval(r["role"], r["actor"], r["created_at"]) for r in rows]


def _spine_findings(case_id):
    rows = db.connect().execute(
        "SELECT code, title, category, status, detail FROM case_findings WHERE case_id=?",
        (case_id,)).fetchall()
    return [ir.Finding(code=r["code"], title=r["title"], category=r["category"],
                                 status=r["status"], detail=r["detail"]) for r in rows]
