"""
人才库（跨岗位候选人资产）—— 按 `people` 表聚合，不再按姓名。

原来错在哪
----------
旧实现 `talent_id(name) = sha1(归一化姓名)`，即**同名 = 同一个人**。
这不只是"不够准"，而是会把两个人的材料搅在一起：
王磊 A 在北京做后端、王磊 B 在成都做设计，打开人才库是同一个人，
简历、分数、备注全都混成一份。HR 据此做的判断是错的，而且无从发现。

现在的口径
----------
身份由 `people.id` 承载，归属关系由多信号比对决定（见 identity.py / store.py）：
  · 电话/邮箱一致 → 同一人
  · 院校 + 单位 + 城市多个信号一致 → 同一人
  · 只有姓名一致 → **pending**，人才库里明确标注"待确认"，
    不合并、也不会被当成同一个人统计

聚合用一条 SQL 取全部候选人 + 人员信息，不再"每个岗位读一遍文件再扫全表"。
"""
import secrets
from   datetime import datetime
from   engine.guardrails import audit
from   engine.intake import crypto
from   engine.storage import db
from   engine.extract import identity
from   engine.guardrails import outcome
from   engine.storage import store

TALENT_STATUSES = (
    ("new", "新入库"),
    ("screening", "评审中"),
    ("passed", "通过筛选"),
    ("hold", "待定"),
    ("rejected", "已淘汰"),
    ("hired", "已入职"),
    ("archived", "已归档"),
)
STATUS_LABEL = dict(TALENT_STATUSES)
STATUS_ORDER = [k for k, _ in TALENT_STATUSES]

IDENTITY_LABEL = {
    "auto": "自动判定",
    "uncertain": "待确认（疑似同一人）",
    "confirmed": "已人工确认",
}


def _now():
    return db.now_iso()


def _fmt_date(iso):
    return (iso or "").replace("T", " ")[:16]


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def aggregate(with_text=False):
    """按人汇总。返回 [entry]，按最佳得分降序。

    一次 SQL 拿全量数据，Python 侧聚合——比"逐岗位读文件"快一个数量级，
    且不会因为某岗位读失败而整页空白。
    """
    db.init()
    conn = db.connect()

    people = {}
    # ⚠ 必须排除 `merged_into` 非空的档案。
    # 踩过的坑：HR 在界面上确认"是同一人"后，候选人被挂到目标档案、
    # 源档案被写上 merged_into=目标，但这里没过滤 merged_into，
    # 于是人才库里同时出现"合并后的档案"和"已被合并的空壳"——
    # 两个同名条目、其中一个没有评审记录，看起来就像合并失败了。
    for r in conn.execute(
        "SELECT p.*, IFNULL(p.merged_into,'') AS merged FROM people p"
        " WHERE IFNULL(p.erased_at,'')='' AND IFNULL(p.merged_into,'')=''"
    ):
        people[r["id"]] = {
            "id": r["id"],
            "name": r["name"] or "未命名",
            "identity_state": "auto",
            "evaluations": [],
            "jobs": [],
            "_job_seen": set(),
            "materials": {"resume_chars": 0, "assessment_chars": 0, "files": 0},
            "identity": db.jload(r["identity_json"], {}),
            "_meta": {
                "status": r["status"] or "new",
                "note": r["note"] or "",
                "tags": db.jload(r["tags_json"], []),
                "retention_until": r["retention_until"] or "",
            },
        }

    job_titles = {r["id"]: (r["title"] or "未命名岗位")
                  for r in conn.execute("SELECT id, title FROM jobs")}

    for r in conn.execute(
        "SELECT id, job_id, person_id, name, identity_state, result_json, interview_json,"
        " resume_enc, assessment_enc, transcript_enc, rubric_version, created_at,"
        " retention_until, person_decision FROM candidates ORDER BY created_at ASC"
    ):
        pid = r["person_id"]
        if pid not in people:
            continue
        p = people[pid]
        job_id = r["job_id"]
        if job_id not in p["_job_seen"]:
            p["_job_seen"].add(job_id)
            p["jobs"].append({"id": job_id, "title": job_titles.get(job_id, "未命名岗位")})

        res = db.jload(r["result_json"], {}) or {}
        if res:
            p["evaluations"].append({
                "candidate_id": r["id"],
                "job_id": job_id,
                "job_title": job_titles.get(job_id, "未命名岗位"),
                "total": res.get("total"),
                "grade": res.get("grade"),
                "tags": list(res.get("tags") or []),
                "scores": res.get("scores") or {},
                "date": _fmt_date(r["created_at"]),
                "rubric_version": r["rubric_version"] or "",
                "has_interview": bool(db.jload(r["interview_json"], {})),
                "injection_risk": ((res.get("audit") or {}).get("injection") or {}).get("risk"),
            })
        if r["identity_state"] == "uncertain":
            p["identity_state"] = "uncertain"

        m = p["materials"]
        try:
            m["resume_chars"] = max(m["resume_chars"], len(crypto.decrypt_text(r["resume_enc"] or "")))
            m["assessment_chars"] = max(m["assessment_chars"],
                                        len(crypto.decrypt_text(r["assessment_enc"] or "")))
        except crypto.PIICryptoError:
            m["resume_chars"] = max(m["resume_chars"], -1)   # 标记"有材料但解不开"
        m["files"] = max(m["files"], _file_count(conn, r["id"]))
        if not p.get("retention_until"):
            p["retention_until"] = r["retention_until"] or ""

    out = []
    for pid, p in people.items():
        p.pop("_job_seen", None)
        evals = [e for e in p["evaluations"] if e.get("total") is not None]
        evals.sort(key=lambda e: e.get("date") or "", reverse=True)
        p["evaluations"] = evals
        p["eval_count"] = len(evals)
        p["job_count"] = len(p["jobs"])
        if evals:
            best = max(evals, key=lambda e: e["total"])
            p["best"] = {"total": best["total"], "grade": best["grade"],
                         "job_title": best["job_title"], "job_id": best["job_id"],
                         "rubric_version": best["rubric_version"]}
            latest = evals[0]
            p["latest"] = {"total": latest["total"], "grade": latest["grade"],
                           "job_title": latest["job_title"], "date": latest["date"],
                           "rubric_version": latest["rubric_version"]}
            # 口径不同 = 分数不可比，必须显式告诉界面
            versions = {e["rubric_version"] for e in evals if e["rubric_version"]}
            p["rubric_versions"] = sorted(versions)
            p["comparable"] = len(versions) <= 1
        else:
            p["best"] = None
            p["latest"] = None
            p["rubric_versions"] = []
            p["comparable"] = True

        tags = []
        for e in evals:
            for t in e.get("tags") or []:
                if t not in tags:
                    tags.append(t)
        p["tags"] = tags

        meta = p.pop("_meta")
        status = meta["status"] if meta["status"] in STATUS_ORDER else "new"
        p["status"] = status
        p["status_label"] = STATUS_LABEL.get(status, status)
        p["note"] = meta["note"]
        p["meta_tags"] = meta["tags"]
        p["retention_until"] = meta["retention_until"] or p.get("retention_until", "")
        p["identity_label"] = IDENTITY_LABEL.get(p["identity_state"], p["identity_state"])
        out.append(p)

    out.sort(key=lambda x: (-(x["best"]["total"] if x["best"] else -1), x["name"]))
    return out


def _file_count(conn, cand_id):
    return conn.execute("SELECT COUNT(*) AS n FROM candidate_files WHERE candidate_id=?",
                        (cand_id,)).fetchone()["n"]


def query(keyword=None, grade=None, status=None, tag=None, job_id=None,
          only_evaluated=False, pending_identity=False):
    rows = aggregate()
    kw = (keyword or "").strip().casefold()

    def hit(p):
        if kw:
            blob = " ".join([p["name"], p.get("note") or "",
                             " ".join(j["title"] for j in p["jobs"]),
                             " ".join(p["tags"])]).casefold()
            if kw not in blob:
                return False
        if grade:
            if not p["best"] or p["best"]["grade"] != grade:
                return False
        if status and p["status"] != status:
            return False
        if tag and tag not in p["tags"]:
            return False
        if job_id and not any(e["job_id"] == job_id for e in p["evaluations"]):
            return False
        if only_evaluated and not p["evaluations"]:
            return False
        if pending_identity and p["identity_state"] != "uncertain":
            return False
        return True

    return [p for p in rows if hit(p)]


def get(pid):
    for p in aggregate():
        if p["id"] == pid:
            return p
    return None


def stats():
    rows = aggregate()
    grades = {"A": 0, "B": 0, "C": 0}
    for p in rows:
        if p["best"] and p["best"]["grade"] in grades:
            grades[p["best"]["grade"]] += 1
    by_status = {}
    for p in rows:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    tag_counter = {}
    for p in rows:
        for t in p["tags"]:
            tag_counter[t] = tag_counter.get(t, 0) + 1
    return {
        "total": len(rows),
        "evaluated": sum(1 for p in rows if p["evaluations"]),
        "multi_job": sum(1 for p in rows if p["job_count"] > 1),
        "pending_identity": sum(1 for p in rows if p["identity_state"] == "uncertain"),
        "grades": grades,
        "by_status": by_status,
        "top_tags": sorted(tag_counter.items(), key=lambda kv: -kv[1])[:12],
    }


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------
def update_person(pid, patch, actor="system"):
    """更新人才库元数据（状态/备注/标签/联系方式）。联系方式加密存。"""
    db.init()
    conn = db.connect()
    row = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not row:
        raise KeyError("人才库里没有这个人：%s" % pid)

    sets, args = [], []
    if "status" in patch:
        st = patch["status"] if patch["status"] in STATUS_ORDER else "new"
        sets.append("status=?")
        args.append(st)
    if "note" in patch:
        sets.append("note=?")
        args.append((patch["note"] or "")[:4000])
    if "tags" in patch:
        sets.append("tags_json=?")
        args.append(db.jdump(patch["tags"] or []))
    if "contact" in patch:
        sets.append("contact_enc=?")
        args.append(crypto.encrypt_text(patch["contact"] or ""))
    if not sets:
        return get(pid)
    sets.append("updated_at=?")
    args.append(_now())
    args.append(pid)
    with db.tx(conn) as c:
        c.execute("UPDATE people SET %s WHERE id=?" % ", ".join(sets), args)
        audit.record(c, "talent.update", actor=actor, target_type="person", target_id=pid,
                     detail={"keys": sorted(patch.keys())})
    return get(pid)


def get_contact(pid):
    conn = db.connect()
    r = conn.execute("SELECT contact_enc FROM people WHERE id=?", (pid,)).fetchone()
    if not r or not r["contact_enc"]:
        return ""
    try:
        return crypto.decrypt_text(r["contact_enc"])
    except crypto.PIICryptoError:
        return ""


# ---------------------------------------------------------------------------
# 跨岗位复用
# ---------------------------------------------------------------------------
def import_to_job(pid, job_id, actor="system"):
    """把库里某人的材料带进目标岗位（免重新上传）。

    返回 (ok, message, candidate)。

    已在该岗位 → 不重复导入（按 person_id 判断，不再按姓名）。
    """
    db.init()
    person = get(pid)
    if not person:
        return False, "人才库里没有这个人", None
    job = store.get_job(job_id)
    if not job:
        return False, "目标岗位不存在", None

    conn = db.connect()
    dupe = conn.execute(
        "SELECT id FROM candidates WHERE job_id=? AND person_id=?", (job_id, pid)).fetchone()
    if dupe:
        return False, "该岗位里已经有这个人了", None

    # 挑材料最全的一次作为来源（同一 person 的候选人记录）
    best_src, best_len = None, -1
    for r in conn.execute("SELECT * FROM candidates WHERE person_id=?", (pid,)):
        try:
            n = sum(len(crypto.decrypt_text(r[k] or ""))
                    for k in ("resume_enc", "assessment_enc", "transcript_enc"))
        except crypto.PIICryptoError:
            continue
        if n > best_len:
            best_len, best_src = n, r
    if not best_src:
        return False, "找不到这个人的原始材料（或材料无法解密）", None

    src = store.get_candidate(best_src["id"])
    cand = store.add_candidate(
        job_id, name=src["name"],
        files=[dict(f, imported_from=True) for f in (src.get("files") or [])],
        resume_text=src.get("resume_text") or "",
        assessment_text=src.get("assessment_text") or "",
        transcript_text=src.get("transcript_text") or "",
        actor=actor,
    )
    # 显式挂到同一个 person：材料是同一个人的，无需再比对
    conn = db.connect()
    with db.tx(conn) as c:
        c.execute("UPDATE candidates SET person_id=?, identity_state='confirmed' WHERE id=?",
                  (pid, cand["id"]))
        c.execute("UPDATE people SET merged_into='' WHERE id=?", (cand["person_id"],))
        # 清掉 add_candidate 时可能新建的临时档案
        if cand["person_id"] != pid:
            c.execute("UPDATE people SET merged_into=? WHERE id=?", (pid, cand["person_id"]))
            c.execute("DELETE FROM people WHERE id=? AND NOT EXISTS"
                      " (SELECT 1 FROM candidates WHERE person_id=people.id)",
                      (cand["person_id"],))
        audit.record(c, "talent.import", actor=actor, target_type="person", target_id=pid,
                     detail={"job_id": job_id, "candidate_id": cand["id"]})

    meta = conn.execute("SELECT status FROM people WHERE id=?", (pid,)).fetchone()
    if meta and (meta["status"] or "new") == "new":
        update_person(pid, {"status": "screening"}, actor=actor)
    return True, "已加入岗位「%s」" % (job.get("title") or ""), store.get_candidate(cand["id"])


def delete_person(pid, actor="system"):
    """只删人才库档案（people 行），不动岗位里的候选人记录与材料。

    岗位里的候选人仍能查到，只是不再出现在人才库视图里。
    真要删数据请用 privacy.erase_person（会连材料一起清）。
    """
    db.init()
    conn = db.connect()
    with db.tx(conn) as c:
        cur = c.execute("DELETE FROM people WHERE id=?", (pid,))
        audit.record(c, "talent.delete", actor=actor, target_type="person", target_id=pid)
    return cur.rowcount > 0


def pending_identity_list():
    """待人工确认身份的候选人（前端需要弹确认框）。"""
    db.init()
    conn = db.connect()
    rows = conn.execute(
        "SELECT c.id, c.name, c.job_id, c.person_id, c.identity_json, j.title AS job_title"
        " FROM candidates c JOIN jobs j ON j.id = c.job_id"
        " WHERE c.identity_state='uncertain' ORDER BY c.created_at DESC").fetchall()
    out = []
    for r in rows:
        ident = db.jload(r["identity_json"], {})
        sug = ident.get("suggestion") or {}
        other = None
        if sug.get("person_id"):
            o = conn.execute("SELECT id, name, identity_json FROM people WHERE id=?",
                             (sug["person_id"],)).fetchone()
            if o:
                other = {"id": o["id"], "name": o["name"],
                         "identity": db.jload(o["identity_json"], {})}
        out.append({
            "candidate_id": r["id"], "name": r["name"], "job_id": r["job_id"],
            "job_title": r["job_title"], "person_id": r["person_id"],
            "identity": ident, "suggestion": sug, "suggested_person": other,
        })
    return out


# ---------------------------------------------------------------------------
# 结果回填（反馈闭环）
# ---------------------------------------------------------------------------
def record_outcome(pid, stage, note="", job_id="", candidate_id="", actor="system"):
    return outcome.record(pid, stage, note=note, job_id=job_id,
                          candidate_id=candidate_id, actor=actor)


def outcomes_for(pid):
    return outcome.for_person(pid)
