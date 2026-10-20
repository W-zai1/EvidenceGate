"""
隐私生命周期：保留期、导出、删除、密钥轮换。

法律依据（这不是"最佳实践"，是硬要求）
--------------------------------------
· 个保法（PIPL）第 19 条：保存期限应为实现处理目的的**最短时间**。
· 第 45 条：个人有权查阅、复制其个人信息；有权要求删除。
· 第 24 条：自动化决策须提供**解释**，并支持**拒绝仅由自动化决策作出**的决定。
· GDPR 第 17 条（删除权）、第 20 条（数据可携）。

原实现里简历明文躺在 `data/jobs/<id>/candidates.json`，没有保留期、没有导出、
没有删除接口。这在真实招聘场景是直接的合规风险，而不是"以后再说"。

本模块提供
----------
  retention_status(job_id)   —— 哪些候选人已过期/将过期
  purge_expired()            —— 清理过期材料（默认只清正文，保留统计壳）
  export_person(pid)         —— 导出某个人的全部数据（JSON，供个人查阅/携带）
  erase_person(pid)          —— 彻底删除（正文+文件+people 行，留 erased_at 与审计）
  rotate_key()               —— 轮换 PII 密钥并重加密全库

关于"删除"的诚实说明
--------------------
删除不是万能的：`audit` 表里仍会留下"某人被删除了"的事实（不含正文），
这是审计要求；备份文件里的旧密文也不会自动消失。
这两点在 export/erase 的返回值和 README 里都要写清楚，不能假装删干净了。
"""
import json
import os
import shutil
from   datetime import datetime, timedelta

from engine.guardrails import audit
from   engine.intake import crypto
from   engine.storage import db
from   engine.extract import identity

PII_TABLES = ("candidates", "people", "candidate_files")


def _now():
    return db.now_iso()


# ---------------------------------------------------------------------------
# 保留期
# ---------------------------------------------------------------------------
def retention_status(job_id=None):
    """列出过期/临期记录。默认阈值：已过期 / 30 天内到期。"""
    db.init()
    conn = db.connect()
    now = _now()
    soon = (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds")

    sql = ("SELECT c.id, c.name, c.job_id, c.retention_until, j.title AS job_title"
           " FROM candidates c LEFT JOIN jobs j ON j.id = c.job_id"
           " WHERE IFNULL(c.retention_until,'')<>''")
    args = []
    if job_id:
        sql += " AND c.job_id=?"
        args.append(job_id)
    sql += " ORDER BY c.retention_until ASC"

    expired, expiring = [], []
    for r in conn.execute(sql, args):
        d = dict(r)
        if d["retention_until"] <= now:
            expired.append(d)
        elif d["retention_until"] <= soon:
            expiring.append(d)
    return {"expired": expired, "expiring": expiring,
            "expired_count": len(expired), "expiring_count": len(expiring),
            "checked_at": now}


def purge_expired(dry_run=True, delete_files=True, actor="system"):
    """清理过期候选人的**正文材料**。

    默认 dry_run=True：先看会删什么，再决定删不删——批量删除不可逆，
    不该由一次误点触发。

    dry_run=False 时：清空三类正文（保留分数与统计，便于审计"曾评过"），
    选择性删除磁盘文件，写入 `privacy_ops` 与 audit。
    """
    db.init()
    conn = db.connect()
    now = _now()
    rows = conn.execute(
        "SELECT id, name, job_id, retention_until FROM candidates"
        " WHERE IFNULL(retention_until,'')<>'' AND retention_until<=?", (now,)).fetchall()

    plan = [{"candidate_id": r["id"], "name": r["name"], "job_id": r["job_id"],
             "retention_until": r["retention_until"]} for r in rows]
    if dry_run or not plan:
        return {"dry_run": True, "would_purge": len(plan), "items": plan,
                "message": "预览：确认后以 dry_run=False 执行。" if plan else "没有过期记录。"}

    freed_files = 0
    with db.tx(conn) as c:
        for r in rows:
            c.execute(
                "UPDATE candidates SET resume_enc='', assessment_enc='', transcript_enc='',"
                " updated_at=? WHERE id=?", (now, r["id"]))
            if delete_files:
                d = os.path.join(db.DATA_DIR, "jobs", r["job_id"], "files", r["id"])
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                    freed_files += 1
        c.execute(
            "INSERT INTO privacy_ops(id, op, target_type, target_id, actor, detail_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("po_" + str(int(datetime.now().timestamp() * 1000)), "purge", "candidate", "",
             actor, db.jdump({"count": len(plan), "files": freed_files}), now))
        audit.record(c, "privacy.purge", actor=actor, target_type="candidate", target_id="",
                     detail={"count": len(plan), "files_removed": freed_files})
    return {"dry_run": False, "purged": len(plan), "files_removed": freed_files,
            "items": plan}


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export_person(pid, actor="system"):
    """导出某个人的全部数据（个人查阅/可携带权）。

    返回 dict：身份、各岗位材料与结果、回填结果、备注。
    联系方式与正文是**解密后的明文**——导出物本身就是敏感数据，
    调用方（server）必须要求 admin 权限并且不允许缓存。
    """
    db.init()
    conn = db.connect()
    p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p:
        raise KeyError("人才库里没有这个人：%s" % pid)

    entry = {
        "exported_at": _now(),
        "schema": "talent-export/1",
        "person": {
            "id": p["id"], "name": p["name"], "status": p["status"],
            "note": p["note"], "tags": db.jload(p["tags_json"], []),
            "identity": db.jload(p["identity_json"], {}),
            "created_at": p["created_at"],
        },
        "contact": "",
        "candidates": [],
        "outcomes": [],
    }
    if p["contact_enc"]:
        try:
            entry["contact"] = crypto.decrypt_text(p["contact_enc"])
        except crypto.PIICryptoError:
            entry["contact"] = None

    for r in conn.execute("SELECT * FROM candidates WHERE person_id=?", (pid,)):
        job = conn.execute("SELECT title FROM jobs WHERE id=?", (r["job_id"],)).fetchone()
        texts = {}
        for k, col in (("resume", "resume_enc"), ("assessment", "assessment_enc"),
                       ("transcript", "transcript_enc")):
            try:
                texts[k] = crypto.decrypt_text(r[col] or "")
            except crypto.PIICryptoError:
                texts[k] = None
        files = []
        for f in conn.execute("SELECT * FROM candidate_files WHERE candidate_id=?", (r["id"],)):
            files.append({"filename": f["filename"], "role": f["role"],
                          "kind": f["kind"], "chars": f["chars"],
                          "stored": f["stored"]})
        entry["candidates"].append({
            "candidate_id": r["id"],
            "job_id": r["job_id"],
            "job_title": (job["title"] if job else ""),
            "status": r["status"],
            "rubric_version": r["rubric_version"],
            "created_at": r["created_at"],
            "retention_until": r["retention_until"],
            "texts": texts,
            "files": files,
            "result": db.jload(r["result_json"], {}),
            "interview": db.jload(r["interview_json"], {}),
        })

    for r in conn.execute("SELECT * FROM outcomes WHERE person_id=? ORDER BY reached_at", (pid,)):
        entry["outcomes"].append(dict(r))

    with db.tx(conn) as c:
        c.execute("INSERT INTO privacy_ops(id, op, target_type, target_id, actor,"
                  " detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
                  ("po_" + str(int(datetime.now().timestamp() * 1000)), "export", "person",
                   pid, actor, "{}", _now()))
        audit.record(c, "privacy.export", actor=actor, target_type="person", target_id=pid)
    return entry


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
def erase_person(pid, actor="system", keep_outcomes=False):
    """彻底删除某个人的材料与档案。

    保留什么：
      · audit 里一条"谁删了什么"的记录（不含正文）——审计需要；
      · people 行被删，但 `privacy_ops` 留下操作记录。
    保留候选人的 result（分数、结论），因为它是"评审发生过"的事实；
    若连结论也要删，把 candidates 行一并删（delete_candidates=True 效果）。
    """
    db.init()
    conn = db.connect()
    p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p:
        raise KeyError("人才库里没有这个人：%s" % pid)

    cands = conn.execute("SELECT id, job_id FROM candidates WHERE person_id=?",
                         (pid,)).fetchall()
    removed_files = 0
    now = _now()
    with db.tx(conn) as c:
        for r in cands:
            c.execute("UPDATE candidates SET resume_enc='', assessment_enc='',"
                      " transcript_enc='', person_id=NULL, identity_state='erased',"
                      " updated_at=? WHERE id=?", (now, r["id"]))
            d = os.path.join(db.DATA_DIR, "jobs", r["job_id"], "files", r["id"])
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                removed_files += 1
            c.execute("DELETE FROM candidate_files WHERE candidate_id=?", (r["id"],))
        if not keep_outcomes:
            c.execute("DELETE FROM outcomes WHERE person_id=?", (pid,))
        c.execute("DELETE FROM people WHERE id=?", (pid,))
        c.execute("INSERT INTO privacy_ops(id, op, target_type, target_id, actor,"
                  " detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
                  ("po_" + str(int(datetime.now().timestamp() * 1000)), "erase", "person",
                   pid, actor, db.jdump({"candidates": len(cands),
                                         "files_removed": removed_files}), now))
        audit.record(c, "privacy.erase", actor=actor, target_type="person", target_id=pid,
                     detail={"candidates": len(cands), "files_removed": removed_files})
    return {"erased": True, "person_id": pid, "candidates_cleared": len(cands),
            "files_removed": removed_files,
            "note": "审计日志与备份中的历史密文不在此次删除范围内，详见 README。"}


# ---------------------------------------------------------------------------
# 密钥轮换
# ---------------------------------------------------------------------------
def rotate_key(actor="system"):
    """换新密钥并重加密全库 PII。

    必要性：密钥一旦泄露，仅"以后用新密钥"是不够的——历史密文仍可被解开。
    轮换会把所有存量密文用新密钥重写，旧密钥立刻失效。
    """
    db.init()
    conn = db.connect()

    rows_c = conn.execute("SELECT id, resume_enc, assessment_enc, transcript_enc"
                          " FROM candidates").fetchall()
    rows_p = conn.execute("SELECT id, contact_enc FROM people").fetchall()
    jobs_u = conn.execute("SELECT user_id, api_key_enc FROM user_settings").fetchall()

    # 先解密（用旧密钥），再换密钥，再加密写回——中途失败要能回滚
    plain_c, plain_p, plain_u = {}, {}, {}
    for r in rows_c:
        plain_c[r["id"]] = {k: (crypto.decrypt_text(r[k]) if r[k] else "")
                            for k in ("resume_enc", "assessment_enc", "transcript_enc")}
    for r in rows_p:
        plain_p[r["id"]] = crypto.decrypt_text(r["contact_enc"]) if r["contact_enc"] else ""
    for r in jobs_u:
        plain_u[r["user_id"]] = crypto.decrypt_text(r["api_key_enc"]) if r["api_key_enc"] else ""

    new_key = crypto.install_new_key()

    with db.tx(conn) as c:
        for cid, texts in plain_c.items():
            c.execute("UPDATE candidates SET resume_enc=?, assessment_enc=?, transcript_enc=?"
                      " WHERE id=?",
                      (crypto.encrypt_text(texts["resume_enc"]) if texts["resume_enc"] else "",
                       crypto.encrypt_text(texts["assessment_enc"]) if texts["assessment_enc"] else "",
                       crypto.encrypt_text(texts["transcript_enc"]) if texts["transcript_enc"] else "",
                       cid))
        for pid, contact in plain_p.items():
            c.execute("UPDATE people SET contact_enc=? WHERE id=?",
                      (crypto.encrypt_text(contact) if contact else "", pid))
        for uid, key in plain_u.items():
            c.execute("UPDATE user_settings SET api_key_enc=? WHERE user_id=?",
                      (crypto.encrypt_text(key) if key else "", uid))
        audit.record(c, "privacy.rotate_key", actor=actor,
                     detail={"candidates": len(plain_c), "people": len(plain_p),
                             "settings": len(plain_u),
                             "fingerprint": crypto.key_fingerprint(new_key)})
    return {"rotated": True, "candidates": len(plain_c), "people": len(plain_p),
            "settings": len(plain_u), "fingerprint": crypto.key_fingerprint(new_key)}


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------
def overview():
    """隐私面板用的汇总。"""
    db.init()
    conn = db.connect()
    st = retention_status()
    return {
        "key_fingerprint": crypto.key_fingerprint(),
        "expired_count": st["expired_count"],
        "expiring_count": st["expiring_count"],
        "pii_candidates": conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE IFNULL(resume_enc,'')<>''").fetchone()["n"],
        "people": conn.execute("SELECT COUNT(*) AS n FROM people").fetchone()["n"],
        "ops": [dict(r) for r in conn.execute(
            "SELECT * FROM privacy_ops ORDER BY created_at DESC LIMIT 20")],
    }
