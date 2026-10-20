"""
结果回填与校准（feedback loop / calibration / drift）。

为什么这是整个项目最不能省的一块
--------------------------------
一个打分系统如果永远不知道自己的判断对不对，那它给出的 0–10 分
**只是一个数字，不是信息**。原来这个项目从"上传简历"到"A/B/C 分级"就结束了：
没有人记录被评级为 A 的人后来有没有进面试、有没有拿到 offer、有没有留下。

后果很具体：
  · 无法回答"你们的分数准不准"——唯一诚实的答案是"不知道"；
  · 无法发现模型/提示词更新导致的分数漂移；
  · 无法向业务方证明这东西省了时间还是帮了倒忙。

所以现在有：
  · `outcomes`：每个候选人的真实流转结果（进面/offer/入职/淘汰/放弃），
    谁在什么时候记的、依据什么，全部留痕。
  · `hit_rate()`：把 AI 判断和真实结果对齐，算出 A/B/C 各档的转化率。
    这是唯一能回答"分数有没有用"的东西。
  · `calibration_items`：抽样人工评分（人的判断作为标准答案），
    算偏差（AI 比人高还是低）、排序一致性。
  · `drift()`：按 rubric_version 分组对比，区分"换尺子"和"真漂移"。

关于"不够样本就别下结论"
------------------------
所有这些函数在样本不足时**返回 insufficient=True 而不是给个数字**。
用一个 3 个样本算出来的"准确率 67%"去说服人是更坏的做法。
"""
import secrets
from   datetime import datetime, timedelta

from engine.guardrails import audit
from   engine.storage import db

# 流转阶段（正向流程 + 终止结果）
STAGES = (
    ("screened", "通过筛选"),
    ("interview_1", "一面"),
    ("interview_2", "二面"),
    ("interview_final", "终面"),
    ("offer", "发出 offer"),
    ("hired", "已入职"),
    ("rejected", "被淘汰"),
    ("withdrawn", "候选人放弃"),
    ("on_hold", "冻结/待定"),
)
STAGE_LABEL = dict(STAGES)
STAGE_ORDER = [k for k, _ in STAGES]

# 视为"正向"的阶段（用于命中率）
POSITIVE = ("interview_1", "interview_2", "interview_final", "offer", "hired")
# 深度正向（衡量"高分是否真的更有价值"）
DEEP_POSITIVE = ("offer", "hired")

MIN_SAMPLES = 10          # 低于此数不给结论
MIN_PER_GRADE = 3         # 每档至少这么多才比档间差异


def _now():
    return db.now_iso()


def _new_id():
    return "o_" + secrets.token_hex(6)


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------
def record(pid, stage, note="", job_id="", candidate_id="", actor="system", reached_at=None):
    """记录一次流转结果。可重复记录（同一人可先一面后 offer）。"""
    if stage not in STAGE_ORDER:
        raise ValueError("未知的流转阶段：%s（可选：%s）" % (stage, ", ".join(STAGE_ORDER)))
    db.init()
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
        raise KeyError("人才库里没有这个人：%s" % pid)

    oid = _new_id()
    now = _now()
    with db.tx(conn) as c:
        c.execute(
            "INSERT INTO outcomes(id, person_id, candidate_id, job_id, stage, reached_at,"
            " recorded_by, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, pid, candidate_id or "", job_id or "", stage,
             reached_at or now, actor, (note or "")[:1000], now),
        )
        audit.record(c, "outcome.record", actor=actor, target_type="person", target_id=pid,
                     detail={"stage": stage, "job_id": job_id})
    return {"id": oid, "person_id": pid, "stage": stage,
            "stage_label": STAGE_LABEL.get(stage, stage), "created_at": now}


def for_person(pid):
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM outcomes WHERE person_id=? ORDER BY reached_at ASC", (pid,)).fetchall()
    return [dict(r) for r in rows]


def latest_stage(pid):
    """某人走得最远的那一步（按阶段顺序，而不是时间）。"""
    rows = for_person(pid)
    if not rows:
        return None
    idx = {s: i for i, (s, _) in enumerate(STAGES)}
    best = max(rows, key=lambda r: idx.get(r["stage"], -1))
    return best["stage"]


def recent(limit=50, job_id=None):
    conn = db.connect()
    if job_id:
        rows = conn.execute(
            "SELECT o.*, p.name FROM outcomes o LEFT JOIN people p ON p.id = o.person_id"
            " WHERE o.job_id=? ORDER BY o.created_at DESC LIMIT ?", (job_id, int(limit)))
    else:
        rows = conn.execute(
            "SELECT o.*, p.name FROM outcomes o LEFT JOIN people p ON p.id = o.person_id"
            " ORDER BY o.created_at DESC LIMIT ?", (int(limit),))
    out = []
    for r in rows:
        d = dict(r)
        d["stage_label"] = STAGE_LABEL.get(d.get("stage"), d.get("stage"))
        out.append(d)
    return out


def delete(oid, actor="system"):
    conn = db.connect()
    with db.tx(conn) as c:
        cur = c.execute("DELETE FROM outcomes WHERE id=?", (oid,))
        audit.record(c, "outcome.delete", actor=actor, target_type="outcome", target_id=oid)
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 命中率：AI 判断 vs 真实结果
# ---------------------------------------------------------------------------
def hit_rate(job_id=None, since_days=None):
    """按 AI 等级统计真实转化率。

    返回每个等级的：样本数、进面率、offer/入职率。
    样本不足时 insufficient=True，数值仍然给出（明确标注不可用）。
    """
    db.init()
    conn = db.connect()

    # 每人取"最高分那次评审"作为 AI 判断（同一人多次评审时按最强证据算）
    sql = """
        SELECT c.person_id, c.job_id, c.result_json, c.created_at
        FROM candidates c
        WHERE c.person_id IS NOT NULL AND c.result_json IS NOT NULL
    """
    args = []
    if job_id:
        sql += " AND c.job_id=?"
        args.append(job_id)
    if since_days:
        cutoff = (datetime.now() - timedelta(days=int(since_days))).isoformat(timespec="seconds")
        sql += " AND c.created_at>=?"
        args.append(cutoff)

    best = {}
    for r in conn.execute(sql, args):
        res = db.jload(r["result_json"], {})
        total = res.get("total")
        if total is None:
            continue
        cur = best.get(r["person_id"])
        if cur is None or total > cur["total"]:
            best[r["person_id"]] = {"total": total, "grade": res.get("grade"),
                                    "job_id": r["job_id"]}

    # 真实结果
    reached = {}
    for r in conn.execute("SELECT person_id, stage FROM outcomes"):
        reached.setdefault(r["person_id"], set()).add(r["stage"])

    buckets = {}
    for pid, info in best.items():
        g = info["grade"] or "?"
        b = buckets.setdefault(g, {"grade": g, "n": 0, "interview": 0,
                                   "deep": 0, "rejected": 0,
                                   "totals": [], "outcome_known": 0})
        b["n"] += 1
        b["totals"].append(info["total"])
        stages = reached.get(pid) or set()
        if stages:
            b["outcome_known"] += 1
        if stages & set(POSITIVE):
            b["interview"] += 1
        if stages & set(DEEP_POSITIVE):
            b["deep"] += 1
        if "rejected" in stages:
            b["rejected"] += 1

    total_scored = len(best)
    with_outcome = sum(1 for pid in best if reached.get(pid))
    rows = []
    for g in sorted(buckets, key=lambda x: {"A": 0, "B": 1, "C": 2}.get(x, 9)):
        b = buckets[g]
        known = b["outcome_known"]
        rows.append({
            "grade": g,
            "scored": b["n"],
            "outcome_known": known,
            "avg_score": round(sum(b["totals"]) / len(b["totals"]), 2) if b["totals"] else None,
            "interview": b["interview"],
            "interview_rate": round(b["interview"] / known, 3) if known else None,
            "deep": b["deep"],
            "deep_rate": round(b["deep"] / known, 3) if known else None,
            "rejected": b["rejected"],
            "coverage": round(known / b["n"], 3) if b["n"] else 0.0,
        })

    enough = with_outcome >= MIN_SAMPLES and all(
        r["outcome_known"] >= MIN_PER_GRADE for r in rows) and len(rows) >= 2

    # 排序一致性：A 档进面率应当高于 C 档，否则分数没有区分力
    verdict = None
    if enough:
        by = {r["grade"]: r for r in rows}
        if "A" in by and "C" in by and by["A"]["interview_rate"] is not None \
                and by["C"]["interview_rate"] is not None:
            diff = by["A"]["interview_rate"] - by["C"]["interview_rate"]
            if diff > 0.05:
                verdict = {"ok": True, "monotonic": True,
                           "message": "A 档进面率高于 C 档（+%.0f 个百分点），分数具备区分力。"
                                      % (diff * 100)}
            else:
                verdict = {"ok": False, "monotonic": False,
                           "message": "A 档进面率没有明显高于 C 档（%.0f 个百分点）——"
                                      "当前分数**没有区分力**，不应作为筛选依据。"
                                      % (diff * 100)}

    return {
        "insufficient": not enough,
        "min_samples": MIN_SAMPLES,
        "scored_total": total_scored,
        "outcome_known": with_outcome,
        "coverage": round(with_outcome / total_scored, 3) if total_scored else 0.0,
        "by_grade": rows,
        "verdict": verdict,
        "message": None if enough else
                   ("回填结果不足（已有 %d 份有结果的样本，至少需要 %d 份且每档 ≥%d），"
                    "暂不能判断分数是否有效。" % (with_outcome, MIN_SAMPLES, MIN_PER_GRADE)),
    }


# ---------------------------------------------------------------------------
# 人工标定
# ---------------------------------------------------------------------------
def _new_calib_id():
    return "cs_" + secrets.token_hex(5)


def create_set(name, description=""):
    conn = db.connect()
    sid = _new_calib_id()
    with db.tx(conn) as c:
        c.execute("INSERT INTO calibration_sets(id, name, description, created_at)"
                  " VALUES (?,?,?,?)", (sid, name or "标定集", description or "", _now()))
    return {"id": sid, "name": name, "description": description}


def list_sets():
    conn = db.connect()
    out = []
    for r in conn.execute("SELECT * FROM calibration_sets ORDER BY created_at DESC"):
        n = conn.execute("SELECT COUNT(*) AS n FROM calibration_items WHERE set_id=?",
                         (r["id"],)).fetchone()["n"]
        out.append({**dict(r), "item_count": n})
    return out


def add_item(set_id, human_label, human_score=None, candidate_id="", rubric_version="",
             note="", actor="system"):
    """人工给一个候选人打标（这就是"标准答案"）。"""
    conn = db.connect()
    item = "ci_" + secrets.token_hex(6)
    ai_total, ai_grade, ai_scores, model = None, None, {}, ""
    if candidate_id:
        r = conn.execute("SELECT result_json, rubric_version FROM candidates WHERE id=?",
                         (candidate_id,)).fetchone()
        if r:
            res = db.jload(r["result_json"], {})
            ai_total = res.get("total")
            ai_grade = res.get("grade")
            ai_scores = res.get("scores") or {}
            model = (res.get("model") or "")
            rubric_version = rubric_version or (r["rubric_version"] or "")
    with db.tx(conn) as c:
        c.execute(
            "INSERT INTO calibration_items(id, set_id, candidate_id, human_label, human_score,"
            " rubric_version, ai_total, ai_grade, ai_scores_json, model, recorded_at, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item, set_id, candidate_id or "", human_label, human_score, rubric_version,
             ai_total, ai_grade, db.jdump(ai_scores), model, _now(), (note or "")[:500]),
        )
    return {"id": item, "ai_total": ai_total, "ai_grade": ai_grade}


def calibration_report(set_id=None):
    """人工 vs AI 的偏差与排序一致性。

    mean_gap > 0 表示 AI 普遍给分偏高。
    spearman 用无并列的简化秩相关；样本 < 8 时不给相关系数。
    """
    db.init()
    conn = db.connect()
    sql = "SELECT * FROM calibration_items"
    args = []
    if set_id:
        sql += " WHERE set_id=?"
        args.append(set_id)
    rows = conn.execute(sql, args).fetchall()

    pairs = [(r["human_score"], r["ai_total"]) for r in rows
             if r["human_score"] is not None and r["ai_total"] is not None]

    gaps = [ai - hu for hu, ai in pairs]
    res = {
        "items": len(rows),
        "pairs": len(pairs),
        "mean_gap": round(sum(gaps) / len(gaps), 3) if gaps else None,
        "mean_abs_gap": round(sum(abs(g) for g in gaps) / len(gaps), 3) if gaps else None,
        "max_gap": round(max(gaps), 2) if gaps else None,
        "min_gap": round(min(gaps), 2) if gaps else None,
        "spearman": None,
        "agreement": None,
        "insufficient": len(pairs) < 8,
    }
    if pairs:
        res["agreement"] = round(
            sum(1 for hu, ai in pairs if abs(ai - hu) <= 1.0) / len(pairs), 3)
    if len(pairs) >= 8:
        res["spearman"] = _spearman([p[0] for p in pairs], [p[1] for p in pairs])

    # 标签一致性（A/B/C）
    lab = [(r["human_label"], r["ai_grade"]) for r in rows if r["ai_grade"]]
    if lab:
        res["label_agree"] = round(
            sum(1 for h, a in lab if h == a) / len(lab), 3)
        res["label_pairs"] = len(lab)
    if res["insufficient"]:
        res["message"] = ("样本 %d 对，不足 8 对，不给相关性与结论。"
                          "样本少时算出的相关性基本都是噪声。" % len(pairs))
    return res


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _spearman(a, b):
    ra, rb = _ranks(a), _ranks(b)
    n = len(ra)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db_ = sum((x - mb) ** 2 for x in rb) ** 0.5
    if da == 0 or db_ == 0:
        return None
    return round(num / (da * db_), 3)


# ---------------------------------------------------------------------------
# 漂移监控
# ---------------------------------------------------------------------------
def drift(limit_versions=6, job_id=None):
    """按 (岗位, rubric_version) 分组对比分数分布，发现"换模型/改口径"带来的漂移。

    version 已包含模型名，所以版本不同既可能是改口径也可能是换模型；
    这里只报警"分布变了"，具体原因需要人看 `parts`。

    为什么必须按岗位分组
    --------------------
    原来是 `SELECT ... FROM candidates`（**没有 job 过滤**），把所有岗位的
    分数倒进同一个桶算均值。这是个方法论错误：

      · "高级后端"和"初级前端"的分数本来就该不同，混在一起算出的均值
        只反映"这两类岗位各占多少"，不反映任何口径变化；
      · 更糟的是，某个岗位新导入一批简历，就能让"均值偏移 0.5 分"的告警
        响起来——而口径一个字都没改。这是典型的辛普森悖论式误判。

    所以先按岗位分桶，再在岗位内部按版本比较。job_id 不传时只返回
    各岗位的汇总，不跨岗位做偏移告警。
    """
    db.init()
    conn = db.connect()
    sql = ("SELECT rubric_version, result_json, created_at, job_id FROM candidates"
           " WHERE result_json IS NOT NULL")
    args = []
    if job_id:
        sql += " AND job_id=?"
        args.append(job_id)

    # buckets[(job_id, version)] = {...}
    buckets = {}
    for r in conn.execute(sql, args):
        res = db.jload(r["result_json"], {})
        total = res.get("total")
        if total is None:
            continue
        jid = r["job_id"] or ""
        v = r["rubric_version"] or "未标注"
        b = buckets.setdefault((jid, v), {
            "job_id": jid, "version": v, "n": 0, "totals": [],
            "grades": {"A": 0, "B": 0, "C": 0},
            "first": r["created_at"], "last": r["created_at"]})
        b["n"] += 1
        b["totals"].append(total)
        if res.get("grade") in b["grades"]:
            b["grades"][res["grade"]] += 1
        b["first"] = min(b["first"], r["created_at"])
        b["last"] = max(b["last"], r["created_at"])

    out = []
    for (jid, v), b in buckets.items():
        ts = sorted(b["totals"])
        n = len(ts)
        out.append({
            "job_id": jid, "version": v, "n": n,
            "mean": round(sum(ts) / n, 2),
            "median": ts[n // 2],
            "min": ts[0], "max": ts[-1],
            "grades": b["grades"],
            "first": b["first"], "last": b["last"],
        })
    out.sort(key=lambda x: x["last"], reverse=True)
    out = out[:int(limit_versions)]

    # 只在**同一岗位内部**比较版本，跨岗位一律不比较。
    #
    # 措辞刻意不声称"谁比谁新"：两批数据可能是同一秒写入的，
    # 此时时间戳无法判定先后，硬说"新版比旧版偏移 +3 分"就是在编方向。
    # 只报告"这两版差多少"——这个事实无论先后都成立。
    alerts = []
    by_job = {}
    for b in out:
        by_job.setdefault(b["job_id"], []).append(b)
    for jid, lst in by_job.items():
        if len(lst) < 2:
            continue
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b2 = lst[i], lst[j]
                if a["n"] < 5 or b2["n"] < 5:
                    continue
                diff = abs(a["mean"] - b2["mean"])
                if diff >= 0.5:
                    alerts.append(
                        "岗位 %s 内两个口径的均分相差 %.2f 分（%s: %.2f / %d 份；"
                        "%s: %.2f / %d 份）——分数不可跨版本比较，请勿混排。"
                        % (jid or "（未标注）", diff,
                           a["version"], a["mean"], a["n"],
                           b2["version"], b2["mean"], b2["n"]))
    return {"versions": out, "alerts": alerts,
            "grouped_by": "job_id",
            "note": "按岗位分组统计：不同岗位的分数分布本就不同，跨岗位比较没有意义。",
            "message": None if out else "还没有任何带口径版本的评审结果。"}
