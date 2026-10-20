"""通用案件层（领域无关）。

把「一次取证-裁定-闸门-留痕」从任何具体业务（人才 / 报销）里抽出来，变成一张
通用的 ``cases`` 表。本模块只做三件事：

1. 把材料 / 主张 / 签署 / 闸门结论**落库**（材料正文在应用层加密，见 ``crypto``）；
2. 把脊柱（``engine.spine``）的纯函数接到数据库上——run_gate 只是把库里的行
   装配成脊柱对象再调 gate.from_profile；
3. 把证据包（spine.bundle）接到库上——bundle_bytes 只是把库里的行喂给 build_bundle。

本模块**不写任何业务判断**：能不能通过由脊柱的 profile + 闸门条款决定。
"""
import json

from engine.storage import db
from engine.intake import crypto
from engine.spine import ir
from engine.spine import verify as spine_verify
from engine.spine import gate as spine_gate
from engine.spine.profiles import registry

SIGN_ROLES = {"claimant", "approver", "reviewer", "maker", "checker"}


class CaseError(ValueError):
    """案件层的用户输入错误（角色越界、引用幽灵材料、结论过期等）。"""


def _now():
    return db.now_iso()


def _require_profile(key):
    try:
        return registry.get(key)
    except KeyError:
        raise KeyError("未知 profile: %s" % key)


def create_case(title, profile_key, note="", actor="", single_deploy=False):
    prof = _require_profile(profile_key)
    cid = db.new_id("case")
    ts = _now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO cases(id, title, profile_key, profile_version, status,"
            " note, created_at, updated_at, single_deploy)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, title, prof.key, prof.version, "draft", note, ts, ts,
             1 if single_deploy else 0))
    return get_case(cid)


def add_artifact(case_id, role, filename, text, actor=""):
    prof = _profile_of(case_id)
    if role not in prof.required_roles:
        raise CaseError("不接受材料角色 %r（本场景只收 %s）" % (role, prof.required_roles))
    aid = db.new_id("art")
    enc = crypto.encrypt_text(text or "")
    sha = ir.Artifact(id=aid, role=role, filename=filename, text=text or "").ensure_hash()
    ts = _now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO case_artifacts(id, case_id, role, filename, text_enc,"
            " sha256, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, case_id, role, filename, enc, sha, actor, ts))
        _touch(c, case_id)
    return {"id": aid, "role": role, "filename": filename,
            "sha256": sha, "created_at": ts}


def set_claims(case_id, claims, actor=""):
    arts = {a["id"]: a for a in _raw_artifacts(case_id)}
    for cl in claims:
        for ref in cl.get("citations", []):
            if ref.get("artifact_id") not in arts:
                raise CaseError("引用了不存在的材料: %s" % ref.get("artifact_id"))
    ts = _now()
    with db.tx() as c:
        c.execute("DELETE FROM case_claims WHERE case_id=?", (case_id,))
        for i, cl in enumerate(claims):
            cid = db.new_id("clm")
            c.execute(
                "INSERT INTO case_claims(id, case_id, ord, text, stance, citations,"
                " status, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, case_id, i, cl.get("text", ""), cl.get("stance", "support"),
                 json.dumps(cl.get("citations", []), ensure_ascii=False),
                 "unverified", cl.get("note", ""), ts))
        _touch(c, case_id)
    return get_case(case_id)


def sign(case_id, role, actor):
    if role not in SIGN_ROLES:
        raise CaseError("签署角色 %r 不适用于场景" % role)
    aid = db.new_id("apr")
    ts = _now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO case_approvals(id, case_id, role, actor, created_at)"
            " VALUES (?,?,?,?,?)", (aid, case_id, role, actor, ts))
        _touch(c, case_id)
    return get_case(case_id)


def run_gate(case_id, actor=""):
    prof = _profile_of(case_id)
    arts = _spine_artifacts(case_id)
    claims = _spine_claims(case_id, arts)
    claims, stat = spine_verify.verify_claims(claims, arts)
    _persist_claim_status(case_id, claims)
    findings = spine_verify.audit_claims(claims)
    _persist_findings(case_id, findings)
    approvals = _spine_approvals(case_id)
    trail = ir.TrailSnapshot(ok=True, count=len(arts), head="0" * 64, anchored=False)
    ctx = spine_gate.GateContext(
        claims=claims, findings=findings, artifacts=arts,
        approvals=approvals, trail=trail, required_roles=list(prof.required_roles), judge_roles=list(prof.judge_roles))
    res = spine_gate.from_profile(prof, ctx, evaluated_at=_now())
    _persist_gate(case_id, res)
    status = {"pass": "verified", "fail": "blocked",
              "needs_human": "review"}.get(res.verdict, "blocked")
    _set_status(case_id, status)
    return _assemble(case_id, res, claims, findings, approvals)


def bundle_bytes(case_id, actor=""):
    row = db.connect().execute(
        "SELECT revision, gate_revision FROM cases WHERE id=?", (case_id,)).fetchone()
    if row and row["gate_revision"] and row["revision"] > row["gate_revision"]:
        raise CaseError("案件在最近一次闸门之后被改动，请重新求值闸门后再导出")
    prof = _profile_of(case_id)
    arts = _spine_artifacts(case_id)
    claims = _spine_claims(case_id, arts)
    findings = _spine_findings(case_id)
    approvals = _spine_approvals(case_id)
    row2 = db.connect().execute(
        "SELECT gate_json FROM cases WHERE id=?", (case_id,)).fetchone()
    gate_res = spine_gate.GateResult.from_dict(json.loads(row2["gate_json"] or "{}")) if row2 and row2["gate_json"] else None
    case = ir.Case(id=case_id, title="", profile_key=prof.key,
                   profile_version=prof.version)
    data = spine_gate.build_bundle(
        case, arts, claims, findings, gate_res, approvals=approvals)
    srow = db.connect().execute(
        "SELECT single_deploy FROM cases WHERE id=?", (case_id,)).fetchone()
    return {"case_id": case_id, "data_base64": _b64(data),
            "filename": "evidence-%s.zip" % case_id,
            "single_deploy": bool(srow["single_deploy"]) if srow else False}


def list_cases():
    rows = db.connect().execute(
        "SELECT id, title, profile_key, profile_version, status FROM cases"
        " ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_case(case_id, actor=""):
    with db.tx() as c:
        c.execute("DELETE FROM case_claims WHERE case_id=?", (case_id,))
        c.execute("DELETE FROM case_findings WHERE case_id=?", (case_id,))
        c.execute("DELETE FROM case_approvals WHERE case_id=?", (case_id,))
        c.execute("DELETE FROM gate_results WHERE case_id=?", (case_id,))
        c.execute("DELETE FROM case_artifacts WHERE case_id=?", (case_id,))
        c.execute("DELETE FROM cases WHERE id=?", (case_id,))


def get_case(case_id):
    """返回一条案件的完整视图（含材料正文、主张、闸门结论、签署情况）。

    这是给界面看的聚合视图：使用者在详情页要能一次看到「材料 / 主张 /
    闸门条款 / 签署」四段，而不必自己拼。
    """
    row = db.connect().execute(
        "SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        raise CaseError("案件不存在: %s" % case_id)
    d = dict(row)
    d["artifacts"] = _raw_artifacts(case_id)
    d["claims"] = _raw_claims(case_id)
    d["approvals"] = _raw_approvals(case_id)
    d["findings"] = _raw_findings(case_id)
    d["profile_label"] = "%s@%s" % (d["profile_key"], d["profile_version"])
    prof = _require_profile(d["profile_key"])
    # 该场景需要哪些人签（决定详情页「三级复核」要渲染几个格子）
    d["sign_roles"] = list(prof.judge_roles)
    d["single_deploy"] = bool(d.get("single_deploy"))
    for a in d["artifacts"]:
        a["chars"] = len(a.get("text") or "")
    d["counts"] = {
        "artifacts": len(d["artifacts"]),
        "claims": len(d["claims"]),
        "verified": sum(1 for c in d["claims"] if c.get("status") == "verified"),
        "unsupported": sum(1 for c in d["claims"] if c.get("status") == "unsupported"),
        "signed": len(d["approvals"]),
        "sign_total": len(prof.judge_roles),
    }
    d["gate"] = _latest_gate(case_id, stale=_gate_is_stale(d))
    if not d.get("evidence_digest") and d["gate"]:
        d["evidence_digest"] = d["gate"].get("hash", "")
    return d


def _latest_gate(case_id, stale=False):
    """最近一次闸门结论。同一秒内多次求值时用 rowid 而不是时间戳排序。"""
    row = db.connect().execute(
        "SELECT result_json, evaluated_at FROM gate_results WHERE case_id=?"
        " ORDER BY rowid DESC LIMIT 1", (case_id,)).fetchone()
    if not row or not row["result_json"]:
        return None
    try:
        g = json.loads(row["result_json"])
    except ValueError:
        return None
    # spine 用 title 表述条款；界面用 requirement（读起来是"要求"）。
    # 注意：不能把 requirement 塞进 clauses 里——clauses 参与 hash 计算，
    # 加一个展示字段就会让复算对不上，把一个好结论误判成被篡改。
    # 所以只给 clauses 做一份浅拷贝用于展示，原始结构保持不动。
    g["clauses"] = [dict(cl, requirement=cl.get("title", ""))
                    for cl in g.get("clauses", [])]
    g["stale"] = bool(stale)
    return g


def _gate_is_stale(case_row):
    """案件在最近一次闸门之后被改过 → 当前结论不对应现在的材料。

    用修订号比，不用时间戳：时间只到秒，求值和改材料落在同一秒时比不出先后，
    会把过期结论当成有效的放出去。
    """
    cur = case_row.get("revision") or 0
    at = case_row.get("gate_revision")
    if at is None:
        # 老数据没有 gate_revision，退回时间戳比较
        row = db.connect().execute(
            "SELECT evaluated_at FROM gate_results WHERE case_id=?"
            " ORDER BY rowid DESC LIMIT 1", (case_row["id"],)).fetchone()
        if not row:
            return False
        return (case_row.get("updated_at") or "") > (row["evaluated_at"] or "")
    return int(cur) > int(at)


def _profile_of(case_id):
    row = db.connect().execute(
        "SELECT profile_key FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        raise CaseError("案件不存在: %s" % case_id)
    return _require_profile(row["profile_key"])


def _raw_artifacts(case_id):
    rows = db.connect().execute(
        "SELECT id, role, filename, text_enc, sha256, created_at FROM case_artifacts"
        " WHERE case_id=?", (case_id,)).fetchall()
    out = []
    for r in rows:
        text = crypto.decrypt_text(r["text_enc"] or "")
        out.append({"id": r["id"], "role": r["role"], "filename": r["filename"],
                    "text": text, "sha256": r["sha256"], "created_at": r["created_at"]})
    return out


def _raw_claims(case_id):
    rows = db.connect().execute(
        "SELECT id, text, stance, citations, status, note FROM case_claims"
        " WHERE case_id=? ORDER BY ord", (case_id,)).fetchall()
    return [{"id": r["id"], "text": r["text"], "stance": r["stance"],
             "citations": json.loads(r["citations"] or "[]"),
             "status": r["status"], "note": r["note"]} for r in rows]


def _raw_approvals(case_id):
    rows = db.connect().execute(
        "SELECT id, role, actor, created_at FROM case_approvals WHERE case_id=?",
        (case_id,)).fetchall()
    return [{"id": r["id"], "role": r["role"], "actor": r["actor"],
            "created_at": r["created_at"]} for r in rows]


def _raw_findings(case_id):
    rows = db.connect().execute(
        "SELECT id, code, title, category, status, detail FROM case_findings"
        " WHERE case_id=?", (case_id,)).fetchall()
    return [dict(r) for r in rows]


def _spine_artifacts(case_id):
    out = []
    for a in _raw_artifacts(case_id):
        out.append(ir.Artifact(id=a["id"], role=a["role"], filename=a["filename"],
                               text=a["text"]))
    return out


def _spine_claims(case_id, arts):
    by_id = {a.id: a for a in arts}
    out = []
    for c in _raw_claims(case_id):
        refs = []
        for ref in c["citations"]:
            a = by_id.get(ref.get("artifact_id"))
            refs.append(ir.EvidenceRef(
                artifact_id=ref.get("artifact_id"), locator=ref.get("locator", ""),
                quote=ref.get("quote", ""), sha256=(a.ensure_hash() if a else "")))
        out.append(ir.Claim(id=c["id"], text=c["text"], citations=refs,
                           status=c["status"], note=c.get("note", "")))
    return out


def _spine_approvals(case_id):
    return [ir.Approval(a["role"], a["actor"], a["created_at"])
            for a in _raw_approvals(case_id)]


def _spine_findings(case_id):
    return [ir.Finding(code=f["code"], title=f["title"], category=f["category"],
                      status=f["status"], detail=f["detail"])
            for f in _raw_findings(case_id)]


def _persist_claim_status(case_id, claims):
    with db.tx() as c:
        for cl in claims:
            c.execute("UPDATE case_claims SET status=?, note=? WHERE id=?",
                      (cl.status, cl.note or "", cl.id))


def _persist_findings(case_id, findings):
    with db.tx() as c:
        c.execute("DELETE FROM case_findings WHERE case_id=?", (case_id,))
        for f in findings:
            fid = db.new_id("fnd")
            c.execute(
                "INSERT INTO case_findings(id, case_id, code, title, category,"
                " status, detail) VALUES (?,?,?,?,?,?,?)",
                (fid, case_id, f.code, f.title, f.category, f.status, f.detail))


def _persist_gate(case_id, res):
    gid = db.new_id("gt")
    ts = _now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO gate_results(id, case_id, verdict, result_json, evaluated_at,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (gid, case_id, res.verdict, json.dumps(res.to_dict(), ensure_ascii=False),
             res.evaluated_at, ts))
        # gate_revision 记下"这次结论是对着哪一版案件算的"：之后任何改动
        # 把 revision 推高，结论就自动判为过期，不需要额外清理。
        c.execute("UPDATE cases SET gate_json=?, evidence_digest=?,"
                  " gate_revision=revision WHERE id=?",
                  (json.dumps(res.to_dict(), ensure_ascii=False), res.hash, case_id))


def _set_status(case_id, status):
    with db.tx() as c:
        c.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?",
                  (status, _now(), case_id))


def _assemble(case_id, res, claims, findings, approvals):
    # 统一走 get_case 的视图口径：否则 run_gate 返回的闸门缺 requirement 别名，
    # 界面在「刚求值完」和「重新打开」两种路径上会渲染出不同的东西。
    d = get_case(case_id)
    d["gate"] = _latest_gate(case_id, stale=False)
    return d


def _touch(c, case_id):
    """标记案件被改动过。

    没有这一步，「改完材料再导出」会悄悄导出一份与当前材料不符的旧结论——
    闸门结论的可信度全靠「改动即过期」这条不变量撑着。
    """
    c.execute("UPDATE cases SET updated_at=?, revision=revision+1 WHERE id=?",
              (_now(), case_id))


def _b64(data):
    import base64
    return base64.b64encode(data).decode("ascii")


def verify_stored(gate_dict):
    return spine_gate.verify_stored(gate_dict)
